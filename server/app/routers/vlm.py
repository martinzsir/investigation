"""
server/app/routers/vlm.py
P8 多模态图像研判（VLM 草案通道的 Web 侧；独立开关）。

三条端点（全部人显式触发，不自动发起）：
  POST /cases/{cid}/vlm/draft              对已上传书证图像发起 VLM 分析，
       成功只产 image_draft 提案（模型直入生产=0）；
  GET  /cases/{cid}/vlm/drafts             列草案 + 派生 stale（TTL）；
  POST /cases/{cid}/vlm/drafts/{pid}/verify  人比对原件核验通过：
       写 state.image_evidence（自动入图/入报告）+ 提案 approve；
       stale 草案拒绝升格（须重新分析）。

红线：
  - Agent 身份不得发起/核验（图像研判是人验闭环）；
  - 降级/阻断/超时一律 200 {ok:false,...}（与 draft_verify 端点同口径），
    前端按档位渲染，不当 5xx；
  - image_loader 只读 cases/<cid>/evidence/（越界 fail-closed）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.access import AccessContext
from core.llm.draft_image import (
    draft_image_inspect,
    is_draft_stale,
    list_image_drafts,
)
from core.proposal import ProposalStore, ProposalValidationError

from server.app.deps import WebContext, get_ctx, get_principal
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
from server.app.store.backend import open_local_conn
from server.app.store.state_store import StateStore

router = APIRouter(tags=["vlm"])

# 与 image_guard 安全类别对齐（face/id_scan 在闸门阻断，API 侧快速失败）
_CONTENT_CLASSES = ("invoice", "receipt", "document", "other")
_SUBJECT_TYPES = ("person", "org", "bid_project")


class VlmDraftIn(BaseModel):
    image_uri: str
    content_class: str
    instruction: str = ""
    prompt_version: str | None = None
    subject_type: str = ""
    subject_id: str = ""


class VlmVerifyIn(BaseModel):
    verify_conclusion: str
    subject_type: str = ""     # 可覆盖提案值
    subject_id: str = ""
    clue_id: str = ""          # 可空（证据归属线索）


def _make_image_loader(case_dir: Path, state: StateStore):
    """只读 cases/<cid>/evidence/ 的图像加载器（越界/缺失 → fail-closed）。

    image_uri 两种形态：
      1. evidence/<material_id>/<filename>（案件目录相对路径）；
      2. <material_id>（直接给书证 ID，filename 由 state 解析）。
    """
    ev_root = (case_dir / "evidence").resolve()

    def _load(uri: str) -> bytes:
        rel = str(uri or "").strip().lstrip("/")
        target = (case_dir / rel).resolve()
        if ev_root not in target.parents:
            row = state.get_evidence(rel)
            if row is None:
                raise FileNotFoundError(f"image_uri 不可解析：{uri}")
            target = (ev_root / row["material_id"]
                      / row["filename"]).resolve()
        if ev_root not in target.parents or not target.is_file():
            raise FileNotFoundError(
                f"image_uri 不在 evidence 目录或非文件：{uri}")
        return target.read_bytes()

    return _load


@router.post("/cases/{case_id}/vlm/draft")
def create_vlm_draft(case_id: str, body: VlmDraftIn,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """发起 VLM 图像分析草案（同步）；成功只落 image_draft 提案。"""
    _get_owned_case(case_id, p, ctx.cases)
    if p.operator.startswith("agent:"):
        raise APIError(ERR_FORBIDDEN,
                       "Agent 身份不得发起图像研判（人验闭环）", 403)
    image_uri = (body.image_uri or "").strip()
    content_class = (body.content_class or "").strip()
    if not image_uri:
        raise APIError(ERR_VALIDATION, "image_uri 不得为空", 400)
    if content_class not in _CONTENT_CLASSES:
        raise APIError(
            ERR_VALIDATION,
            f"content_class={content_class!r} 非法（合法："
            f"{'/'.join(_CONTENT_CLASSES)}；人脸/证件类禁止出网）", 400)
    subject_type = (body.subject_type or "").strip()
    if subject_type and subject_type not in _SUBJECT_TYPES:
        raise APIError(
            ERR_VALIDATION,
            f"subject_type={subject_type!r} 非法（合法："
            f"{'/'.join(_SUBJECT_TYPES)}）", 400)

    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    case_dir = ctx.factory.case_dir(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    access = AccessContext(
        operator=p.operator, role=p.role, clearance=p.clearance,
        case_id=case_id, purpose="vlm_draft", network="web")

    state = StateStore(case_id, case_dir / "state.sqlite")
    conn = open_local_conn(ctx.proposals_db)
    try:
        result = draft_image_inspect(
            conn, access, case_id=case_id, image_uri=image_uri,
            content_class=content_class,
            instruction=(body.instruction or "").strip(),
            prompt_version=body.prompt_version,
            subject_type=subject_type,
            subject_id=(body.subject_id or "").strip(),
            pack=case.pack_id, base_dir=base_dir,
            image_loader=_make_image_loader(case_dir, state))
    finally:
        conn.close()
        state.close()
    return ok(result, data_version=version)


@router.get("/cases/{case_id}/vlm/drafts")
def get_vlm_drafts(case_id: str,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """列 VLM 草案（含 TTL 派生 stale；不物理改状态）。"""
    _get_owned_case(case_id, p, ctx.cases)
    conn = open_local_conn(ctx.proposals_db)
    try:
        drafts = list_image_drafts(conn, case_id=case_id)
    finally:
        conn.close()
    return ok({"drafts": drafts},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/vlm/drafts/{proposal_id}/verify")
def verify_vlm_draft(case_id: str, proposal_id: str, body: VlmVerifyIn,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """人验通过：写 image_evidence（入图/入报告）+ 提案 approve。

    stale/非 draft/跨案件 → 拒绝升格；核验须具名自然人。
    """
    _get_owned_case(case_id, p, ctx.cases)
    if p.operator.startswith("agent:"):
        raise APIError(ERR_FORBIDDEN,
                       "Agent 身份不得核验图像草案（核验是人的判断）", 403)
    verify_conclusion = (body.verify_conclusion or "").strip()
    if not verify_conclusion:
        raise APIError(ERR_VALIDATION,
                       "verify_conclusion 不得为空（人验须留结论，可审计）",
                       400)

    case_dir = ctx.factory.case_dir(case_id)
    conn = open_local_conn(ctx.proposals_db)
    state = StateStore(case_id, case_dir / "state.sqlite")
    try:
        ps = ProposalStore(conn)
        rec = ps.get(proposal_id)
        if (rec is None or rec["case_id"] != case_id
                or rec["kind"] != "image_draft"):
            raise APIError(ERR_NOT_FOUND,
                           f"图像草案不存在：{proposal_id}", 404)
        if rec["status"] != "draft":
            raise APIError(
                ERR_CONFLICT,
                f"草案当前状态 {rec['status']}，不可重复核验", 409)
        if is_draft_stale(rec["payload"]):
            raise APIError(
                ERR_CONFLICT,
                "草案已过 TTL（stale）：不得人验升格，请重新发起分析",
                409)
        inp = rec["payload"].get("input") or {}
        cand = rec["payload"].get("candidate") or {}
        subject_type = (body.subject_type or "").strip() or str(
            inp.get("subject_type") or "").strip()
        subject_id = (body.subject_id or "").strip() or str(
            inp.get("subject_id") or "").strip()
        if subject_type not in _SUBJECT_TYPES or not subject_id:
            raise APIError(
                ERR_VALIDATION,
                "核验须有合法主体（subject_type + subject_id）："
                f"收到 {subject_type!r}/{subject_id!r}", 400)
        model_score = (rec["payload"].get("_sort_hint") or {}).get(
            "model_score")
        image_row = state.insert_image_evidence(
            case_id=case_id, clue_id=(body.clue_id or "").strip(),
            image_uri=str(inp.get("image_uri") or ""),
            model=str(inp.get("model") or ""),
            prompt_version=str(inp.get("prompt_version") or ""),
            model_score=model_score,
            title=str(cand.get("title") or ""),
            detail=str(cand.get("detail") or ""),
            severity=str(cand.get("severity") or ""),
            verifier=p.operator,
            verify_conclusion=verify_conclusion,
            subject_type=subject_type, subject_id=subject_id,
            draft_id=proposal_id)
        try:
            decided = ps.decide(proposal_id, "approve",
                                operator=p.operator,
                                reason=verify_conclusion)
        except ProposalValidationError as e:
            raise APIError(ERR_CONFLICT, str(e), 409)
    finally:
        state.close()
        conn.close()
    return ok({
        "proposal_id": proposal_id,
        "status": decided["status"],
        "image_evidence": {
            k: image_row[k] for k in (
                "image_evidence_id", "image_uri", "model",
                "prompt_version", "model_score", "title", "detail",
                "severity", "verifier",
                "verify_conclusion", "subject_type", "subject_id",
                "clue_id", "created_at")},
    }, data_version=ctx.repo.current_version(case_id))


# ======================================================================
# 书证材料卡图像 findings（按 material_id 分组：待核草案 + 已人验证据）
# ======================================================================
def _material_id_of_uri(uri: str, material_ids: set[str]) -> str | None:
    """image_uri 反解 material_id（evidence/<mid>/<file> 或裸 <mid>）。"""
    u = str(uri or "").strip().lstrip("/")
    if u in material_ids:
        return u
    parts = u.split("/")
    if len(parts) >= 3 and parts[0] == "evidence" and parts[1] in material_ids:
        return parts[1]
    return None


def _group_findings(materials: list[dict], drafts: list[dict],
                    verified: list[dict]) -> dict[str, dict]:
    """按 material_id 分组图像 findings（书证材料卡子列表数据源）。

    线索归属由材料决定：材料清单是哪个线索的，findings 就挂哪些材料——
    finding 自身 clue_id 只是图谱/报告归组辅助，不参与此处过滤。
    """
    mids = {str(m["material_id"]) for m in materials}
    findings: dict[str, dict] = {
        mid: {"pending": [], "verified": []} for mid in mids}
    for d in drafts:
        if d.get("status") != "draft":
            continue
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        inp = payload.get("input") or {}
        cand = payload.get("candidate") or {}
        hint = payload.get("_sort_hint") or {}
        mid = _material_id_of_uri(str(inp.get("image_uri") or ""), mids)
        if mid is None:
            continue
        findings[mid]["pending"].append({
            "proposal_id": d.get("proposal_id"),
            "title": str(cand.get("title") or ""),
            "detail": str(cand.get("detail") or ""),
            "severity": str(cand.get("severity") or "info"),
            "image_uri": str(inp.get("image_uri") or ""),
            "model": str(inp.get("model") or ""),
            "model_score": hint.get("model_score"),
            "stale": bool(d.get("stale")),
            "created_at": d.get("created_at") or "",
        })
    for r in verified:
        mid = _material_id_of_uri(str(r.get("image_uri") or ""), mids)
        if mid is None:
            continue
        findings[mid]["verified"].append({
            "image_evidence_id": r.get("image_evidence_id"),
            "title": str(r.get("title") or ""),
            "detail": str(r.get("detail") or ""),
            "severity": str(r.get("severity") or "info"),
            "image_uri": r.get("image_uri") or "",
            "model": r.get("model") or "",
            "model_score": r.get("model_score"),
            "verifier": r.get("verifier") or "",
            "verify_conclusion": r.get("verify_conclusion") or "",
            "subject_type": r.get("subject_type") or "",
            "subject_id": r.get("subject_id") or "",
            "clue_id": r.get("clue_id") or "",
            "created_at": r.get("created_at") or "",
        })
    return findings


@router.get("/cases/{case_id}/vlm/findings")
def get_vlm_findings(case_id: str, clue_id: str,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """书证材料卡图像 findings：按 material_id 分组（待核草案 + 已人验）。

    归属键 = image_uri 反解 material_id（与 _make_image_loader 同口径）；
    线索归属由材料决定（clue_id 参数取该线索书证清单）；
    无 state.sqlite → 空分组（读面不建库，与书证清单同口径）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    state_path = ctx.factory.case_dir(case_id) / "state.sqlite"
    materials: list[dict] = []
    verified: list[dict] = []
    if state_path.exists():
        state = StateStore(case_id, state_path)
        try:
            materials = state.list_evidence(clue_id=clue_id)
            verified = state.list_image_evidence(case_id=case_id)
        finally:
            state.close()
    conn = open_local_conn(ctx.proposals_db)
    try:
        drafts = list_image_drafts(conn, case_id=case_id, status="draft")
    finally:
        conn.close()
    return ok({"findings": _group_findings(materials, drafts, verified)},
              data_version=ctx.repo.current_version(case_id))
