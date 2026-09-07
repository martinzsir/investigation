"""
server/app/main.py
FastAPI 应用工厂（M1 骨架，S1~S6 落点）。

- S1：SSE 与普通端点同一 Bearer 鉴权（deps.get_principal）；
- S2：CORS 白名单由 SUNZI_CORS_ORIGINS 环境变量驱动，**默认关闭**；
- S3：所有业务路由统一 /api/v1 前缀；
- S4：错误信封 {"ok":false,"error":{"code","message"}}，
  含 DEGRADED_WRITE_REJECTED(409) 码位（降级写拒绝，M2 起实际触发）；
- S5：GET /api/v1/health 免认证探针；
- S6：成功信封 {"ok":true,"data","data_version"} + 幂等入队语义。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.envelope import APIError, error_body, ok
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.routers import auth as auth_router
from server.app.routers import audit as audit_router
from server.app.routers import cases as cases_router
from server.app.routers import clues as clues_router
from server.app.routers import dashboard as dashboard_router
from server.app.routers import ingest as ingest_router
from server.app.routers import rule_workshop as rule_workshop_router
from server.app.routers import tasks as tasks_router
from server.app.store import StoreFactory

API_PREFIX = "/api/v1"
SERVICE_VERSION = "M1"


def create_app(ctx: WebContext, *, cors_origins: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="孙武侦查官 Web 服务", version=SERVICE_VERSION)
    app.state.ctx = ctx

    # S2：CORS 默认关闭；白名单仅来自显式参数或 SUNZI_CORS_ORIGINS
    if cors_origins is None:
        cors_origins = [
            o.strip() for o in os.getenv("SUNZI_CORS_ORIGINS", "").split(",")
            if o.strip()
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return JSONResponse(status_code=exc.status,
                            content=error_body(exc.code, exc.message))

    # S5：免认证健康探针
    @app.get(f"{API_PREFIX}/health")
    def health():
        try:
            ctx.repo.list_cases()
            meta_ok = True
        except Exception:
            meta_ok = False
        return ok({
            "status": "ok" if meta_ok else "degraded",
            "service": "sunzi-web",
            "version": SERVICE_VERSION,
            "meta": "ok" if meta_ok else "degraded",
        })

    app.include_router(auth_router.router, prefix=API_PREFIX)
    app.include_router(cases_router.router, prefix=API_PREFIX)
    app.include_router(tasks_router.router, prefix=API_PREFIX)
    app.include_router(audit_router.router, prefix=API_PREFIX)
    app.include_router(audit_router.admin_router, prefix=API_PREFIX)
    app.include_router(dashboard_router.router, prefix=API_PREFIX)
    app.include_router(clues_router.router, prefix=API_PREFIX)
    app.include_router(rule_workshop_router.router, prefix=API_PREFIX)
    app.include_router(ingest_router.router, prefix=API_PREFIX)
    return app


def build_default_app() -> FastAPI:
    """生产/本机默认装配：meta/meta.db + cases/（路径可经环境变量覆盖）。"""
    meta_path = os.getenv("SUNZI_META_DB", "meta/meta.db")
    cases_root = os.getenv("SUNZI_CASES_ROOT", "cases")
    ontology_root = os.getenv("SUNZI_ONTOLOGY_ROOT") or None
    ttl = int(os.getenv("SUNZI_SESSION_TTL_HOURS", "12"))

    Path(meta_path).parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteMetaRepo(meta_path)
    factory = StoreFactory(cases_root=cases_root, meta=repo)
    cases = CaseService(repo, factory, ontology_root=ontology_root,
                        cases_root=cases_root)
    ctx = WebContext(repo=repo, factory=factory, cases=cases,
                     session_ttl_hours=ttl)
    return create_app(ctx)


# uvicorn server.app.main:app
app = build_default_app()
