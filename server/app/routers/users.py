"""
server/app/routers/users.py
用户管理面端点（S0-2）：平台管理员授权 本体管理员 / 平台管理员 能力位。

- 授权门槛：platform_admin_flag（is_admin=1 或 system）；
- 授权动作落平台审计（record_platform_event），可追责；
- 标准域写端点（S3/S4/S5 落地时）以 require_ontology_admin 消费能力位，
  双条件 = is_ontology_admin=1 且 ROLE_RANK ≥ 偏将（core/access.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, ERR_NOT_FOUND, APIError, ok
from server.app.security import Principal, platform_admin_flag

router = APIRouter(prefix="/users", tags=["users"])


class FlagIn(BaseModel):
    enabled: bool


def _require_platform_admin(p: Principal) -> None:
    if not platform_admin_flag(p.is_admin, p.role):
        raise APIError(ERR_FORBIDDEN, "仅平台管理员可执行授权操作", 403)


@router.post("/{operator}/ontology-admin")
def set_ontology_admin(operator: str, body: FlagIn,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """授权/撤销本体管理员能力位（S0-2，PRD §11：授权方 = 平台管理员）。"""
    _require_platform_admin(p)
    if ctx.repo.get_user(operator) is None:
        raise APIError(ERR_NOT_FOUND, f"用户不存在：{operator}", 404)
    ctx.repo.set_user_ontology_admin(operator, body.enabled)
    ctx.repo.record_platform_event(
        "ontology_admin_grant", operator=p.operator,
        detail={"target": operator, "enabled": body.enabled})
    return ok({"operator": operator, "is_ontology_admin": body.enabled})


@router.post("/{operator}/admin")
def set_platform_admin(operator: str, body: FlagIn,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """授权/撤销平台管理员标志（W-024 setter 首次接线）。"""
    _require_platform_admin(p)
    if ctx.repo.get_user(operator) is None:
        raise APIError(ERR_NOT_FOUND, f"用户不存在：{operator}", 404)
    ctx.repo.set_user_admin(operator, body.enabled)
    ctx.repo.record_platform_event(
        "platform_admin_grant", operator=p.operator,
        detail={"target": operator, "enabled": body.enabled})
    return ok({"operator": operator, "is_admin": body.enabled})
