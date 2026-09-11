"""
server/app/routers/clues.py
W-019/020 线索与处置端点。

POST /cases/{cid}/clues/{clue_id}/actions 🔒：处置动作入队 DISPOSE 快速通道
（API 不直接写 state.sqlite，backend_api.md 2.4：业务写仍入队、FIFO、不产
版本文件）。会话快照（operator/role/clearance）随任务行，operator 强制取
会话；file 前置校验（human 角色 + legal_basis），core ActionExecutor 四步
校验在 Worker 兜底（纵深防御，两处红线语义一致）。

GET 列表/详情/suppressed（W-019）：读产物 artifact + state 状态真值，
只读不触发任务（AC-6）；内间线索秩级过滤（REQ-011 延续）。
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.verify_machine import VERIFY_STATUSES

from core.access import AccessContext, can_see_jian_types
from server.app import clues_view, ontology_meta
from server.app.deps import (
    WebContext,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import (
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TASK_DISPOSE, TASK_VERIFY, enqueue_task

router = APIRouter(tags=["clues"])

# REQ-V-005：核查项读面投影（剔除 case_id/clue_id/item_key 内部列）
_VERIFY_ITEM_FIELDS = (
    "item_id", "kind", "text", "origin", "status", "conclusion",
    "operator", "updated_at", "channel", "ref_function",
    "external", "falsification",
)

_EMPTY_VERIFY_PROGRESS = {
    "total": 0, "concluded": 0, "pending": 0,
    "suggested": 0, "ignored": 0, "by_status": {},
}


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _verify_items_payload(state: StateStore | None, clue_id: str) -> dict:
    """state 不存在（未 BUILD/未打开过工作区）→ available:false 空清单。

    纯读 state 真值；REQ-V-002 惰性供给在线索详情 assemble_detail 触发，
    本端点不重跑 evidence 构建（读面保持轻量、不创建 state.sqlite）。
    """
    if state is None:
        return {"items": [], "progress": dict(_EMPTY_VERIFY_PROGRESS),
                "available": False}
    items = [{k: it.get(k) for k in _VERIFY_ITEM_FIELDS}
             for it in state.list_verify_items(clue_id)]
    return {"items": items,
            "progress": state.verify_progress(clue_id),
            "available": True}


def _read_state(factory, case_id: str):
    """只读打开 state.sqlite（不存在则 None，读面不创建文件）。"""
    path = factory.case_dir(case_id) / "state.sqlite"
    if not path.exists():
        return None, {}
    st = StateStore(case_id, path)
    return st, st.status_map()


class ClueActionIn(BaseModel):
    action: str
    note: str = ""
    reason: str = ""
    legal_basis: str = ""
    idem_key: str = ""


@router.post("/cases/{case_id}/clues/{clue_id}/actions", status_code=202)
def clue_action(case_id: str, clue_id: str, body: ClueActionIn,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """W-020：处置动作入队（202 + task_id；进度走任务 SSE）。"""
    _get_owned_case(case_id, p, ctx.cases)  # 跨租户 404
    case = ctx.repo.get_case(case_id)
    # R6：合法动作来自案件包 actions 声明（set_clue_status 类），不硬编码
    legal_actions = ontology_meta.dispose_action_names(
        case.pack_id, ctx.cases.snapshot_ontology_root(case_id))
    action = (body.action or "").strip()
    if action not in legal_actions:
        raise APIError(
            ERR_VALIDATION,
            f"非法处置动作：{action!r}（合法：{', '.join(sorted(legal_actions))}）",
            400)
    # 前置红线（core 在 Worker 兜底；API 侧快速失败省一次入队）
    if action == "file":
        if p.role != "human":
            raise APIError(
                ERR_FORBIDDEN,
                "「已立案」为 human 专属终态，正兵/AI 无权操作（W-020 AC-2）",
                403)
        if not body.legal_basis.strip():
            raise APIError(
                ERR_VALIDATION,
                "file 动作必须提供 legal_basis（法定依据/案号，W-020 AC-3）",
                400)
    action_params: dict = {}
    if body.note.strip():
        action_params["note"] = body.note.strip()
    if body.reason.strip():
        action_params["reason"] = body.reason.strip()
    if body.legal_basis.strip():
        action_params["legal_basis"] = body.legal_basis.strip()
    # 会话快照入任务行——Worker 不信任请求体 operator（REQ-009 主体一致性）
    params = {
        "action": action,
        "clue_id": clue_id,
        "action_params": action_params,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = body.idem_key.strip() or f"dispose:{clue_id}:{action}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_DISPOSE,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# W-019：线索读面（只读已产出结果，不触发重扫，AC-6）
# ----------------------------------------------------------------------
@router.get("/cases/{case_id}/clues")
def list_clues(case_id: str,
               level: str | None = None,
               dimension: str | None = None,
               jian: str | None = None,
               subject: str | None = None,
               status: str | None = None,
               page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200),
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """线索列表：级别/维度/间类/主体/状态筛选 + 优先级排序 + 分页。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    try:
        data = clues_view.assemble_list(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            state_map=state_map, role=p.role,
            level=level, dimension=dimension, jian=jian,
            subject=subject, status=status,
            page=page, page_size=page_size,
            pack_id=case.pack_id,
            ontology_base=ctx.cases.snapshot_ontology_root(case_id))
    finally:
        if state is not None:
            state.close()
    return ok(data, data_version=version)


@router.get("/cases/{case_id}/clues/suppressed")
def list_suppressed(case_id: str,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """被抑制规则结果（不删除，仅移出主列表，AC-4）。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    data = clues_view.assemble_suppressed(
        case_dir=ctx.factory.case_dir(case_id), version=version)
    return ok(data, data_version=version)


@router.get("/cases/{case_id}/clues/{clue_id}")
def clue_detail(case_id: str, clue_id: str,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """线索详情：五间/source_rows 溯源/merged_from 合并来源/状态/决策。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    access = AccessContext(
        operator=p.operator, role=p.role, clearance=p.clearance,
        case_id=case_id, purpose="线索详情", network="web")
    try:
        decisions = state.list_decisions() if state is not None else []
        data = clues_view.assemble_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            clue_id=clue_id, state_map=state_map, decisions=decisions,
            access=access, pack_id=case.pack_id,
            base_dir=ctx.cases.snapshot_ontology_root(case_id),
            state_store=state)
    finally:
        if state is not None:
            state.close()
    if data is None:
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    # 间类密级过滤（详情 fail-closed 不可枚举；声明驱动，不硬编码内间）
    if not can_see_jian_types(
            data.get("jian_types") or [], role=p.role,
            jian_clearances=ontology_meta.jian_clearances(
                case.pack_id, ctx.cases.snapshot_ontology_root(case_id))):
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    return ok(data, data_version=version)


# ----------------------------------------------------------------------
# REQ-V-005：核查工作区读写端点（读同步、写 202 入队 TASK_VERIFY）
# 状态机/红线在 Worker（core.verify_machine + agent: 拒绝）兜底，
# API 侧只做参数形状快速失败——与处置动作同款纵深防御。
# ----------------------------------------------------------------------
class VerifyItemIn(BaseModel):
    text: str = ""
    idem_key: str = ""


class VerifyTransitionIn(BaseModel):
    next_status: str = ""
    conclusion: str = ""
    text: str | None = None      # 仅采纳（建议→待核查）时有效=改写建议文本
    idem_key: str = ""


@router.get("/cases/{case_id}/clues/{clue_id}/verify-items")
def list_verify_items(case_id: str, clue_id: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """核查项清单 + progress（纯读 state；无 state.sqlite → available:false）。"""
    _get_owned_case(case_id, p, ctx.cases)  # 跨租户 404
    state, _ = _read_state(ctx.factory, case_id)
    try:
        data = _verify_items_payload(state, clue_id)
    finally:
        if state is not None:
            state.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/clues/{clue_id}/verify-items",
             status_code=202)
def add_verify_item(case_id: str, clue_id: str, body: VerifyItemIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """人工添加核查项（结构化构造器同通道）→ 202 + task_dto。"""
    _get_owned_case(case_id, p, ctx.cases)
    text = (body.text or "").strip()
    if not text:
        raise APIError(ERR_VALIDATION, "核查项文本不能为空（text）", 400)
    params = {
        "op": "add_manual",
        "clue_id": clue_id,
        "text": text,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = body.idem_key.strip() or f"verify-add:{clue_id}:{_sha1(text)}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


@router.post(
    "/cases/{case_id}/clues/{clue_id}/verify-items/{item_id}/transitions",
    status_code=202)
def transition_verify_item(case_id: str, clue_id: str, item_id: str,
                           body: VerifyTransitionIn,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """核查项状态迁移（采纳/忽略/裁决/重开）→ 202 + task_dto。

    迁移合法性、终态结论必填、item 归属校验均在 Worker 兜底；
    API 侧仅校验 next_status 形状（七态白名单），省一次非法入队。
    """
    _get_owned_case(case_id, p, ctx.cases)
    nxt = (body.next_status or "").strip()
    if not nxt:
        raise APIError(ERR_VALIDATION, "缺少目标状态 next_status", 400)
    if nxt not in VERIFY_STATUSES:
        # 仅形状校验；当前状态→nxt 是否合法由 core 状态机在 Worker 判
        raise APIError(
            ERR_VALIDATION,
            f"非法核查项状态：{nxt!r}（合法：{'/'.join(VERIFY_STATUSES)}）",
            400)
    params = {
        "op": "transition",
        "clue_id": clue_id,
        "item_id": item_id,
        "next_status": nxt,
        "conclusion": (body.conclusion or "").strip(),
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    # text 仅采纳（建议→待核查）有效；不传不落键，传空串与不传区分幂等域
    if body.text is not None:
        params["text"] = body.text
    text_for_key = body.text if body.text is not None else ""
    idem = (body.idem_key.strip()
            or f"verify:{item_id}:{nxt}:{_sha1(text_for_key)}")
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))
