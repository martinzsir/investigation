"""
server/app/hypotheses_store.py
案件级人工假设持久化（P1）。

设计要点：
  - **只持久化人工部分**。自动假设是从 findings/rules 派生的（每次 BUILD
    重算，随数据变化），落盘会造成「产物与数据不一致」的漂移；人工假设是
    正兵的判断（add/reorder/promote/remove），不该被重扫冲掉。
  - 落盘位置 cases/<cid>/hypotheses.json（案件快照根，与 lenses.json 同级），
    不进本体指纹/归档——改假设 ≠ 改本体版本。
  - 容错口径同 snapshot_config.load_lens_overrides：文件缺失/损坏回落空，
    单条非法只忽略该条，不让配置问题拖垮 BUILD。
  - 人工假设 id 用 HM 前缀（HM1/HM2…），与自动 H1..H5 天然隔离；
    合并展示时自动在前、人工在后（人工是「正兵补充」，不抢占编号）。

红线：
  - 证伪条件（falsification）必填——庙算招牌能力是「自动证伪」，无此字段
    的假设不入库。
  - 每次变更写审计条目（operator + ts + action），可追溯谁在什么时候改了什么。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

HYPOTHESES_FILENAME = "hypotheses.json"
SCHEMA_VERSION = 1

# 字段长度约束（与画布人工节点同一量级，防超长文本污染产物）
DESC_MAX = 100
FIELD_MAX = 200
AUDIT_KEEP = 200          # 审计条目保留上限（环形截断）
MANUAL_ID_PREFIX = "HM"   # 人工假设 id 前缀，与自动 H* 隔离

_REQUIRED_TEXT = ("description", "falsification")
_TEXT_FIELDS = ("description", "procedure", "falsification",
                "evidence_needed", "data_sources")


def hypotheses_path(case_dir: str | Path) -> Path:
    """案件人工假设文件路径：cases/<cid>/hypotheses.json。"""
    return Path(case_dir) / HYPOTHESES_FILENAME


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_manual(case_dir: str | Path) -> dict[str, Any]:
    """读案件人工假设 → {"items": [...], "order": [...], "audit": [...]}。

    缺失/损坏回落空结构（调用方照常工作，不报错）。
    """
    empty: dict[str, Any] = {"items": [], "order": [], "audit": []}
    path = hypotheses_path(case_dir)
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, AttributeError):
        return empty
    if not isinstance(data, dict):
        return empty
    items = data.get("items")
    order = data.get("order")
    audit = data.get("audit")
    return {
        "items": items if isinstance(items, list) else [],
        "order": order if isinstance(order, list) else [],
        "audit": audit if isinstance(audit, list) else [],
    }


class HypothesesError(ValueError):
    """人工假设业务校验失败：路由层统一转 400 VALIDATION。"""


def _clean_text(v: Any, label: str, max_len: int) -> str:
    s = str(v or "").strip()
    if not s:
        raise HypothesesError(f"请填写{label}")
    if len(s) > max_len:
        raise HypothesesError(f"{label}不超过 {max_len} 字")
    return s


def _clean_list(v: Any, label: str, max_len: int) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        raise HypothesesError(f"{label}必须是数组")
    out: list[str] = []
    for x in v:
        s = str(x or "").strip()
        if s:
            if len(s) > max_len:
                raise HypothesesError(f"{label}单项不超过 {max_len} 字")
            out.append(s)
    return out


def normalize_incoming(body: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化入参 → 可落盘的假设条目。

    必填：description + falsification（证伪条件是庙算硬要求）。
    可选：evidence_needed / data_sources / procedure / dimension / jian_types。
    """
    if not isinstance(body, dict):
        raise HypothesesError("请求体必须为对象")
    desc = _clean_text(body.get("description"), "假设描述", DESC_MAX)
    fals = _clean_text(body.get("falsification"), "证伪条件", FIELD_MAX)
    return {
        "description": desc,
        "falsification": fals,
        "evidence_needed": _clean_list(body.get("evidence_needed"),
                                       "所需证据", FIELD_MAX),
        "data_sources": _clean_list(body.get("data_sources"),
                                    "可调用数据源", FIELD_MAX),
        "procedure": str(body.get("procedure") or "").strip()[:FIELD_MAX],
        "dimension": _clean_list(body.get("dimension"), "维度", 50),
        "jian_types": _clean_list(body.get("jian_types"), "间类", 50),
    }


def _next_id(items: list[dict]) -> str:
    """下一个人工假设 id：HM1/HM2…（跳过已占用编号，防删除后撞号）。"""
    used = set()
    for it in items:
        hid = str(it.get("id") or "")
        if hid.startswith(MANUAL_ID_PREFIX):
            tail = hid[len(MANUAL_ID_PREFIX):]
            if tail.isdigit():
                used.add(int(tail))
    n = 1
    while n in used:
        n += 1
    return f"{MANUAL_ID_PREFIX}{n}"


def add_manual(case_dir: str | Path, body: dict[str, Any],
               operator: str) -> dict[str, Any]:
    """新增人工假设（返回落盘后的完整结构）。"""
    data = load_manual(case_dir)
    items = [x for x in data["items"] if isinstance(x, dict)]
    entry = normalize_incoming(body)
    entry["id"] = _next_id(items)
    entry["source"] = "manual"
    entry["status"] = "待推演"
    entry["created_by"] = operator
    entry["created_at"] = _now()
    items.append(entry)
    data["items"] = items
    data["order"] = [str(x.get("id")) for x in items]
    data["audit"] = _append_audit(
        data.get("audit"), "add", f"{entry['id']} {entry['description']}",
        operator)
    _save(case_dir, data)
    return data


def update_manual(case_dir: str | Path, hid: str, body: dict[str, Any],
                  operator: str) -> dict[str, Any]:
    """修改已有人工假设（只能改人工的，自动假设不可改）。"""
    data = load_manual(case_dir)
    items = [x for x in data["items"] if isinstance(x, dict)]
    target = None
    for it in items:
        if str(it.get("id")) == hid:
            target = it
            break
    if target is None:
        raise HypothesesError(f"人工假设不存在：{hid}（自动生成的假设不可修改）")
    patch = normalize_incoming({**target, **body})
    for k, v in patch.items():
        target[k] = v
    target["updated_by"] = operator
    target["updated_at"] = _now()
    data["items"] = items
    data["audit"] = _append_audit(
        data.get("audit"), "update", f"{hid} {target['description']}", operator)
    _save(case_dir, data)
    return data


def remove_manual(case_dir: str | Path, hid: str,
                  operator: str) -> dict[str, Any]:
    """删除人工假设。"""
    data = load_manual(case_dir)
    items = [x for x in data["items"] if isinstance(x, dict)]
    rest = [x for x in items if str(x.get("id")) != hid]
    if len(rest) == len(items):
        raise HypothesesError(f"人工假设不存在：{hid}")
    data["items"] = rest
    data["order"] = [str(x.get("id")) for x in rest]
    data["audit"] = _append_audit(data.get("audit"), "remove", hid, operator)
    _save(case_dir, data)
    return data


def reorder(case_dir: str | Path, ordered_ids: list[str],
            operator: str) -> dict[str, Any]:
    """重排人工假设顺序（必须与现有人工假设一一对应）。"""
    data = load_manual(case_dir)
    items = [x for x in data["items"] if isinstance(x, dict)]
    current = [str(x.get("id")) for x in items]
    ids = [str(x) for x in (ordered_ids or [])]
    if len(set(ids)) != len(ids) or sorted(ids) != sorted(current):
        raise HypothesesError(
            f"重排序列表必须与现有人工假设一一对应：现有 {current}")
    by_id = {str(x.get("id")): x for x in items}
    data["items"] = [by_id[i] for i in ids]
    data["order"] = ids
    data["audit"] = _append_audit(data.get("audit"), "reorder",
                                  " → ".join(ids), operator)
    _save(case_dir, data)
    return data


def _append_audit(audit: Any, action: str, detail: str,
                  operator: str) -> list[dict]:
    out = [x for x in (audit or []) if isinstance(x, dict)]
    out.append({"action": action, "detail": detail,
                "operator": operator, "ts": _now()})
    return out[-AUDIT_KEEP:]


def _save(case_dir: str | Path, data: dict[str, Any]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "items": data.get("items") or [],
        "order": data.get("order") or [],
        "audit": data.get("audit") or [],
    }
    _atomic_write_json(hypotheses_path(case_dir), payload)
