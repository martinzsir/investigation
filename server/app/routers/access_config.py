"""
server/app/routers/access_config.py
W-015 权限与字段遮蔽配置（M4 阶段 A）。

读：GET /cases/{cid}/policies —— 对象/链接策略 + 字段遮蔽矩阵。
写：PUT /cases/{cid}/policies 🔒 —— 整体替换 policies.json；
    未声明对象一律拒绝（fail-closed，矩阵不留白）；保存即生效
    （PolicyEngine 读时执行，无需重建语义层，AC-6）。
权限：GET 登录即可；PUT 需偏将及以上。
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

router = APIRouter(tags=["access-config"])

ALLOWED_MASKS = {"partial", "full", "none"}


class PoliciesIn(BaseModel):
    object_policies: list[dict]
    link_policies: list[dict]
    property_policies: list[dict]
    reason: str | None = None  # 变更理由（FE-T-012，落审计链 note）


def _check_property_policy(pp: dict) -> None:
    if pp.get("default") not in ("allow", "deny"):
        raise APIError(ERR_VALIDATION,
                       f"属性策略 default 仅可选 allow/deny：{pp}", 400)
    mask = pp.get("mask")
    if mask and mask not in ALLOWED_MASKS:
        raise APIError(ERR_VALIDATION,
                       f"mask 仅可选 {sorted(ALLOWED_MASKS)}：{mask}", 400)


@router.get("/cases/{case_id}/policies")
def list_policies(case_id: str,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    data = json.loads((snap_dir / "policies.json").read_text(
        encoding="utf-8"))
    return ok({
        "object_policies": data.get("object_policies", []),
        "link_policies": data.get("link_policies", []),
        "property_policies": data.get("property_policies", []),
        "pack": pack_id,
    }, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/policies")
def save_policies(case_id: str, body: PoliciesIn,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)

    for pp in body.property_policies:
        _check_property_policy(pp)

    path = snap_dir / "policies.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["object_policies"] = body.object_policies
    data["link_policies"] = body.link_policies
    data["property_policies"] = body.property_policies

    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        atomic_write_json(tmp_root / pack_id / "policies.json", data)
        try:
            load_pack(pack_id, base_dir=tmp_root)
        except Exception as e:
            raise APIError(ERR_VALIDATION,
                           f"policies.json 校验失败，未落盘：{e}", 400)
    atomic_write_json(path, data)
    ctx.repo.record_ops("policies_save", case_id,
                        {"by": p.operator,
                         "objects": len(body.object_policies),
                         "links": len(body.link_policies),
                         "properties": len(body.property_policies)})
    record_config_audit(ctx, case_id, p, "policies_save",
                        filename="policies.json", reason=body.reason,
                        summary={"objects": len(body.object_policies),
                                 "links": len(body.link_policies),
                                 "properties": len(body.property_policies)})
    return ok({"saved": True},
              data_version=ctx.repo.current_version(case_id))
