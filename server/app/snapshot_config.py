"""
server/app/snapshot_config.py
M4 配置类读写共享助手（W-013/015/016/017 复用 rule_workshop 范式）。

核心：配置文件写案件快照（cases/{cid}/ontology/<pack>/xxx.json），
写盘前在临时副本上过 core.ontology_loader.load_pack 全量强校验，
不合法不落盘；合法后 os.replace 原子替换。

与 rule_workshop.py 同构，抽取共享以避免重复。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from core.ontology_loader import load_pack

from server.app.deps import WebContext
from server.app.envelope import ERR_FORBIDDEN, ERR_NOT_FOUND, APIError
from server.app.security import Principal


def snapshot_paths(ctx: WebContext, case_id: str
                   ) -> tuple[str, Path, Path]:
    """返回 (pack_id, 快照包目录, 快照 base_dir)。"""
    case = ctx.repo.get_case(case_id)
    pack_id = case.pack_id if case else "default"
    snap_dir = ctx.cases.snapshot_dir(case_id, pack_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    if not snap_dir.exists():
        raise APIError(ERR_NOT_FOUND, f"案件快照不存在：{case_id}", 404)
    return pack_id, snap_dir, base_dir


def require_analyst(p: Principal) -> None:
    """偏将及以上（clearance>=2）或 human/system 才可写配置。"""
    if p.role not in ("human", "system") and p.clearance < 2:
        raise APIError(ERR_FORBIDDEN,
                       "配置写入需偏将及以上（clearance>=2）", 403)


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def validate_snapshot(snap_dir: Path, pack_id: str,
                      base_dir: Path) -> None:
    """在临时副本上跑 load_pack 全量校验，不合法抛异常（不落盘）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        load_pack(pack_id, base_dir=tmp_root)


def save_config_json(snap_dir: Path, pack_id: str, base_dir: Path,
                     filename: str, data, *, ctx: WebContext,
                     case_id: str, op: str, p: Principal,
                     detail: dict | None = None) -> None:
    """校验通过后原子写配置文件 + 记 ops 审计。"""
    validate_snapshot(snap_dir, pack_id, base_dir)
    atomic_write_json(snap_dir / filename, data)
    ctx.repo.record_ops(op, case_id,
                        {"file": filename, "by": p.operator,
                         **(detail or {})})


def record_config_audit(ctx: WebContext, case_id: str, p: Principal, op: str,
                        *, filename: str, reason: str | None = None,
                        summary: dict | None = None) -> None:
    """配置写追加进案件审计链（state.sqlite audit_chain 哈希链；FE-T-012）。

    在配置文件原子落盘后调用：after_state 带 config_action 标记，
    timeline 的 action 派生为 "config"（core.audit._event_action），
    note 列落变更理由；配置整包不进链（体量大、快照文件本身可溯），
    链上只留 谁/何时/哪个文件/改了什么摘要/理由。

    审计追加失败不回滚配置（数据已生效），降级为 ops 错误事件留痕，
    避免留痕故障阻断已成功的配置写。

    state.sqlite 写入在 store 层（state_sink，state_store 唯一合法消费面
    之一）；本助手只做版本解析与失败降级编排。
    """
    from server.app.store.state_sink import append_config_event

    try:
        version = ctx.repo.current_version(case_id)
        append_config_event(
            state_path=ctx.factory.case_dir(case_id) / "state.sqlite",
            case_id=case_id,
            operator=p.operator,
            op=op,
            filename=filename,
            ontology_version=f"v{version}",
            reason=reason or "",
            summary=summary,
        )
    except Exception as e:  # 留痕失败不阻断主流程
        ctx.repo.record_ops("config_audit_failed", case_id,
                            {"op": op, "file": filename, "by": p.operator,
                             "error": str(e)[:120]})
