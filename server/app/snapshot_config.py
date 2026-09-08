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
