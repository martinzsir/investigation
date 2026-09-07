"""
server/app/routers/tasks.py
任务端点：提交 BUILD / 列表 / 详情 / SSE 进度流。

- POST /cases/{cid}/tasks 入队（API 不执行，W-006）；幂等键冲突返回既有任务（S6）；
- 跨租户任务/案件一律 404；
- operator 取自会话，请求体不接受 operator。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from server.app.deps import (
    WebContext,
    case_dto,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import (
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.meta.models import TaskRow
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.sse import sse_response
from server.app.worker.tasks import HANDLERS, TASK_BUILD, enqueue_task

router = APIRouter(tags=["tasks"])


class CreateTaskIn(BaseModel):
    task_type: str = TASK_BUILD
    idem_key: str = ""
    params: dict = Field(default_factory=dict)


def _get_owned_task(task_id: str, p: Principal, ctx: WebContext) -> TaskRow:
    task = ctx.repo.get_task(task_id)
    if task is None:
        raise APIError(ERR_NOT_FOUND, f"任务不存在：{task_id}", 404)
    case = ctx.repo.get_case(task.case_id)
    if case is None or case.tenant_id != p.tenant_id:
        raise APIError(ERR_NOT_FOUND, f"任务不存在：{task_id}", 404)
    return task


@router.post("/cases/{case_id}/tasks")
def create_task(case_id: str, body: CreateTaskIn,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)  # 404 校验
    if body.task_type not in HANDLERS:
        raise APIError(ERR_VALIDATION,
                       f"不支持的任务类型：{body.task_type}（可用："
                       f"{sorted(HANDLERS)}）", 400)
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=body.task_type,
        params=body.params, idem_key=body.idem_key,
        created_by=p.operator)  # operator 取自会话（S6）
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))


@router.get("/tasks")
def list_tasks(case_id: str | None = None,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    case_ids: list[str] = []
    if case_id is not None:
        _get_owned_case(case_id, p, ctx.cases)
        case_ids = [case_id]
    else:
        case_ids = [c.id for c in ctx.cases.list_cases(p.tenant_id)]
    rows: list[TaskRow] = []
    for cid in case_ids:
        rows.extend(ctx.repo.list_tasks(case_id=cid))
    return ok([task_dto(t) for t in rows])


@router.get("/tasks/{task_id}")
def get_task(task_id: str, p: Principal = Depends(get_principal),
             ctx: WebContext = Depends(get_ctx)):
    task = _get_owned_task(task_id, p, ctx)
    return ok(task_dto(task))


@router.get("/tasks/{task_id}/events")
def task_events(task_id: str, request: Request,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    _get_owned_task(task_id, p, ctx)  # S1：SSE 同样 Bearer 鉴权
    return sse_response(ctx.repo, task_id,
                        request.headers.get("last-event-id"))
