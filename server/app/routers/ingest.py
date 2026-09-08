"""
server/app/routers/ingest.py
W-010 数据源注册与列映射向导 + W-012 导入幂等。

- POST /cases/{cid}/sources/upload：multipart 收五格式（CSV/Excel/
  Parquet/JSON/SQLite）→ 暂存 cases/<cid>/uploads/ → 指纹（文件名+
  sha256+行数）+ 列画像 → 注册表 staged 行；返回向导目标集（快照 bindings
  声明的源表与原始列）。
- POST /cases/{cid}/sources/{uid}/import：指纹幂等判定在入队前完成
  （同指纹 409 拦截、判定阶段无任务行，AC-1/4）；映射过校验后入队
  TASK_IMPORT（Worker 落冷层→bindings 清洗→链式 BUILD）。
- GET /cases/{cid}/sources：注册表列表。

权限：登录即可上传；导入（改检测数据面）需偏将及以上。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File
from pydantic import BaseModel

from server.app import ingest_io, source_analyze
from server.app.deps import WebContext, get_ctx, get_principal, task_dto
from server.app.envelope import (
    ERR_CONFLICT,
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.worker.ingest import declared_source_tables, validate_mapping
from server.app.worker.tasks import TASK_IMPORT, TaskExecError, enqueue_task

router = APIRouter(tags=["ingest"])


class ImportIn(BaseModel):
    target_table: str
    column_map: dict[str, str] = {}
    clean: list[str] = []


class AnalyzeIn(BaseModel):
    target_table: str | None = None


class MappingIn(BaseModel):
    target_table: str
    mapping: dict[str, str] = {}   # {声明列(属性): 上传件源列}
    notes: str | None = None


def _source_dto(r: dict) -> dict:
    try:
        mapping = json.loads(r.get("mapping_json") or "{}")
    except (ValueError, TypeError):
        mapping = {}
    return {
        "upload_id": r["upload_id"], "filename": r["filename"],
        "format": r["fmt"], "fingerprint": r["fingerprint"],
        "rows": r["rows"], "status": r["status"],
        "table_name": r["table_name"], "mapping_json": mapping,
        "created_by": r["created_by"], "created_at": r["created_at"],
    }


def _require_analyst(p: Principal) -> None:
    if p.role not in ("human", "system") and p.clearance < 2:
        raise APIError(ERR_FORBIDDEN,
                       "数据导入需偏将及以上（clearance>=2）", 403)


def _wizard_targets(ctx: WebContext, case_id: str, pack_id: str) -> dict:
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    try:
        tables = declared_source_tables(pack=pack_id, base_dir=base_dir)
    except Exception as e:
        raise APIError(ERR_VALIDATION, f"案件快照装载失败：{e}", 400)
    return tables


@router.post("/cases/{case_id}/sources/upload")
async def upload_source(case_id: str, file: UploadFile = File(...),
                        p: Principal = Depends(get_principal),
                        ctx: WebContext = Depends(get_ctx)):
    """上传暂存 + 指纹 + 列画像（不落冷层、不入队）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    fmt = ingest_io.sniff_format(file.filename or "")
    if fmt is None:
        raise APIError(ERR_VALIDATION,
                       f"不支持的文件类型：{file.filename}（支持 CSV/Excel/"
                       "Parquet/JSON/SQLite）", 400)

    upload_id = f"up_{uuid.uuid4().hex[:12]}"
    stage_dir = ctx.factory.case_dir(case_id) / "uploads"
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged = stage_dir / f"{upload_id}.{fmt}"
    content = await file.read()
    if not content:
        raise APIError(ERR_VALIDATION, "上传文件为空", 400)
    staged.write_bytes(content)

    try:
        df = ingest_io.read_table(staged, fmt)
    except Exception as e:
        staged.unlink(missing_ok=True)
        raise APIError(ERR_VALIDATION, f"文件解析失败：{e}", 400)

    columns = ingest_io.profile_columns(df)
    content_hash = ingest_io.sha256_file(staged)
    fp = ingest_io.fingerprint(file.filename, content_hash, len(df))
    row = ctx.repo.register_source(
        case_id=case_id, upload_id=upload_id,
        filename=file.filename, fmt=fmt, fingerprint=fp,
        rows=len(df), columns=columns, created_by=p.operator)

    targets = _wizard_targets(ctx, case_id, case.pack_id)
    return ok({
        "upload_id": upload_id,
        "filename": file.filename,
        "format": fmt,
        "fingerprint": fp,
        "rows": len(df),
        "columns": columns,
        "declared_tables": targets,
        "status": row["status"],
    }, data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/sources")
def list_sources(case_id: str,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """数据源注册表（staged/imported）。"""
    _get_owned_case(case_id, p, ctx.cases)
    rows = ctx.repo.list_sources(case_id)
    out = [_source_dto(r) for r in rows]
    return ok({"items": out, "total": len(out)},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/sources/{upload_id}/import")
def import_source(case_id: str, upload_id: str, body: ImportIn,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    """映射确认 + 幂等判定 → 入队 TASK_IMPORT（判定阶段无任务行，AC-4）。"""
    _get_owned_case(case_id, p, ctx.cases)
    _require_analyst(p)
    case = ctx.repo.get_case(case_id)

    src = ctx.repo.get_source(case_id, upload_id)
    if src is None:
        raise APIError(ERR_NOT_FOUND, f"上传件不存在：{upload_id}", 404)

    # W-012 AC-1/2/3：指纹幂等判定（同文件名+同内容+同行数=重复；异名/异内容
    # 指纹不同，放行）。同指纹源已入队(queued)或已导入(imported)即拦截
    # ——判定在入队前，拒绝不产任务行（AC-4）。
    dup = ctx.repo.find_source_by_fingerprint(
        case_id, src["fingerprint"], exclude_upload_id=upload_id)
    if (dup is not None and dup["upload_id"] != upload_id
            and dup["status"] in ("queued", "imported")):
        raise APIError(ERR_CONFLICT,
                       f"相同数据源已在导入（upload_id={dup['upload_id']}，"
                       f"指纹={src['fingerprint']}，状态={dup['status']}）", 409)

    # 映射校验（目标表/原始列必须在快照 bindings 声明内）
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    try:
        declared = declared_source_tables(pack=case.pack_id, base_dir=base_dir)
        validate_mapping(body.target_table, body.column_map, declared)
    except TaskExecError as e:
        raise APIError(ERR_VALIDATION, e.message, 400)

    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_IMPORT,
        params={"upload_id": upload_id,
                "target_table": body.target_table,
                "column_map": body.column_map, "clean": body.clean},
        idem_key=f"import:{upload_id}", created_by=p.operator)
    ctx.repo.set_source_status(case_id, upload_id, "queued")
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))


def _staged_file(ctx: WebContext, case_id: str, src: dict) -> Path:
    staged = ctx.factory.case_dir(case_id) / "uploads" / f"{src['upload_id']}.{src['fmt']}"
    if not staged.exists():
        raise APIError(ERR_NOT_FOUND, f"暂存文件已丢失：{staged.name}", 404)
    return staged


@router.post("/cases/{case_id}/sources/{upload_id}/analyze")
def analyze_source(case_id: str, upload_id: str,
                   body: AnalyzeIn | None = None,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """W-P-002 列分析（同步只读计算；不产任务、不写映射、不改状态）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    src = ctx.repo.get_source(case_id, upload_id)
    if src is None:
        raise APIError(ERR_NOT_FOUND, f"上传件不存在：{upload_id}", 404)
    staged = _staged_file(ctx, case_id, src)

    try:
        df = ingest_io.read_table(staged, src["fmt"])
    except Exception as e:
        raise APIError(ERR_VALIDATION, f"文件解析失败：{e}", 400)

    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    try:
        result = source_analyze.analyze_source(
            df=df, pack=case.pack_id, base_dir=base_dir,
            target_table=(body.target_table if body else None))
    except (FileNotFoundError, ValueError) as e:
        raise APIError(ERR_VALIDATION, f"案件快照装载失败：{e}", 400)

    return ok({
        "upload_id": upload_id,
        "filename": src["filename"],
        "format": src["fmt"],
        "row_count": int(src["rows"]),
        "sha256": ingest_io.sha256_file(staged),
        "fingerprint": src["fingerprint"],
        **result,
    }, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/sources/{upload_id}")
def save_source_mapping(case_id: str, upload_id: str, body: MappingIn,
                        p: Principal = Depends(get_principal),
                        ctx: WebContext = Depends(get_ctx)):
    """W-P-003 向导映射草稿保存（仅 staged/queued 可改；导入时回落读取）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    src = ctx.repo.get_source(case_id, upload_id)
    if src is None:
        raise APIError(ERR_NOT_FOUND, f"上传件不存在：{upload_id}", 404)
    if src["status"] not in ("staged", "queued"):
        raise APIError(ERR_CONFLICT,
                       f"数据源状态为 {src['status']}，映射不可修改"
                       "（仅暂存/排队中的数据源可改映射）", 409)

    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    try:
        declared = declared_source_tables(pack=case.pack_id, base_dir=base_dir)
    except Exception as e:
        raise APIError(ERR_VALIDATION, f"案件快照装载失败：{e}", 400)
    if body.target_table not in declared:
        raise APIError(ERR_VALIDATION,
                       f"目标源表 {body.target_table!r} 未在案件快照 bindings 声明，"
                       f"可用 {sorted(declared)}", 400)

    try:
        src_cols = set(json.loads(src.get("columns_json") or "{}").keys())
    except (ValueError, TypeError):
        src_cols = set()
    decl_cols = set(declared[body.target_table])
    bad_targets = sorted({k for k in body.mapping if k not in decl_cols})
    bad_sources = sorted({v for v in body.mapping.values() if v not in src_cols})
    if bad_targets:
        raise APIError(ERR_VALIDATION,
                       f"映射目标列 {bad_targets} 不在表 {body.target_table} "
                       f"声明列 {sorted(decl_cols)} 内", 400)
    if bad_sources:
        raise APIError(ERR_VALIDATION,
                       f"上传件不存在源列 {bad_sources}（可用 {sorted(src_cols)}）",
                       400)

    # 体向转换：body.mapping 为 {声明列: 源列}（向导视角），管线 column_map
    # 为 {源列: 声明列}（rename 视角）；空值忽略。
    column_map = {v: k for k, v in body.mapping.items() if k and v}
    try:
        validate_mapping(body.target_table, column_map, declared)
    except TaskExecError as e:
        raise APIError(ERR_VALIDATION, e.message, 400)

    saved_mapping = {
        "target_table": body.target_table,
        "column_map": column_map,
        "mapping": body.mapping,
        "notes": body.notes or "",
        "updated_by": p.operator,
    }
    row = ctx.repo.save_source_mapping(
        case_id, upload_id, target_table=body.target_table,
        mapping=saved_mapping, by=p.operator)
    ctx.repo.record_ops(
        "source_mapping_save", case_id,
        {"upload_id": upload_id, "table": body.target_table,
         "mapped": len(column_map), "by": p.operator})
    return ok(_source_dto(row), data_version=ctx.repo.current_version(case_id))
