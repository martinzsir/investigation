"""
server/app/routers/cases.py
案件端点：列表 / 建案 / 详情。跨租户访问一律 404（不泄露存在性）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.app.cases import CaseAlreadyExists, CaseService, PackNotFound
from server.app.deps import (
    WebContext,
    case_dto,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import (
    ERR_CONFLICT,
    ERR_NOT_FOUND,
    APIError,
    ok,
)
from server.app.meta.models import CASE_ARCHIVED, IllegalTransition
from server.app.portal_view import assemble_summary
from server.app.security import Principal
from server.app.snapshot_config import require_analyst
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TASK_ARCHIVE, enqueue_task

router = APIRouter(prefix="/cases", tags=["cases"])


class CreateCaseIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    pack_id: str = "default"


class ArchiveIn(BaseModel):
    reason: str = ""


def _get_owned_case(case_id: str, p: Principal, svc: CaseService):
    """取案件并做租户校验；跨租户/不存在统一 404。"""
    case = svc.get_case(case_id)
    if case is None or case.tenant_id != p.tenant_id:
        raise APIError(ERR_NOT_FOUND, f"案件不存在：{case_id}", 404)
    return case


@router.get("")
def list_cases(p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    rows = ctx.cases.list_cases(p.tenant_id)
    return ok([case_dto(c) for c in rows])


@router.post("")
def create_case(body: CreateCaseIn, p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    try:
        case = ctx.cases.create_case(
            case_id=body.case_id, name=body.name,
            tenant_id=p.tenant_id, pack_id=body.pack_id,
            created_by=p.operator)  # operator 取自会话，不取请求体
    except CaseAlreadyExists as e:
        raise APIError(ERR_CONFLICT, str(e), 409)
    except PackNotFound as e:
        raise APIError(ERR_NOT_FOUND, str(e), 404)
    return ok(case_dto(case), data_version=0)


@router.get("/{case_id}")
def get_case(case_id: str, p: Principal = Depends(get_principal),
             ctx: WebContext = Depends(get_ctx)):
    case = _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    return ok(case_dto(case), data_version=version)


@router.get("/{case_id}/summary")
def case_summary(case_id: str, p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """W-P-015 门户汇总：案件 dto + 待办计数 + 最近任务 + 健康度。"""
    case = _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    tasks = ctx.repo.list_tasks(case_id=case_id, limit=50)
    state_path = ctx.factory.case_dir(case_id) / "state.sqlite"
    state_map: dict = {}
    chain_ok = True
    state = None
    if state_path.exists():
        state = StateStore(case_id, state_path)
        try:
            state_map = state.status_map()
            chain_ok = state.chain_verify()
        except Exception:
            chain_ok = False
        finally:
            state.close()
    conn = None
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            conn = store.read_conn
        except FileNotFoundError:
            conn = None  # 未 BUILD：诊断计数为 0
        summary = assemble_summary(
            case_dir=ctx.factory.case_dir(case_id),
            state_map=state_map, chain_ok=chain_ok,
            conn=conn, tasks=tasks, version=version)
    finally:
        if store is not None:
            store.close()
    recent = sorted(tasks, key=lambda t: t.created_at, reverse=True)[:5]
    return ok({"case": case_dto(case), "data_version": version, **summary,
               "recent_tasks": [task_dto(t) for t in recent]},
              data_version=version)


@router.post("/{case_id}/archive", status_code=202)
def archive_case(case_id: str, body: ArchiveIn = ArchiveIn(),
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """W-P-015 归档：侦查中→已封存（状态机非法迁移 409）+ 入队版本压实。"""
    case = _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    if case.status == CASE_ARCHIVED:
        raise APIError(ERR_CONFLICT, "案件已封存（终态），不可重复归档", 409)
    try:
        ctx.repo.transition_case(case_id, CASE_ARCHIVED, by=p.operator)
    except IllegalTransition as e:
        raise APIError(ERR_CONFLICT, str(e), 409)
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_ARCHIVE,
        params={"reason": body.reason, "operator": p.operator},
        idem_key=f"archive:{case_id}", created_by=p.operator)
    ctx.repo.record_ops("case_archive", case_id,
                        {"by": p.operator, "reason": body.reason})
    return ok(task_dto(task))
