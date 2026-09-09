"""
server/app/routers/knowledge.py
W-016 知识包维护（M4 阶段 A）。

读：GET /cases/{cid}/knowledge —— 关系断言列表 + valid_until + 主体别名。
写：POST/PUT /cases/{cid}/knowledge 🔒 —— 增改/停用断言；
    过期断言扫描自动排除（core r5 既有）；变更进审计链。
    （敏感地点白名单后端暂无字段，MVP-4 不做。）
权限：GET 登录即可；写需偏将及以上。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.ontology_loader import load_pack

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.snapshot_config import (
    atomic_write_json,
    record_config_audit,
    require_analyst,
    snapshot_paths,
)

router = APIRouter(tags=["knowledge"])


class KnowledgeIn(BaseModel):
    relation_assertions: list[dict]
    subject_aliases: dict | None = None
    reason: str | None = None  # 变更理由（FE-T-012，落审计链 note）


@router.get("/cases/{case_id}/knowledge")
def list_knowledge(case_id: str,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    data = json.loads((snap_dir / "case_knowledge.json").read_text(
        encoding="utf-8"))
    return ok({
        "relation_assertions": data.get("relation_assertions", []),
        "subject_aliases": data.get("subject_aliases", {}),
        "pack": pack_id,
    }, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/knowledge")
def save_knowledge(case_id: str, body: KnowledgeIn,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)

    for a in body.relation_assertions:
        if not all(k in a for k in ("from", "to", "type")):
            raise APIError(ERR_VALIDATION,
                           f"断言缺少必填字段 from/to/type：{a}", 400)

    path = snap_dir / "case_knowledge.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["relation_assertions"] = body.relation_assertions
    if body.subject_aliases is not None:
        data["subject_aliases"] = body.subject_aliases

    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        atomic_write_json(tmp_root / pack_id / "case_knowledge.json", data)
        try:
            load_pack(pack_id, base_dir=tmp_root)
        except Exception as e:
            raise APIError(ERR_VALIDATION,
                           f"case_knowledge.json 校验失败，未落盘：{e}", 400)
    atomic_write_json(path, data)
    ctx.repo.record_ops("knowledge_save", case_id,
                        {"by": p.operator,
                         "assertions": len(body.relation_assertions)})
    record_config_audit(ctx, case_id, p, "knowledge_save",
                        filename="case_knowledge.json", reason=body.reason,
                        summary={"assertions": len(body.relation_assertions)})
    return ok({"saved": True,
               "assertions": len(body.relation_assertions)},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/knowledge")
def add_knowledge(case_id: str, body: KnowledgeIn,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    """POST=追加断言（合并到现有列表），其余同 PUT。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)

    path = snap_dir / "case_knowledge.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    existing = list(data.get("relation_assertions", []))
    for a in body.relation_assertions:
        if not all(k in a for k in ("from", "to", "type")):
            raise APIError(ERR_VALIDATION,
                           f"断言缺少必填字段 from/to/type：{a}", 400)
        existing.append(a)
    data["relation_assertions"] = existing
    if body.subject_aliases is not None:
        merged = dict(data.get("subject_aliases") or {})
        merged.update(body.subject_aliases)
        data["subject_aliases"] = merged

    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        atomic_write_json(tmp_root / pack_id / "case_knowledge.json", data)
        try:
            load_pack(pack_id, base_dir=tmp_root)
        except Exception as e:
            raise APIError(ERR_VALIDATION,
                           f"case_knowledge.json 校验失败，未落盘：{e}", 400)
    atomic_write_json(path, data)
    ctx.repo.record_ops("knowledge_add", case_id,
                        {"by": p.operator, "added": len(body.relation_assertions)})
    record_config_audit(ctx, case_id, p, "knowledge_add",
                        filename="case_knowledge.json", reason=body.reason,
                        summary={"added": len(body.relation_assertions),
                                 "total": len(existing)})
    return ok({"added": len(body.relation_assertions),
               "total": len(existing)},
              data_version=ctx.repo.current_version(case_id))
