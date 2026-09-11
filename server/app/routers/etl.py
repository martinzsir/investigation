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
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core import clean_ops
from core.ontology_loader import load_pack

from server.app import ingest_io
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_NOT_FOUND, ERR_VALIDATION, ERR_CONFLICT, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.snapshot_config import (
    record_config_audit,
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
                     data, op: str, p: Principal, detail: dict | None = None,
                     reason: str | None = None):
    """临时副本写新内容 → load_pack 全量校验 → 原子写 → ops + 审计链留痕。"""
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
    record_config_audit(ctx, case_id, p, op, filename=filename,
                        reason=reason, summary={"file": filename,
                                                **(detail or {})})


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
    # reason 为审计链留痕字段，不落 data_elements.json（写盘前剥离）
    data = dict(body) if isinstance(body, dict) else {}
    reason = data.pop("reason", None)
    _write_validated(ctx=ctx, case_id=case_id, filename="data_elements.json",
                     data=data, op="data_elements_edit", p=p, reason=reason)
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
            # P3-2：binding 级 split 声明直出（只读展示，不在此编辑）
            "split": list(b.get("split") or []),
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
                     detail={"objects": changed},
                     reason=body.get("reason") if isinstance(body, dict)
                     else None)
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


# ----------------------------------------------------------------------
# P3-3: ETL 清洗预演（不写盘、不创草稿；仅对样本展示 before/after）
# ----------------------------------------------------------------------
class PreviewIn(BaseModel):
    upload_id: str
    source_col: str
    op_token: str          # e.g. "trim_prefix:ID-" / "strip_thousands"
    sqlite_table: str = ""


@router.post("/cases/{case_id}/etl-pipeline/preview")
def preview_etl_op(case_id: str, body: PreviewIn,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """ETL 清洗预演：对上传件样本值应用 clean op，返回 before/after 对。

    只支持 py clean 层 op（无 SQL 注入风险）；transform 层预演需 DuckDB
    编译，属后续批次。不写盘、不创建草稿——仅展示效果供分析师决策。
    """
    _get_owned_case(case_id, p, ctx.cases)
    src = ctx.repo.get_source(case_id, body.upload_id)
    if src is None:
        raise APIError(ERR_NOT_FOUND,
                       f"上传件不存在：{body.upload_id}", 404)
    staged = (ctx.factory.case_dir(case_id) / "uploads"
              / f"{src['upload_id']}.{src['fmt']}")
    if not staged.exists():
        raise APIError(ERR_NOT_FOUND, f"暂存文件已丢失：{staged.name}", 404)
    try:
        df = ingest_io.read_table(
            staged, src["fmt"],
            table=body.sqlite_table or None)
    except Exception as e:
        raise APIError(ERR_VALIDATION, f"文件解析失败：{e}", 400)
    if body.source_col not in df.columns:
        raise APIError(ERR_VALIDATION,
                       f"源列 {body.source_col!r} 不在上传件列 "
                       f"{list(df.columns)}", 400)
    # 校验 op token（clean 层；未知 op / 层不匹配硬失败）
    try:
        name, param = clean_ops.validate_op(body.op_token, "clean")
    except ValueError as e:
        raise APIError(ERR_VALIDATION, f"op 校验失败：{e}", 400)
    spec = clean_ops.OPS.get(name)
    if not spec or not callable(spec.fn):
        raise APIError(ERR_VALIDATION,
                       f"op {name!r} 无 py 实现，不能预演", 400)
    # 取样本值（前 10 个非空值）
    series = df[body.source_col].astype(str)
    samples = [v for v in series.tolist() if v and v.strip()][:10]
    # 应用 clean op（paramless op 不接受 param 关键字，仅带参 op 传递）
    clean_ctx = clean_ops.CleanContext()
    call_kwargs = {"param": param} if param is not None else {}
    pairs: list[dict] = []
    for v in samples:
        result = spec.fn(v, clean_ctx, **call_kwargs)
        # reject_if 返回 (value, False) tuple
        if isinstance(result, tuple) and len(result) == 2:
            after, keep = result
            pairs.append({"before": v, "after": str(after),
                          "rejected": not keep})
        else:
            pairs.append({"before": v, "after": str(result),
                          "rejected": False})
    # 全量非空行统计 affected_rows（IN-TC-21）
    non_empty_all = [v for v in series.tolist() if v and v.strip()]
    affected_rows = 0
    for v in non_empty_all:
        result = spec.fn(v, clean_ctx, **call_kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            after, keep = result
            if not keep or str(after) != v:
                affected_rows += 1
        elif str(result) != v:
            affected_rows += 1
    return ok({"op": body.op_token, "source_col": body.source_col,
               "samples": pairs, "total_rows": len(df),
               "affected_rows": affected_rows},
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# P4: ETL 处置草稿（etl_fix_draft；与 de_recommendation 分表）
# 红线：publish 只改 state 状态，不写 bindings.clean/source_sql（后续批次）
# ----------------------------------------------------------------------

def _open_etl_state(ctx: WebContext, case_id: str):
    """复用 research.py _open_state 模式；state.sqlite 缺失返 None。"""
    from server.app.store.state_store import StateStore
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    return StateStore(case_id, path) if path.exists() else None


class EtlDraftIn(BaseModel):
    upload_id: str
    target_object: str
    target_prop: str
    op_token: str
    op_class: str
    preview_affected_rows: int = 0
    preview_samples: list[dict] = []
    note: str = ""


class EtlDraftDecideIn(BaseModel):
    note: str = ""


@router.post("/cases/{case_id}/etl-drafts")
def create_etl_draft(case_id: str, body: EtlDraftIn,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """创建 ETL 处置草稿（A/B 类；C 类不调此端点）。

    幂等：save_etl_fix_draft 用 INSERT OR REPLACE，draft_id 由后端生成。
    op 校验：clean 层未知 op 硬失败（防注入），与 preview 端点同口径。
    """
    _get_owned_case(case_id, p, ctx.cases)
    if body.op_class not in ("A", "B"):
        raise APIError(ERR_VALIDATION,
                       f"op_class 非法：{body.op_class}（A|B；C 类不落表）", 400)
    if ctx.repo.get_source(case_id, body.upload_id) is None:
        raise APIError(ERR_NOT_FOUND, f"上传件不存在：{body.upload_id}", 404)
    try:
        clean_ops.validate_op(body.op_token, "clean")
    except ValueError as e:
        raise APIError(ERR_VALIDATION, f"op 校验失败：{e}", 400)
    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    st = _open_etl_state(ctx, case_id)
    try:
        if st is None:
            raise APIError(ERR_NOT_FOUND,
                           f"案件 state.sqlite 不存在：{case_id}", 404)
        st.save_etl_fix_draft(
            draft_id=draft_id, case_id=case_id, upload_id=body.upload_id,
            target_object=body.target_object, target_prop=body.target_prop,
            op_token=body.op_token, op_class=body.op_class,
            source="Step2",
            preview_affected_rows=body.preview_affected_rows,
            preview_samples=body.preview_samples,
            created_at=now, created_by=p.operator, note=body.note)
        draft = st.get_etl_fix_draft(draft_id)
    finally:
        if st is not None:
            st.close()
    return ok(draft, data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/etl-drafts")
def list_etl_drafts(case_id: str,
                    upload_id: str | None = None,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """草稿列表（可按 upload_id 过滤；IN-TC-18 与 de_recommendation 分表）。"""
    _get_owned_case(case_id, p, ctx.cases)
    st = _open_etl_state(ctx, case_id)
    try:
        items = (st.list_etl_fix_drafts(
            case_id=case_id, upload_id=upload_id)
            if st is not None else [])
    finally:
        if st is not None:
            st.close()
    return ok({"items": items, "total": len(items)},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/etl-drafts/{draft_id}/confirm")
def confirm_etl_draft(case_id: str, draft_id: str,
                      body: EtlDraftDecideIn,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """复核通过：待复核 → 已确认（A 类 created_by 可自审；B 类需独立 reviewer）。

    IN-TC-19：A 类 reviewed_by 允许空，故 A 类前端用 created_by 自审通过；
    IN-TC-20：B 类强制 reviewed_by（在 publish 端点 fail-closed 拦截）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    st = _open_etl_state(ctx, case_id)
    try:
        if st is None or st.get_etl_fix_draft(draft_id) is None:
            raise APIError(ERR_NOT_FOUND, f"草稿不存在：{draft_id}", 404)
        draft = st.confirm_etl_fix_draft(
            draft_id, reviewed_by=p.operator, reviewed_at=now)
        if draft is None:
            raise APIError(ERR_CONFLICT,
                           f"草稿状态非待复核，无法确认：{draft_id}", 409)
    finally:
        if st is not None:
            st.close()
    return ok(draft, data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/etl-drafts/{draft_id}/reject")
def reject_etl_draft(case_id: str, draft_id: str,
                     body: EtlDraftDecideIn,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """复核驳回：待复核 → 已驳回（不可再发布）。"""
    _get_owned_case(case_id, p, ctx.cases)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    st = _open_etl_state(ctx, case_id)
    try:
        if st is None or st.get_etl_fix_draft(draft_id) is None:
            raise APIError(ERR_NOT_FOUND, f"草稿不存在：{draft_id}", 404)
        draft = st.reject_etl_fix_draft(
            draft_id, reviewed_by=p.operator, reviewed_at=now,
            note=body.note)
        if draft is None:
            raise APIError(ERR_CONFLICT,
                           f"草稿状态非待复核，无法驳回：{draft_id}", 409)
    finally:
        if st is not None:
            st.close()
    return ok(draft, data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/etl-drafts/{draft_id}/publish")
def publish_etl_draft(case_id: str, draft_id: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """发布草稿：已确认 → 已发布（fail-closed：未确认/已驳回一律拒绝）。

    IN-TC-22：state_store.publish_etl_fix_draft 硬断言 status=已确认
    IN-TC-20：op_class != A 且 reviewed_by 空 → ValueError
    红线：本端点只改 state 状态；写 bindings.clean/source_sql 属后续批次
    （保持单写口红线，state_store 不触 bindings）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    st = _open_etl_state(ctx, case_id)
    try:
        if st is None or st.get_etl_fix_draft(draft_id) is None:
            raise APIError(ERR_NOT_FOUND, f"草稿不存在：{draft_id}", 404)
        try:
            draft = st.publish_etl_fix_draft(
                draft_id, reviewed_by=p.operator, reviewed_at=now)
        except ValueError as e:
            raise APIError(ERR_CONFLICT, str(e), 409)
    finally:
        if st is not None:
            st.close()
    return ok(draft, data_version=ctx.repo.current_version(case_id))
