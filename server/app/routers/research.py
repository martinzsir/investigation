"""
server/app/routers/research.py
W-P-005 庙算派生视图（hypotheses）/ W-P-006 数据画像（profiles）。

均为只读语义层消费；未 BUILD 案件返回 available:false 空结构，不 500。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.app import hypotheses_view, profiles_view
from server.app.deps import (
    WebContext,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import ERR_CONFLICT, ERR_NOT_FOUND, APIError, ok
from server.app.meta.models import TASK_PENDING, TASK_RUNNING
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_store import StateStore
from server.app.worker.tasks import (
    TASK_DE_DECIDE,
    TASK_DE_RECO,
    enqueue_task,
)

router = APIRouter(tags=["research"])


def _state_map(ctx: WebContext, case_id: str) -> dict[str, dict]:
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    if not path.exists():
        return {}
    from server.app.store.state_store import StateStore
    st = StateStore(case_id, path)
    try:
        return st.status_map()
    finally:
        st.close()


@router.get("/cases/{case_id}/hypotheses")
def get_hypotheses(case_id: str,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """庙算页：覆盖缺口诊断 + 间×级热力 + 未处置候补池（派生口径，不改等级）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            conn = store.read_conn
        except FileNotFoundError:
            conn = None  # 未 BUILD：覆盖诊断为空，候补池仍可读产物
        data = hypotheses_view.assemble_hypotheses(
            case_dir=ctx.factory.case_dir(case_id),
            state_map=_state_map(ctx, case_id),
            role=p.role, conn=conn, pack=case.pack_id,
            base_dir=ctx.cases.snapshot_ontology_root(case_id))
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/profiles")
def get_profiles(case_id: str,
                 focus: str | None = None,
                 anchor_date: str | None = Query(None),
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """数据画像：OntologyProfiler 六层报告（快照感知；样本经敏感遮蔽）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    focus_list = None
    if focus:
        focus_list = [s.strip() for s in focus.split(",") if s.strip()]
    data = {"available": False,
            "note": "尚未接入数据源（语义层未构建，先导入数据并 BUILD）"}
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            data = profiles_view.assemble_profiles(
                conn=store.read_conn, pack=case.pack_id,
                base_dir=ctx.cases.snapshot_ontology_root(case_id),
                focus=focus_list, anchor_date=anchor_date)
        except FileNotFoundError:
            pass
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# W-P-007：数据元智能推荐（待核实/采纳/驳回；推荐永不自动生效）
# ----------------------------------------------------------------------
class DeRecoIn(BaseModel):
    upload_id: str


class DeDecideIn(BaseModel):
    decision: str  # adopt | reject
    note: str = ""


_DECISIONS = ("adopt", "reject")


def _open_state(ctx: WebContext, case_id: str) -> StateStore | None:
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    return StateStore(case_id, path) if path.exists() else None


@router.get("/cases/{case_id}/de-recommendations")
def list_de_recommendations(case_id: str,
                            p: Principal = Depends(get_principal),
                            ctx: WebContext = Depends(get_ctx)):
    """推荐列表（含待核实/采纳/驳回；裁决只记 state，不改 bindings）。"""
    _get_owned_case(case_id, p, ctx.cases)
    st = _open_state(ctx, case_id)
    try:
        items = st.list_de_reco() if st is not None else []
    finally:
        if st is not None:
            st.close()
    names = {s.get("upload_id"): s.get("filename")
             for s in ctx.repo.list_sources(case_id)}
    for it in items:
        it["filename"] = names.get(it.get("upload_id"))
    return ok({"items": items, "total": len(items)},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/de-recommendations", status_code=202)
def create_de_recommendation(case_id: str,
                             body: DeRecoIn,
                             p: Principal = Depends(get_principal),
                             ctx: WebContext = Depends(get_ctx)):
    """对暂存上传件生成数据元推荐（同 upload_id 幂等；进行中 409 回跳）。"""
    _get_owned_case(case_id, p, ctx.cases)
    upload_id = body.upload_id.strip()
    if not upload_id:
        raise APIError("VALIDATION", "缺少 upload_id", 400)
    if ctx.repo.get_source(case_id, upload_id) is None:
        raise APIError(ERR_NOT_FOUND, f"上传件不存在：{upload_id}", 404)

    st = _open_state(ctx, case_id)
    try:
        if st is not None and st.find_de_reco_by_upload(upload_id) is not None:
            return ok({"reused": True, "upload_id": upload_id},
                      data_version=ctx.repo.current_version(case_id))
    finally:
        if st is not None:
            st.close()

    for t in (ctx.repo.list_tasks(case_id=case_id, status=TASK_PENDING)
              + ctx.repo.list_tasks(case_id=case_id, status=TASK_RUNNING)):
        if (t.task_type == TASK_DE_RECO
                and (t.params or {}).get("upload_id") == upload_id):
            raise APIError(ERR_CONFLICT,
                           f"该上传件推荐任务进行中：{t.id}", 409)

    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_DE_RECO,
        params={"upload_id": upload_id, "operator": p.operator},
        idem_key=f"de_reco:{upload_id}:{uuid.uuid4().hex[:8]}",
        created_by=p.operator)
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/de-recommendations/{rid}/decide",
             status_code=202)
def decide_de_recommendation(case_id: str, rid: str, body: DeDecideIn,
                             p: Principal = Depends(get_principal),
                             ctx: WebContext = Depends(get_ctx)):
    """采纳/驳回推荐（只记 state + 审计，永不自动改写 bindings/数据元配置）。"""
    _get_owned_case(case_id, p, ctx.cases)
    decision = body.decision.strip()
    if decision not in _DECISIONS:
        raise APIError("VALIDATION",
                       f"decision 非法：{decision}（adopt|reject）", 400)
    st = _open_state(ctx, case_id)
    try:
        if st is None or st.get_de_reco(rid) is None:
            raise APIError(ERR_NOT_FOUND, f"推荐不存在：{rid}", 404)
    finally:
        if st is not None:
            st.close()
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_DE_DECIDE,
        params={"rid": rid, "decision": decision, "note": body.note,
                "operator": p.operator},
        idem_key=f"de_decide:{rid}:{decision}:{uuid.uuid4().hex[:8]}",
        created_by=p.operator)
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))
