"""
server/app/routers/evidence.py
REQ-V-010：书证上传/下载/清单（同步写通道，不产 DuckDB 版本文件）。

与 uploads/（喂 BUILD 的数据源）物理分离：文件落 cases/<cid>/evidence/，
元数据落 state.sqlite clue_evidence（REQ-V-009）。上传成功同步向
audit_chain 追加 evidence_upload 事件（哈希链 chain_verify 可验）。

红线：
  - 书证是人的行为：operator=agent:* 上传 403（REQ-V-010 验收 4）；
  - 跨租户 404（_get_owned_case）；未登录 401（deps 统一）；
  - 已立案终态线索上传/下载仍允许（补充案卷材料，REQ-V-010 细节）；
  - 下载即「证据外带」：platform_audit 记 operator/role/clearance/purpose
    （REQ-011 导出纪律同语义，web 通道落 meta platform_audit）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import (
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.evidence_store import EvidenceTooLargeError, save_evidence_file
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_sink import StateSink
from server.app.store.state_store import StateStore

router = APIRouter(tags=["evidence"])

# 书证类型（实施方案 §3 clue_evidence.material_type DDL 注释）
MATERIAL_TYPES = ("缴款单", "监控截图", "合同", "付款凭证", "审批文件", "其他")

# 清单投影（剔除 case_id/clue_id 内部列——已由路径限定）
_EVIDENCE_FIELDS = ("material_id", "item_id", "material_type", "filename",
                    "orig_name", "sha256", "size", "note", "uploaded_by",
                    "uploaded_at")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _open_state(ctx: WebContext, case_id: str) -> StateStore:
    """写通道打开（不存在即建）；读面用「无 state.sqlite → 空清单」。"""
    return StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")


@router.post("/cases/{case_id}/clues/{clue_id}/evidence")
async def upload_evidence(case_id: str, clue_id: str,
                          file: UploadFile = File(...),
                          material_type: str = Form("其他"),
                          note: str = Form(""),
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """书证上传（同步，对齐 upload_source 先例）：落文件 + 元数据 + 审计链。"""
    _get_owned_case(case_id, p, ctx.cases)  # 跨租户 404
    if p.operator.startswith("agent:"):
        raise APIError(
            ERR_FORBIDDEN,
            f"Agent 身份 {p.operator!r} 不得上传书证（书证是人的行为，"
            "REQ-V-010 验收 4）", 403)
    mtype = (material_type or "").strip() or "其他"
    if mtype not in MATERIAL_TYPES:
        raise APIError(
            ERR_VALIDATION,
            f"非法书证类型：{mtype!r}（合法：{'/'.join(MATERIAL_TYPES)}）", 400)
    content = await file.read()
    if not content:
        raise APIError(ERR_VALIDATION, "上传文件为空", 400)
    ver = ctx.repo.current_version(case_id)
    try:
        saved = save_evidence_file(
            ctx.factory.case_dir(case_id), (file.filename or "", content))
    except EvidenceTooLargeError as e:
        raise APIError(ERR_VALIDATION, str(e), 413)
    state = _open_state(ctx, case_id)
    try:
        row = state.insert_evidence(
            material_id=saved["material_id"],  # 文件目录与 DB 行必须同一 ID
            case_id=case_id, clue_id=clue_id, material_type=mtype,
            filename=saved["filename"], orig_name=saved["orig_name"],
            sha256=saved["sha256"], size=saved["size"],
            uploaded_by=p.operator, uploaded_at=_now(),
            note=(note or "").strip())
        # 同步落审计链（StateSink 窄协议；chain_verify 可验）
        StateSink(state, ontology_version=f"v{ver}").audit_append({
            "event": "evidence_upload",
            "clue_id": clue_id,
            "material_id": row["material_id"],
            "material_type": mtype,
            "orig_name": saved["orig_name"],
            "sha256": saved["sha256"],
            "size": saved["size"],
            "operator": p.operator,
            "ontology_version": f"v{ver}",
        })
    finally:
        state.close()
    return ok({k: row[k] for k in
               ("material_id", "filename", "orig_name", "material_type",
                "sha256", "size", "note", "uploaded_by", "uploaded_at")},
              data_version=ver)


@router.get("/cases/{case_id}/evidence")
def list_case_evidence(case_id: str,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """案件级书证清单（全部线索；P8 VLM 选图用，案卷视图复用）。"""
    _get_owned_case(case_id, p, ctx.cases)
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    items: list[dict] = []
    if path.exists():
        state = StateStore(case_id, path)
        try:
            items = [{k: r[k] for k in _EVIDENCE_FIELDS + ("clue_id",)}
                     for r in state.list_case_evidence(case_id)]
        finally:
            state.close()
    return ok({"items": items},
              data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/clues/{clue_id}/evidence")
def list_evidence(case_id: str, clue_id: str,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    """书证清单（元数据，不含文件；uploaded_at 降序）。"""
    _get_owned_case(case_id, p, ctx.cases)
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    items: list[dict] = []
    if path.exists():
        state = StateStore(case_id, path)
        try:
            items = [{k: r[k] for k in _EVIDENCE_FIELDS}
                     for r in state.list_evidence(clue_id)]
        finally:
            state.close()
    return ok({"items": items},
              data_version=ctx.repo.current_version(case_id))


@router.get(
    "/cases/{case_id}/clues/{clue_id}/evidence/{material_id}/download")
def download_evidence(case_id: str, clue_id: str, material_id: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """书证下载（证据外带）：记平台审计一条；流为原文件字节。"""
    _get_owned_case(case_id, p, ctx.cases)
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    if not path.exists():
        raise APIError(ERR_NOT_FOUND, f"书证不存在：{material_id}", 404)
    state = StateStore(case_id, path)
    try:
        row = state.get_evidence(material_id)
    finally:
        state.close()
    if row is None or row["case_id"] != case_id or row["clue_id"] != clue_id:
        # 跨线索取他人案件书证与不存在同态（不枚举）
        raise APIError(ERR_NOT_FOUND, f"书证不存在：{material_id}", 404)
    fpath = (ctx.factory.case_dir(case_id) / "evidence" / material_id
             / row["filename"])
    if not fpath.is_file():
        raise APIError(ERR_NOT_FOUND, f"书证文件缺失：{material_id}", 404)
    ctx.repo.record_platform_event(
        "evidence_download", operator=p.operator, tenant_id=p.tenant_id,
        detail={"case_id": case_id, "clue_id": clue_id,
                "material_id": material_id, "orig_name": row["orig_name"],
                "sha256": row["sha256"], "role": p.role,
                "clearance": p.clearance, "purpose": "证据外带下载"})
    # Content-Disposition 携带 orig_name（非 ASCII 名由 Starlette RFC5987 编码）
    return FileResponse(fpath, media_type="application/octet-stream",
                        filename=row["orig_name"])
