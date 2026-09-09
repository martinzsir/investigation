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
    ERR_CONFLICT,
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.meta.models import TASK_PENDING, TaskRow
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.sse import sse_response
from server.app.worker.tasks import HANDLERS, TASK_BUILD, enqueue_task

router = APIRouter(tags=["tasks"])


class CreateTaskIn(BaseModel):
    task_type: str = TASK_BUILD
    idem_key: str = ""
    params: dict = Field(default_factory=dict)


class CancelIn(BaseModel):
    reason: str = ""


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


_TASK_STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
_STATUS_ALIAS = {"active": ("PENDING", "RUNNING")}


def _parse_statuses(raw: str | None) -> list[str] | None:
    """status 查询参数：逗号分隔；'active' 别名=排队+运行；非法值忽略。"""
    if not raw:
        return None
    out: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if token in _STATUS_ALIAS:
            out.extend(_STATUS_ALIAS[token])
        elif token in _TASK_STATUSES:
            out.append(token)
    # 去重保序
    seen: set[str] = set()
    uniq = [s for s in out if not (s in seen or seen.add(s))]
    return uniq or None


@router.get("/tasks")
def list_tasks(case_id: str | None = None,
               status: str | None = None,
               task_type: str | None = None,
               page: int = 1,
               page_size: int = 50,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """W-P-024 任务列表：服务端分页 + 状态/类型过滤。
    返回 {items,total,page,page_size,stats}；stats 为该范围全状态计数（不受过滤影响）。"""
    if case_id is not None:
        _get_owned_case(case_id, p, ctx.cases)
        case_ids = [case_id]
    else:
        case_ids = [c.id for c in ctx.cases.list_cases(p.tenant_id)]

    statuses = _parse_statuses(status)
    page = max(1, page)
    page_size = min(max(1, page_size), 200)

    rows = ctx.repo.query_tasks(
        case_ids=case_ids, statuses=statuses,
        task_type=task_type or None,
        limit=page_size, offset=(page - 1) * page_size)
    total = ctx.repo.count_tasks(
        case_ids=case_ids, statuses=statuses, task_type=task_type or None)

    counts = ctx.repo.task_status_counts(case_ids=case_ids)
    pending = counts.get("PENDING", 0)
    running = counts.get("RUNNING", 0)
    stats = {
        "total": sum(counts.get(s, 0) for s in _TASK_STATUSES),
        "pending": pending,
        "running": running,
        "active": pending + running,
        "succeeded": counts.get("SUCCEEDED", 0),
        "failed": counts.get("FAILED", 0),
        "cancelled": counts.get("CANCELLED", 0),
    }

    return ok({
        "items": [task_dto(t) for t in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "stats": stats,
    }, data_version=ctx.repo.current_version(case_id) if case_id else None)


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


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, body: CancelIn = CancelIn(),
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """W-P-014：取消任务。仅 PENDING 可取消（RUNNING/终态 409，不协作中断）；
    权限=任务创建人或 admin。"""
    task = _get_owned_task(task_id, p, ctx)
    user = ctx.repo.get_user(p.operator)
    is_admin = (user is not None and user.is_admin == 1) or p.role == "system"
    if task.created_by != p.operator and not is_admin:
        ctx.repo.record_platform_event(
            "authz_failure", operator=p.operator, tenant_id=p.tenant_id,
            detail={"endpoint": f"/tasks/{task_id}/cancel",
                    "reason": "非创建人且非管理员"})
        raise APIError(ERR_FORBIDDEN, "仅任务创建人或管理员可取消", 403)
    if task.status != TASK_PENDING:
        raise APIError(ERR_CONFLICT,
                       f"任务状态为 {task.status}，仅排队中（PENDING）可取消",
                       409)
    updated = ctx.repo.cancel_task(task_id, by=p.operator, reason=body.reason)
    if updated is None:  # 竞态：被 Worker 抢先认领
        fresh = ctx.repo.get_task(task_id)
        raise APIError(ERR_CONFLICT,
                       f"任务状态为 {fresh.status if fresh else '?'}，"
                       f"仅排队中（PENDING）可取消", 409)
    ctx.repo.record_ops("task_cancel", task.case_id,
                        {"task_id": task_id, "by": p.operator,
                         "reason": body.reason})
    return ok({"task_id": task_id, "status": updated.status})
