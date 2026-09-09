"""
server/app/deps.py
请求依赖：WebContext 装配 + Bearer 会话认证（S1）+ DTO 序列化。

认证纪律：
  - 无 token / token 无效 / 会话过期或吊销 / 用户停用 → 401 统一错误；
  - operator 只从会话取，请求体 operator 字段在路由层显式忽略；
  - AccessContext 一律 network="web"（与 core/access.py NETWORKS 对齐）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, Request

from core.access import AccessContext

from server.app.cases import CaseService
from server.app.envelope import ERR_UNAUTHORIZED, APIError
from server.app.meta.models import (
    CaseRecord,
    Session,
    TaskRow,
    USER_ACTIVE,
    User,
)
from server.app.meta.repo import MetaRepo
from server.app.security import Principal, bearer_token
from server.app.store import StoreFactory

LOGIN_FAIL_MESSAGE = "用户名或密码错误"


@dataclass
class WebContext:
    repo: MetaRepo
    factory: StoreFactory
    cases: CaseService
    session_ttl_hours: int = 12

    def new_session(self, user: User, token: str) -> Session:
        now = datetime.now()
        return Session(token=token, operator=user.operator,
                       created_at=now.isoformat(timespec="seconds"),
                       expires_at=(now + timedelta(
                           hours=self.session_ttl_hours)).isoformat(
                           timespec="seconds"))


def get_ctx(request: Request) -> WebContext:
    return request.app.state.ctx


def _session_valid(sess: Session | None) -> bool:
    if sess is None or sess.revoked:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(sess.expires_at)
    except ValueError:
        return False


def get_principal(request: Request,
                  ctx: WebContext = Depends(get_ctx)) -> Principal:
    token = bearer_token(request.headers.get("authorization"))
    if not token:
        # W-024：鉴权失败（缺令牌）落平台审计
        ctx.repo.record_platform_event(
            "auth_failure", operator="",
            detail={"reason": "missing_token",
                    "ip": request.client.host if request.client else ""})
        raise APIError(ERR_UNAUTHORIZED, "缺少 Bearer 令牌（S1：不开旁路）", 401)
    sess = ctx.repo.get_session(token)
    if not _session_valid(sess):
        ctx.repo.record_platform_event(
            "auth_failure",
            operator=sess.operator if sess else "",
            detail={"reason": "session_invalid",
                    "ip": request.client.host if request.client else ""})
        raise APIError(ERR_UNAUTHORIZED, "会话无效或已过期", 401)
    user = ctx.repo.get_user(sess.operator)
    if user is None or user.status != USER_ACTIVE:
        ctx.repo.record_platform_event(
            "auth_failure", operator=sess.operator,
            detail={"reason": "user_invalid",
                    "ip": request.client.host if request.client else ""})
        raise APIError(ERR_UNAUTHORIZED, "用户不存在或已停用", 401)
    return Principal(operator=user.operator, role=user.role,
                     clearance=user.clearance, tenant_id=user.tenant_id,
                     token=token, is_admin=user.is_admin)


def access_for(p: Principal, *, case_id: str = "default",
               purpose: str = "web") -> AccessContext:
    """web 出口的 AccessContext：operator 强制取会话主体。"""
    return AccessContext(operator=p.operator, role=p.role,
                         clearance=p.clearance, case_id=case_id,
                         purpose=purpose, network="web")


# ---- DTO（出参不直泄 dataclass 内部字段）----
def case_dto(c: CaseRecord) -> dict[str, Any]:
    d = asdict(c)
    d.pop("current_version", None)  # 版本单独经 data_version 通道
    return d


def task_dto(t: TaskRow) -> dict[str, Any]:
    return asdict(t)
