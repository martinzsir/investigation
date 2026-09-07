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
)
from server.app.envelope import (
    ERR_CONFLICT,
    ERR_NOT_FOUND,
    APIError,
    ok,
)
from server.app.security import Principal

router = APIRouter(prefix="/cases", tags=["cases"])


class CreateCaseIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    pack_id: str = "default"


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
