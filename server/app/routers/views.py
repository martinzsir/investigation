"""
server/app/routers/views.py
W-017 角色视图配置（M4 阶段 A）。

读：GET /cases/{cid}/views —— 视图列表。
写：PUT /cases/{cid}/views 🔒 —— 整体替换 views.json；
    引用列必须已声明（AC-5）；视图不绕过 PolicyEngine（AC-4，
    core 视图装载期校验）；物化为 v_<name>，不复制数据（AC-2）。
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

router = APIRouter(tags=["views"])


class ViewsIn(BaseModel):
    views: list[dict]
    reason: str | None = None  # 变更理由（FE-T-012，落审计链 note）


@router.get("/cases/{case_id}/views")
def list_views(case_id: str,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    data = json.loads((snap_dir / "views.json").read_text(encoding="utf-8"))
    return ok({"views": data.get("views", []), "pack": pack_id},
              data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/views")
def save_views(case_id: str, body: ViewsIn,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)

    # AC-5：引用列必须已声明（base_object + properties 属于该对象属性）
    obj_data = json.loads((snap_dir / "objects.json").read_text(
        encoding="utf-8"))
    obj_props = {}
    for o in obj_data.get("objects", []):
        props = set((o.get("properties") or {}).keys())
        props.add(o.get("pk"))  # 代理键也是视图可引用列
        obj_props[o.get("name")] = props
    for v in body.views:
        base = v.get("base_object")
        if base not in obj_props:
            raise APIError(ERR_VALIDATION,
                           f"视图 {v.get('name')} 的 base_object={base!r} "
                           "未声明", 400)
        declared = obj_props[base]
        for col in v.get("properties") or []:
            if col not in declared:
                raise APIError(ERR_VALIDATION,
                               f"视图 {v.get('name')} 引用列 {col!r} "
                               f"不在对象 {base} 已声明属性内", 400)

    path = snap_dir / "views.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["views"] = body.views

    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        atomic_write_json(tmp_root / pack_id / "views.json", data)
        try:
            load_pack(pack_id, base_dir=tmp_root)
        except Exception as e:
            raise APIError(ERR_VALIDATION,
                           f"views.json 校验失败，未落盘：{e}", 400)
    atomic_write_json(path, data)
    ctx.repo.record_ops("views_save", case_id,
                        {"by": p.operator, "count": len(body.views)})
    record_config_audit(ctx, case_id, p, "views_save",
                        filename="views.json", reason=body.reason,
                        summary={"count": len(body.views)})
    return ok({"saved": len(body.views)},
              data_version=ctx.repo.current_version(case_id))
