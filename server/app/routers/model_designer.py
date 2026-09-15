"""
server/app/routers/model_designer.py
W-013 对象模型设计器（M4 阶段 A）。

读：GET /cases/{cid}/objects —— 案件快照 objects.json。
写：PUT /cases/{cid}/objects 🔒 —— 整体替换 objects 数组；
    值类型仅 TYPE_SQL 支持集合、kind 仅 entity/event、
    链接端点引用已声明对象、间类仅五间——全部经 load_pack 强校验。
写：GET/PUT /cases/{cid}/links 🔒 —— links.json 同口径。
校验：POST /cases/{cid}/validate —— 整包 load_pack 校验（未知名硬失败）。
权限：GET 登录即可；PUT 需偏将及以上。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.ontology import OBJECT_KINDS, TYPE_NAMES
from core.ontology_loader import load_pack

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.snapshot_config import (
    atomic_write_json,
    copy_layer_dirs,
    record_config_audit,
    require_analyst,
    snapshot_paths,
)

router = APIRouter(tags=["model-designer"])

FIVE_JIAN = ("生间", "反间", "因间", "死间", "内间")
ALLOWED_VALUE_TYPES = set(TYPE_NAMES)


class ObjectsIn(BaseModel):
    objects: list[dict]
    reason: str | None = None  # 变更理由（FE-T-012，落审计链 note）


class LinksIn(BaseModel):
    links: list[dict]
    reason: str | None = None


def _check_object_fields(obj: dict) -> None:
    """W-013 AC-2/3/5：前端预检（loader 兜底）。"""
    name = obj.get("name")
    if not name:
        raise APIError(ERR_VALIDATION, "对象 name 必填", 400)
    kind = obj.get("kind")
    if kind not in OBJECT_KINDS:
        raise APIError(ERR_VALIDATION,
                       f"对象 {name} 的 kind 仅可选 {OBJECT_KINDS}", 400)
    props = obj.get("properties") or {}
    if not isinstance(props, dict):
        raise APIError(ERR_VALIDATION, f"对象 {name} properties 必须是对象",
                       400)
    for prop, vtype in props.items():
        # 属性值可为字符串值类型，或映射 {"type":..., "data_element":...}
        # （与 ontology_loader._load_objects 同口径，REQ-D-013/016）
        if isinstance(vtype, dict):
            base = vtype.get("type")
            if base is not None and base not in ALLOWED_VALUE_TYPES:
                raise APIError(ERR_VALIDATION,
                               f"对象 {name}.{prop} 值类型 {base!r} 不在支持集合 "
                               f"{sorted(ALLOWED_VALUE_TYPES)}", 400)
        elif vtype not in ALLOWED_VALUE_TYPES:
            raise APIError(ERR_VALIDATION,
                           f"对象 {name}.{prop} 值类型 {vtype!r} 不在支持集合 "
                           f"{sorted(ALLOWED_VALUE_TYPES)}", 400)
    jian = obj.get("jian")
    if jian and jian not in FIVE_JIAN:
        raise APIError(ERR_VALIDATION,
                       f"对象 {name} 的间类仅可选 {FIVE_JIAN}", 400)


def _check_link_fields(link: dict, declared_objects: set[str]) -> None:
    name = link.get("name")
    if not name:
        raise APIError(ERR_VALIDATION, "链接 name 必填", 400)
    from_obj = link.get("from_obj")
    to_obj = link.get("to_obj")
    if from_obj not in declared_objects:
        raise APIError(ERR_VALIDATION,
                       f"链接 {name} 的 from_obj={from_obj!r} 未声明", 400)
    if to_obj not in declared_objects:
        raise APIError(ERR_VALIDATION,
                       f"链接 {name} 的 to_obj={to_obj!r} 未声明", 400)
    jian = link.get("jian")
    if jian and jian not in FIVE_JIAN:
        raise APIError(ERR_VALIDATION,
                       f"链接 {name} 的间类仅可选 {FIVE_JIAN}", 400)


def _value_type(v) -> str | None:
    """属性值归一为类型字符串（string 或映射 {type|composite|data_element}）。"""
    if isinstance(v, dict):
        return v.get("type")
    return v


def _diff_objects_level(old: list[dict], new: list[dict]) -> str:
    """S0-3 F4.2 分级：structural（新增/删除对象、改 pk、属性增删或改 type）
    > display（title/描述等展示字段）> none。判据与 PRD §8.2 一致。"""
    old_map = {o.get("name"): o for o in old}
    new_map = {o.get("name"): o for o in new}
    if set(old_map) != set(new_map):
        return "structural"
    for name, o in new_map.items():
        prev = old_map[name]
        if prev.get("pk") != o.get("pk"):
            return "structural"
        prev_props = prev.get("properties") or {}
        new_props = o.get("properties") or {}
        if set(prev_props) != set(new_props):
            return "structural"
        for prop, t in new_props.items():
            if _value_type(prev_props[prop]) != _value_type(t):
                return "structural"
    return "display" if old != new else "none"


def _diff_links_level(old: list[dict], new: list[dict]) -> str:
    """链接分级：新增/删除/改 from_obj|to_obj → structural；其余 → display。"""
    old_map = {l.get("name"): l for l in old}
    new_map = {l.get("name"): l for l in new}
    if set(old_map) != set(new_map):
        return "structural"
    for name, l in new_map.items():
        prev = old_map[name]
        if (prev.get("from_obj"), prev.get("to_obj")) != \
                (l.get("from_obj"), l.get("to_obj")):
            return "structural"
    return "display" if old != new else "none"


def _validate_in_temp(snap_dir: Path, pack_id: str, base_dir: Path,
                      filename: str, data: dict) -> None:
    """临时副本整包过 load_pack，不合法抛异常（不落盘）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        # 复制 _shared + _industry 上游层（数据元三层合并，S0-1）
        copy_layer_dirs(snap_dir, base_dir, tmp_root)
        atomic_write_json(tmp_root / pack_id / filename, data)
        load_pack(pack_id, base_dir=tmp_root)


@router.get("/cases/{case_id}/objects")
def list_objects(case_id: str,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    data = json.loads((snap_dir / "objects.json").read_text(encoding="utf-8"))
    return ok({"objects": data.get("objects", []), "pack": pack_id},
              data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/objects")
def save_objects(case_id: str, body: ObjectsIn,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)

    for obj in body.objects:
        _check_object_fields(obj)
    path = snap_dir / "objects.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    old_objects = data.get("objects", [])
    data["objects"] = body.objects
    change_level = _diff_objects_level(old_objects, body.objects)
    try:
        _validate_in_temp(snap_dir, pack_id, base_dir, "objects.json", data)
    except Exception as e:
        raise APIError(ERR_VALIDATION,
                       f"objects.json 校验失败，未落盘：{e}", 400)
    atomic_write_json(path, data)
    ctx.repo.record_ops("model_objects_save", case_id,
                        {"count": len(body.objects), "by": p.operator})
    record_config_audit(ctx, case_id, p, "model_objects_save",
                        filename="objects.json", reason=body.reason,
                        summary={"count": len(body.objects),
                                 "change_level": change_level})
    return ok({"saved": len(body.objects), "change_level": change_level},
              data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/links")
def list_links(case_id: str,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    data = json.loads((snap_dir / "links.json").read_text(encoding="utf-8"))
    return ok({"links": data.get("links", []), "pack": pack_id},
              data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/links")
def save_links(case_id: str, body: LinksIn,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)

    obj_data = json.loads((snap_dir / "objects.json").read_text(
        encoding="utf-8"))
    declared = {o.get("name") for o in obj_data.get("objects", [])}
    for link in body.links:
        _check_link_fields(link, declared)

    path = snap_dir / "links.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    old_links = data.get("links", [])
    data["links"] = body.links
    change_level = _diff_links_level(old_links, body.links)
    try:
        _validate_in_temp(snap_dir, pack_id, base_dir, "links.json", data)
    except Exception as e:
        raise APIError(ERR_VALIDATION,
                       f"links.json 校验失败，未落盘：{e}", 400)
    atomic_write_json(path, data)
    ctx.repo.record_ops("model_links_save", case_id,
                        {"count": len(body.links), "by": p.operator})
    record_config_audit(ctx, case_id, p, "model_links_save",
                        filename="links.json", reason=body.reason,
                        summary={"count": len(body.links),
                                 "change_level": change_level})
    return ok({"saved": len(body.links), "change_level": change_level},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/validate")
def validate_pack(case_id: str,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    """整包 load_pack 校验（未知名/版本不符/交叉引用错误硬失败）。"""
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)
    try:
        load_pack(pack_id, base_dir=base_dir)
    except Exception as e:
        raise APIError(ERR_VALIDATION, f"包校验失败：{e}", 400)
    return ok({"valid": True, "pack": pack_id},
              data_version=ctx.repo.current_version(case_id))
