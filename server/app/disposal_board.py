"""
server/app/disposal_board.py
W-P-001 五泳道处置看板组装（纯只读，不触发任务/扫描）。

三源口径与 clues_view 一致：
  - artifacts/clues_v{N}.json：线索属性（title/jian_types/level/priority_score）；
  - state.sqlite clue_disposal_status：处置状态真值（status/note/operator/
    updated_at），缺省待查；
  - 秩级过滤：正兵及以下不见内间线索卡（REQ-011，与线索列表同口径）。

stay_days/overdue：stay_days = 参照日 - updated_at（无处置记录 → None，
前端显示 "—"）；overdue = stay_days > 阈值（快照 thresholds.json
disposal.stale_days，缺省 14）。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.access import can_see_jian_types
from core.registry import ClueStatus

from server.app import ontology_meta
from server.app.clues_view import (
    _load_raw,
    _status_of,
)

DEFAULT_STALE_DAYS = 14

# detail 键 → 主体类型（主体摘要提取用；长文/规则键不取）
_SUBJECT_TYPE_BY_KEY = {
    "主体": "person", "当事人": "person", "姓名": "person", "法人": "person",
    "账号": "account", "账户": "account", "卡号": "account", "账户名": "account",
    "公司": "org", "企业": "org", "机构": "org", "单位": "org",
    "手机": "phone", "电话": "phone", "号码": "phone",
}
_SUBJECT_SKIP_KEYS = {
    "rule_id", "rule_text", "依据", "级别", "等级", "level", "dimension",
    "priority_rank", "priority_score", "merged_from", "suppressed_log",
    "交叉等级",
}
_SUBJECT_NAME_KEYS = ("主体", "姓名", "名称", "账号", "账户", "卡号",
                      "公司", "raw_name", "name", "title")


def stale_days_threshold(snapshot_base: str | Path, pack: str) -> int:
    """读快照 thresholds.json disposal.stale_days；缺失/非法回落 14。"""
    p = Path(snapshot_base) / pack / "thresholds.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        v = (data.get("disposal") or {}).get("stale_days")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return int(v)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return DEFAULT_STALE_DAYS


def _add_subject(out: list[dict], seen: set, name: Any, stype: str) -> None:
    s = str(name or "").strip()
    if not s or s in seen or len(s) > 40:
        return
    seen.add(s)
    out.append({"name": s, "type": stype})


def _subjects(raw: dict) -> list[dict]:
    """主体摘要：从 detail 结构化字段提取名称+类型（自由文本/规则判据不取）。"""
    det = raw.get("detail") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for key, val in det.items():
        if key in _SUBJECT_SKIP_KEYS:
            continue
        stype = _SUBJECT_TYPE_BY_KEY.get(str(key), "other")
        items = val if isinstance(val, list) else [val]
        for item in items:
            if isinstance(item, str):
                _add_subject(out, seen, item, stype)
            elif isinstance(item, dict):
                for nk in _SUBJECT_NAME_KEYS:
                    if nk in item:
                        _add_subject(out, seen, item[nk], stype)
                        break
    return out[:5]


def _stay(updated_at: str, today: date, threshold: int) -> dict:
    if not updated_at:
        return {"stay_days": None, "overdue": False}
    d = None
    try:
        d = datetime.fromisoformat(updated_at).date()
    except ValueError:
        try:
            d = datetime.strptime(updated_at[:10], "%Y-%m-%d").date()
        except ValueError:
            d = None
    if d is None:
        return {"stay_days": None, "overdue": False}
    stay = (today - d).days
    return {"stay_days": stay, "overdue": stay > threshold}


def assemble_board(*, case_dir: str | Path, state_map: dict[str, dict],
                   role: str = "正兵", snapshot_base: str | Path,
                   pack: str, as_of: date | None = None) -> dict[str, Any]:
    """五泳道看板。未产出线索 → available:false（空泳道，不报错）。"""
    raws, art_ver = _load_raw(Path(case_dir), None)
    # R5：间类可见性声明化；R6：泳道顺序取 states.json 声明序
    jian_clearances = ontology_meta.jian_clearances(pack, snapshot_base)
    decl = ontology_meta.states_decl(pack, snapshot_base)
    columns_order = [s["name"] for s in decl["states"]]
    sla_by_status = {s["name"]: s["sla_days"] for s in decl["states"]
                     if isinstance(s.get("sla_days"), int)}
    threshold = stale_days_threshold(snapshot_base, pack)
    today = as_of or date.today()

    columns: dict[str, list[dict]] = {s: [] for s in columns_order}
    counts = {s: 0 for s in columns_order}
    total = 0
    restricted = 0

    for raw in raws:
        jts = raw.get("jian_types") or []
        if not can_see_jian_types(jts, role=role,
                                 jian_clearances=jian_clearances):
            restricted += 1
            continue
        st = _status_of(raw, state_map)
        status = (st["status"] if st["status"] in columns
                  else ClueStatus.PENDING)
        det = raw.get("detail") or {}
        card = {
            "clue_id": raw.get("clue_id"),
            "title": raw.get("title", ""),
            "jian_types": jts,
            "level": det.get("级别") or det.get("level"),
            "priority_score": det.get("priority_score"),
            "score_basis": det.get("score_basis"),
            "score_formula": det.get("score_formula"),
            "score_source": det.get("score_source"),
            "subjects": _subjects(raw),
            "status": status,
            "note": st.get("note", ""),
            "operator": st.get("operator", ""),
            "updated_at": st.get("updated_at", ""),
        }
        # D2：分态 SLA（states.json sla_days）优先，未声明的状态回落全局阈值
        card.update(_stay(card["updated_at"], today,
                          sla_by_status.get(status, threshold)))
        columns[status].append(card)
        counts[status] += 1
        total += 1

    # 泳道内按 priority_score 降序（None 垫底），clue_id 稳定
    for cards in columns.values():
        cards.sort(key=lambda c: (
            -(c["priority_score"] if isinstance(c["priority_score"],
                                                (int, float)) else -1),
            c["clue_id"] or ""))

    out: dict[str, Any] = {
        "available": art_ver is not None,
        "stale_days_threshold": threshold,
        "sla_days": sla_by_status,
        "counts": {"total": total, "by_status": counts},
        "columns": columns,
    }
    if art_ver is None:
        out["note"] = "案件尚未产出线索（先运行 BUILD/RESCAN）"
    if restricted:
        out["access_note"] = (
            f"role={role}：按间类密级策略过滤线索 {restricted} 条（REQ-011）")
    return out
