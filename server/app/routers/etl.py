"""
server/app/routers/etl.py
W-P-011 数据元（data-elements）/ W-P-012 ETL 管道（etl-pipeline）。

治理配置写面（clearance≥2）：全部走 rule_workshop 同款范式——
新内容先写**临时副本**过 core.ontology_loader.load_pack 全量校验，
不合法 400 且不落盘；合法后 os.replace 原子写案件快照 + ops 审计。

红线四：validate 只返回冲突与两路出路（source_sql 拆分/整列降级），
**绝不返回 force/ignore/continue 字段**，且不写盘。
"""
from __future__ import annotations

import json
import os
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
    require_analyst,
    snapshot_paths,
)

router = APIRouter(tags=["etl"])

_VALIDATE_PATHS = [
    {"key": "A_split_source_sql",
     "label": "上游 source_sql 拆分（复合列定义）"},
    {"key": "B_degrade_column", "label": "整列降级为低可信度"},
]


def _snapshot_file(ctx: WebContext, case_id: str, filename: str) -> Path:
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    return snap_dir / filename


def _write_validated(*, ctx: WebContext, case_id: str, filename: str,
                     data, op: str, p: Principal, detail: dict | None = None):
    """临时副本写新内容 → load_pack 全量校验 → 原子写 → ops 审计。"""
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        (tmp_root / pack_id / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            load_pack(pack_id, base_dir=tmp_root)
        except Exception as e:
            raise APIError(ERR_VALIDATION, f"配置校验失败，未落盘：{e}", 400)
    target = snap_dir / filename
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, target)
    ctx.repo.record_ops(op, case_id,
                        {"file": filename, "by": p.operator, **(detail or {})})


# ----------------------------------------------------------------------
# W-P-011 数据元
# ----------------------------------------------------------------------
@router.get("/cases/{case_id}/data-elements")
def get_data_elements(case_id: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """案件快照 data_elements.json 直出（分类树前端构建）。"""
    _get_owned_case(case_id, p, ctx.cases)
    path = _snapshot_file(ctx, case_id, "data_elements.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/data-elements")
def put_data_elements(case_id: str, body: dict,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """全量更新数据元（clearance≥2；loader 校验失败 400 不落盘）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    _write_validated(ctx=ctx, case_id=case_id, filename="data_elements.json",
                     data=body, op="data_elements_edit", p=p)
    return ok({"updated": True},
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# W-P-012 ETL 管道
# ----------------------------------------------------------------------
def _pipeline_from_bindings(bindings: dict) -> list[dict]:
    """bindings.json object_bindings → ETL 管道视图（只派生可编辑字段）。"""
    sources: list[dict] = []
    for b in bindings.get("object_bindings") or []:
        tbl = b.get("source_table")
        if not tbl and isinstance(b.get("source"), dict):
            tbl = b["source"].get("table")
        key = b.get("key") or {}
        sources.append({
            "source_table": tbl,
            "object": b.get("object"),
            "clean": list(b.get("clean") or []),
            "on_cast_error": dict(b.get("on_cast_error") or {}),
            "null_policy": dict(b.get("null_policy") or {}),
            "dedup_key": list(key.get("columns") or []),
            "dedup_on_conflict": key.get("on_conflict", "keep_latest"),
            # 复合列无声明位置（诊断由 quality 页 composite_column_detected
            # 提示）；处置路径见 validate 的 A/B 两路出路。
            "composite_props": [],
        })
    return sources


class ValidateIn(BaseModel):
    target_table: str
    mapping: dict[str, str]  # 属性 → 源列


@router.get("/cases/{case_id}/etl-pipeline")
def get_etl_pipeline(case_id: str,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """从快照 bindings.json 派生 ETL 管道（清洗/CAST/空值/去重策略）。"""
    _get_owned_case(case_id, p, ctx.cases)
    path = _snapshot_file(ctx, case_id, "bindings.json")
    bindings = json.loads(path.read_text(encoding="utf-8"))
    return ok({"sources": _pipeline_from_bindings(bindings)},
              data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/etl-pipeline")
def put_etl_pipeline(case_id: str, body: dict,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """回写 ETL 策略到 bindings.json（clearance≥2；整包 loader 校验后落盘）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    incoming = body.get("sources") if isinstance(body, dict) else None
    if not isinstance(incoming, list):
        raise APIError(ERR_VALIDATION, "body 需为 {sources: [...]}", 400)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    bp = snap_dir / "bindings.json"
    bindings = json.loads(bp.read_text(encoding="utf-8"))
    by_object = {s.get("object"): s for s in incoming}
    changed: list[str] = []
    for b in bindings.get("object_bindings") or []:
        src = by_object.get(b.get("object"))
        if src is None:
            continue
        b["clean"] = list(src.get("clean") or [])
        oce = src.get("on_cast_error") or {}
        np_ = src.get("null_policy") or {}
        if oce:
            b["on_cast_error"] = dict(oce)
        else:
            b.pop("on_cast_error", None)
        if np_:
            b["null_policy"] = dict(np_)
        else:
            b.pop("null_policy", None)
        dedup = src.get("dedup_key") or []
        if dedup:
            b["key"] = {"columns": list(dedup),
                        "on_conflict": src.get("dedup_on_conflict",
                                               "keep_latest")}
        else:
            b.pop("key", None)
        changed.append(b.get("object"))
    _write_validated(ctx=ctx, case_id=case_id, filename="bindings.json",
                     data=bindings, op="etl_pipeline_edit", p=p,
                     detail={"objects": changed})
    return ok({"sources": _pipeline_from_bindings(bindings)},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/etl-pipeline/validate")
def validate_etl_mapping(case_id: str, body: ValidateIn,
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """映射预检（不写盘）：1:1 冲突/缺列/未知属性 + 两路出路（无 force/ignore）。"""
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, _, base_dir = snapshot_paths(ctx, case_id)
    spec = load_pack(pack_id, base_dir=base_dir)

    # 结构化源表 → {别名(属性): 源列}
    type_by_name = {o.name: o for o in spec.objects}
    declared: dict[str, dict[str, str]] = {}
    obj_props: dict[str, set] = {}
    for otype, b in spec.object_bindings.items():
        if not b.source_table:
            continue
        cols = {alias: raw for alias, raw, _t
                in (getattr(b, "projections", ()) or ())}
        if cols:
            declared[b.source_table] = cols
            if otype in type_by_name:
                obj_props[otype] = set(type_by_name[otype].properties)

    table = body.target_table.strip()
    if table not in declared:
        raise APIError(ERR_VALIDATION,
                       f"目标源表 {table!r} 未在快照 bindings 声明，"
                       f"可用 {sorted(declared)}", 400)
    alias_to_raw = declared[table]
    allowed_props = set().union(*obj_props.values()) if obj_props else set()
    # 该表对应对象的属性集（projections 别名即属性）
    table_props = set(alias_to_raw)

    conflicts: list[dict] = []
    # 1:1 冲突：同一源列映射到两个属性
    by_source: dict[str, list[str]] = {}
    for prop, src_col in body.mapping.items():
        by_source.setdefault(str(src_col), []).append(str(prop))
    for src_col, props in by_source.items():
        if len(props) > 1:
            conflicts.append({
                "type": "one_to_one", "source_col": src_col,
                "target_a": props[0], "target_b": props[1],
                "message": f"源列 {src_col} 被同时映射到 {props[0]} 与 "
                           f"{props[1]}（一列只能对一个属性）",
            })
    for prop, src_col in body.mapping.items():
        prop, src_col = str(prop), str(src_col)
        if prop not in allowed_props:
            conflicts.append({
                "type": "unknown_prop", "target_prop": prop,
                "source_col": src_col,
                "message": f"目标属性 {prop} 不在对象声明属性内",
            })
        elif src_col not in alias_to_raw.values() and src_col:
            conflicts.append({
                "type": "missing_column", "target_prop": prop,
                "source_col": src_col,
                "message": f"上传件列 {src_col} 不在表 {table} 声明源列"
                           f" {sorted(set(alias_to_raw.values()))} 内",
            })
    return ok({"valid": not conflicts, "conflicts": conflicts,
               "paths": _VALIDATE_PATHS},
              data_version=ctx.repo.current_version(case_id))
