"""
server/app/routers/auth.py
认证端点：login / logout / me（S1 Bearer 会话，无 Cookie/query-token 旁路）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.app.deps import (
    LOGIN_FAIL_MESSAGE,
    WebContext,
    get_ctx,
    get_principal,
)
from server.app.envelope import ERR_UNAUTHORIZED, APIError, ok
from server.app.meta.models import USER_ACTIVE
from server.app.security import Principal, new_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    operator: str
    password: str


@router.post("/login")
def login(body: LoginIn, ctx: WebContext = Depends(get_ctx)):
    user = ctx.repo.get_user(body.operator)
    if (user is None or user.status != USER_ACTIVE
            or not verify_password(body.password, user.salt,
                                   user.password_hash)):
        # 统一文案：不泄露用户是否存在
        raise APIError(ERR_UNAUTHORIZED, LOGIN_FAIL_MESSAGE, 401)
    token = new_token()
    sess = ctx.new_session(user, token)
    ctx.repo.create_session(sess)
    return ok({
        "token": token,
        "expires_at": sess.expires_at,
        "operator": user.operator,
        "role": user.role,
        "clearance": user.clearance,
        "tenant_id": user.tenant_id,
    })


@router.post("/logout")
def logout(p: Principal = Depends(get_principal),
           ctx: WebContext = Depends(get_ctx)):
    ctx.repo.revoke_session(p.token)
    return ok({"revoked": True})


@router.get("/me")
def me(p: Principal = Depends(get_principal)):
    return ok({
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
        "tenant_id": p.tenant_id,
    })
