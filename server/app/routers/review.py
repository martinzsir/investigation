"""
server/app/routers/review.py
W-021 人审队列与实体裁决（M4 阶段 C）。

读：GET /cases/{cid}/review/queue —— needs_review 候选（从 obj_org 实时
    构建 OrganizationResolver），排除已裁决候选；支持 page/page_size 分页。
读：GET /cases/{cid}/review/{rid}/evidence —— 候选双方属性对比、差异行、
    相似度依据（merge_reason + evidence + attributes）。
读：GET /cases/{cid}/review/history —— 已裁决记录（从 state.sqlite 读）。
写：POST /cases/{cid}/review/{rid}/decision 🔒 —— 合并/驳回 + 理由，入队
    REVIEW 任务（落 state.review_decision + 审计链，不产版本）。
红线 AC-5：系统任何模式下不自动合并 needs_review 候选——只有人审触发的
    REVIEW 任务才调用 core.merge_entities。
权限：GET 登录即可；POST 需正兵及以上。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.entity import build_org_table_from_duckdb

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, ERR_NOT_FOUND, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TASK_REVIEW, enqueue_task

router = APIRouter(tags=["review"])

_ORG_COLS = {"name": "raw_name", "legal_rep": "legal_rep"}


class ReviewDecisionIn(BaseModel):
    action: str  # review_merge / review_reject
    reason: str | None = None


def _require_soldier(p: Principal) -> None:
    if p.role not in ("human", "system") and p.clearance < 1:
        raise APIError(ERR_FORBIDDEN,
                       "实体裁决需正兵及以上（clearance>=1）", 403)


def _decided_ids(case_dir) -> set[str]:
    state_path = case_dir / "state.sqlite"
    if not state_path.exists():
        return set()
    state = StateStore("review", state_path)
    try:
        return {d["target_id"] for d in state.list_decisions()
                if d.get("kind") == "entity_review"}
    finally:
        state.close()


def _candidates(case_id: str, ctx: WebContext):
    store = ctx.factory.for_case(case_id, mode="read")
    try:
        resolver = build_org_table_from_duckdb(
            store.read_conn, table="obj_org", cols=_ORG_COLS)
        return resolver
    finally:
        store.close()


@router.get("/cases/{case_id}/review/queue")
def review_queue(case_id: str,
                 page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=200),
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    try:
        resolver = _candidates(case_id, ctx)
        cands = resolver.review_candidates()
    except FileNotFoundError:
        cands = []
    decided = _decided_ids(ctx.factory.case_dir(case_id))
    items_all = [c for c in cands if c.entity_id not in decided]
    total = len(items_all)
    start = (page - 1) * page_size
    page_items = [c.to_dict() for c in items_all[start:start + page_size]]
    return ok({"items": page_items, "total": total,
               "page": page, "page_size": page_size},
              data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/review/{rid}/evidence")
def review_evidence(case_id: str, rid: str,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    try:
        resolver = _candidates(case_id, ctx)
        cands = resolver.review_candidates()
    except FileNotFoundError:
        raise APIError(ERR_NOT_FOUND, "案件尚未 BUILD", 404)
    cand = next((c for c in cands if c.entity_id == rid), None)
    if cand is None:
        raise APIError(ERR_NOT_FOUND, f"候选不存在或已裁决：{rid}", 404)
    d = cand.to_dict()
    # W-021b：per-variant 属性对比行（供前端双栏对比表）
    attributes = resolver.per_variant_attributes(rid)
    return ok({
        "candidate_id": rid,
        "canonical_name": d["canonical_name"],
        "variants": d["variants"],
        "confidence": d["confidence"],
        "merge_reason": d["merge_reason"],
        "evidence": d["evidence"],
        "attributes": attributes,
    }, data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/review/history")
def review_history(case_id: str,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """已裁决记录（从 state.sqlite 读取，按时间降序）。"""
    _get_owned_case(case_id, p, ctx.cases)
    state_path = ctx.factory.case_dir(case_id) / "state.sqlite"
    if not state_path.exists():
        return ok({"items": [], "total": 0},
                  data_version=ctx.repo.current_version(case_id))
    state = StateStore("review", state_path)
    try:
        raw = [d for d in state.list_decisions()
               if d.get("kind") == "entity_review"]
        # 时间降序
        raw.sort(key=lambda d: d.get("decided_at", ""), reverse=True)
        items = []
        for d in raw:
            payload = d.get("payload") or {}
            items.append({
                "candidate_id": d.get("target_id", ""),
                "canonical_name": payload.get("canonical_name", ""),
                "action": "merge" if d.get("verdict") == "merge" else "reject",
                "confidence": payload.get("confidence", 0),
                "operator": d.get("decided_by", ""),
                "reason": payload.get("reason", ""),
                "occurred_at": d.get("decided_at", ""),
            })
        return ok({"items": items, "total": len(items)},
                  data_version=ctx.repo.current_version(case_id))
    finally:
        state.close()


@router.post("/cases/{case_id}/review/{rid}/decision")
def review_decision(case_id: str, rid: str, body: ReviewDecisionIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    _require_soldier(p)
    if body.action not in ("review_merge", "review_reject"):
        raise APIError(ERR_FORBIDDEN,
                       "action 仅可选 review_merge/review_reject", 400)
    if body.action == "review_reject" and not (body.reason or "").strip():
        raise APIError(ERR_FORBIDDEN, "驳回必须给出理由", 400)
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_REVIEW,
        params={"action": body.action, "candidate_id": rid,
                "reason": body.reason or "",
                "operator": p.operator, "role": p.role,
                "clearance": p.clearance},
        idem_key=f"review:{rid}:{body.action}",
        created_by=p.operator)
    return ok({"task_id": task.id, "action": body.action,
               "candidate_id": rid},
              data_version=ctx.repo.current_version(case_id))
