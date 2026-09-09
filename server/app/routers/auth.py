"""
server/app/routers/auth.py
认证端点：login / logout / me（S1 Bearer 会话，无 Cookie/query-token 旁路）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from server.app.deps import (
    LOGIN_FAIL_MESSAGE,
    WebContext,
    get_ctx,
    get_principal,
)
from server.app.envelope import ERR_UNAUTHORIZED, APIError, ok
from server.app.meta.models import USER_ACTIVE
from server.app.security import (
    Principal,
    new_token,
    platform_admin_flag,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    operator: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request,
          ctx: WebContext = Depends(get_ctx)):
    user = ctx.repo.get_user(body.operator)
    if (user is None or user.status != USER_ACTIVE
            or not verify_password(body.password, user.salt,
                                   user.password_hash)):
        # 统一文案：不泄露用户是否存在
        # W-024：登录失败落平台审计（operator 取自尝试主体，不泄露存在性）
        ctx.repo.record_platform_event(
            "login_failure", operator=body.operator,
            tenant_id=user.tenant_id if user else "",
            detail={"ip": request.client.host if request.client else ""})
        raise APIError(ERR_UNAUTHORIZED, LOGIN_FAIL_MESSAGE, 401)
    token = new_token()
    sess = ctx.new_session(user, token)
    ctx.repo.create_session(sess)
    # W-024：登录成功落平台审计（operator 与会话/案件审计链同值一一对应）
    ctx.repo.record_platform_event(
        "login", operator=user.operator, tenant_id=user.tenant_id,
        detail={"ip": request.client.host if request.client else ""})
    return ok({
        "token": token,
        "expires_at": sess.expires_at,
        "operator": user.operator,
        "role": user.role,
        "clearance": user.clearance,
        "tenant_id": user.tenant_id,
        # 平台管理员标志（W-024；前端管理面据此渲染，不靠 403 探测）
        "is_admin": platform_admin_flag(user.is_admin, user.role),
    })


@router.post("/logout")
def logout(p: Principal = Depends(get_principal),
           ctx: WebContext = Depends(get_ctx)):
    ctx.repo.revoke_session(p.token)
    # W-024：登出落平台审计
    ctx.repo.record_platform_event("logout", operator=p.operator,
                                   tenant_id=p.tenant_id)
    return ok({"revoked": True})


@router.get("/me")
def me(p: Principal = Depends(get_principal)):
    return ok({
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
        "tenant_id": p.tenant_id,
        # 平台管理员标志（W-024；与 login 同口径）
        "is_admin": platform_admin_flag(p.is_admin, p.role),
    })
