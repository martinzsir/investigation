"""
server/app/routers/research.py
W-P-005 庙算派生视图（hypotheses）/ W-P-006 数据画像（profiles）。

均为只读语义层消费；未 BUILD 案件返回 available:false 空结构，不 500。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.app import hypotheses_store, hypotheses_view, profiles_view
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


# ----------------------------------------------------------------------
# 采样预演（P3）
# ----------------------------------------------------------------------
# core/sampling.py 模块完整可用（1% 采样验证假设方向，避免盲投全量算力），
# 但此前仅 run_all.py 调用，web 端零入口。此处补只读预演接口。
# 红线：只给方向**建议**，是否投全量由正兵拍板（不自动触发全量扫描）。

class SamplingIn(BaseModel):
    hypothesis_ids: list[str] = []
    sample_ratio: float = 0.01


@router.post("/cases/{case_id}/sampling/preflight")
def sampling_preflight(case_id: str, body: SamplingIn,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """采样预演：对指定假设跑小样本，给出方向判定（明确/存疑/否定）。

    判定阈值：命中率 ≥5% 方向明确；1%~5% 存疑（建议扩大到 5% 再验）；
    <1% 方向否定（建议调整假设，不投全量）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    ratio = body.sample_ratio
    if not (0 < ratio <= 1):
        raise APIError(ERR_VALIDATION, "sample_ratio 必须在 (0, 1] 区间", 400)
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
        except FileNotFoundError:
            raise APIError(ERR_NOT_FOUND, "案件未 BUILD，无数据可采样", 404)
        from core.sampling import SamplingPreflight
        pre = SamplingPreflight(store, sample_ratio=ratio).run(
            body.hypothesis_ids)
        return ok({
            # 红线条注：预演只给方向建议，不自动触发全量
            "advisory_only": True,
            "note": "采样预演只给方向建议，是否投全量由正兵拍板",
            **pre,
        }, data_version=ctx.repo.current_version(case_id))
    finally:
        if store is not None:
            store.close()


# ----------------------------------------------------------------------
# 人工假设 CRUD（P1）
# ----------------------------------------------------------------------
# 只持久化**人工部分**：自动假设从 findings/rules 派生（每次 BUILD 重算），
# 落盘会造成产物与数据漂移；人工假设是正兵判断，不该被重扫冲掉。
# 落盘 cases/<cid>/hypotheses.json（与 lenses.json 同级），不进本体指纹。

class HypothesisIn(BaseModel):
    """人工假设入参：description + falsification 必填（庙算招牌是自动证伪）。"""
    description: str
    falsification: str
    evidence_needed: list[str] = []
    data_sources: list[str] = []
    procedure: str = ""
    dimension: list[str] = []
    jian_types: list[str] = []


class ReorderIn(BaseModel):
    order: list[str]


def _manual_payload(ctx: WebContext, case_id: str) -> dict:
    return hypotheses_store.load_manual(ctx.factory.case_dir(case_id))


@router.get("/cases/{case_id}/hypotheses/manual")
def list_manual_hypotheses(case_id: str,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """人工假设清单（含审计链）。"""
    _get_owned_case(case_id, p, ctx.cases)
    return ok(_manual_payload(ctx, case_id),
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/hypotheses/manual",
             status_code=201)
def add_manual_hypothesis(case_id: str, body: HypothesisIn,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """新增人工假设（证伪条件必填）。"""
    _get_owned_case(case_id, p, ctx.cases)
    try:
        data = hypotheses_store.add_manual(
            ctx.factory.case_dir(case_id), body.model_dump(), p.username)
    except hypotheses_store.HypothesesError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/hypotheses/manual/{hid}")
def update_manual_hypothesis(case_id: str, hid: str, body: HypothesisIn,
                             p: Principal = Depends(get_principal),
                             ctx: WebContext = Depends(get_ctx)):
    """修改人工假设（自动生成的假设不可改）。"""
    _get_owned_case(case_id, p, ctx.cases)
    try:
        data = hypotheses_store.update_manual(
            ctx.factory.case_dir(case_id), hid, body.model_dump(), p.username)
    except hypotheses_store.HypothesesError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.delete("/cases/{case_id}/hypotheses/manual/{hid}")
def remove_manual_hypothesis(case_id: str, hid: str,
                             p: Principal = Depends(get_principal),
                             ctx: WebContext = Depends(get_ctx)):
    """删除人工假设。"""
    _get_owned_case(case_id, p, ctx.cases)
    try:
        data = hypotheses_store.remove_manual(
            ctx.factory.case_dir(case_id), hid, p.username)
    except hypotheses_store.HypothesesError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/hypotheses/manual/reorder")
def reorder_manual_hypotheses(case_id: str, body: ReorderIn,
                              p: Principal = Depends(get_principal),
                              ctx: WebContext = Depends(get_ctx)):
    """重排人工假设顺序。"""
    _get_owned_case(case_id, p, ctx.cases)
    try:
        data = hypotheses_store.reorder(
            ctx.factory.case_dir(case_id), body.order, p.username)
    except hypotheses_store.HypothesesError as e:
        raise APIError(ERR_VALIDATION, str(e), 400)
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
