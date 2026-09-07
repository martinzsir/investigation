"""
server/app/routers/dashboard.py
W-018 治理仪表盘端点：首屏组装 / 诊断 listing / 诊断下钻。

- 跨租户案件一律 404（沿用 M1 案件纪律）；
- 案件数据经 StoreFactory 请求作用域只读连接（读者租约，finally 释放）；
- 未 BUILD / 表未生成的案件逐节降级，不 500。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from server.app import dashboard
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_NOT_FOUND, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["dashboard"])


@router.get("/cases/{case_id}/dashboard")
def get_dashboard(case_id: str,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    store = None
    data: dict
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            data = dashboard.assemble(case_id, store.read_conn,
                                      repo=ctx.repo)
        except FileNotFoundError:
            # 未 BUILD（无版本文件）：全卡降级，仍返回信封
            data = dashboard.assemble(case_id, None, repo=ctx.repo)
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/diagnostics")
def list_diagnostics(case_id: str,
                     kind: str | None = None,
                     severity: str | None = None,
                     limit: int = Query(200, ge=1, le=1000),
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    items: list[dict] = []
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            items = dashboard.list_diagnostics(store.read_conn, kind=kind,
                                               severity=severity,
                                               limit=limit)
        except FileNotFoundError:
            pass  # 未 BUILD → 空 listing
    finally:
        if store is not None:
            store.close()
    return ok({"items": items, "total": len(items)})


@router.get("/cases/{case_id}/diagnostics/{seq}")
def diagnostic_detail(case_id: str, seq: int,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    row = None
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            row = dashboard.diagnostic_detail(store.read_conn, seq)
        except FileNotFoundError:
            pass
    finally:
        if store is not None:
            store.close()
    if row is None:
        raise APIError(ERR_NOT_FOUND, f"诊断不存在：seq={seq}", 404)
    return ok(row)
