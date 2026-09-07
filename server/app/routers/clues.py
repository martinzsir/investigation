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

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.app import clues_view
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
from server.app.worker.tasks import TASK_DISPOSE, enqueue_task

router = APIRouter(tags=["clues"])

LEGAL_ACTIONS = ("verify", "reset", "exclude", "confirm", "file")


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
    action = (body.action or "").strip()
    if action not in LEGAL_ACTIONS:
        raise APIError(
            ERR_VALIDATION,
            f"非法处置动作：{action!r}（合法：{', '.join(LEGAL_ACTIONS)}）",
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
    version = ctx.repo.current_version(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    try:
        data = clues_view.assemble_list(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            state_map=state_map, role=p.role,
            level=level, dimension=dimension, jian=jian,
            subject=subject, status=status,
            page=page, page_size=page_size)
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
    version = ctx.repo.current_version(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    try:
        decisions = state.list_decisions() if state is not None else []
        data = clues_view.assemble_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            clue_id=clue_id, state_map=state_map, decisions=decisions)
    finally:
        if state is not None:
            state.close()
    if data is None:
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    # 内间线索秩级过滤（详情同样 fail-closed 不可枚举）
    if ("内间" in (data.get("jian_types") or [])
            and p.role not in ("human", "system")
            and p.clearance < 2):
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    return ok(data, data_version=version)
