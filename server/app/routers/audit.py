"""
server/app/routers/audit.py
W-023/024：审计链时间线 / 自检（空链红线）/ 平台审计事件（管理面）。

红线（决策 D-M2-3）：
  - Web 案件空链（actual_count==0）一律 chain_ok=false + empty_chain=true；
    core 的 REQ-G-025「纯查询运行空链=完整」是 CLI/MCP 语义；平台案件建案必经
    BUILD 留痕，不存在合法空链场景，故红线在 API 包装层强制，core 零改动。
  - 身份一律取会话主体（W-024）；时间线 operator 查询参数仅作筛选条件，
    不代表调用者身份；请求体不接受 operator 字段。

读路径纪律：routers 不写 SQL——时间线/自检一律经 core/audit.py 的只读面
（AuditChain.readonly + timeline/chain_integrity）；案件数据经 StoreFactory
请求作用域只读连接（读者租约落 meta，finally 释放）。版本文件缺失 /
audit_chain 表不存在（纯建案未 BUILD）→ 按空链处理，不 500。
"""
from __future__ import annotations

import duckdb
from fastapi import APIRouter, Depends, Query

from core.audit import AuditChain

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["audit"])                         # /cases/{cid}/audit*
admin_router = APIRouter(prefix="/audit", tags=["audit"])  # /audit/events

# 空链降级明细（与 chain_integrity 字段同构；chain_ok=False 即空链红线基线）
_EMPTY_INTEGRITY: dict = {
    "chain_ok": False, "expected_count": 0, "actual_count": 0,
    "broken_links": [], "missing_fields": [], "disposal_events": 0,
    "disposal_activity": {"persisted_non_pending": 0, "actions_applied": 0},
    "empty_chain": True,
}

# 案件版本文件缺失 / audit_chain 表不存在 → 按空链处理
_EMPTY_ERRORS = (FileNotFoundError, duckdb.CatalogException, duckdb.IOException)


def _integrity_dto(integ: dict) -> dict:
    """verify 出参组装：空链红线 + 处置交叉比对（routers 不写 SQL，只编排）。"""
    integ = dict(integ)
    # 决策 D-M2-3：actual_count==0 一律判不完整（平台案件无合法空链场景）
    empty_chain = int(integ.get("actual_count") or 0) == 0
    if empty_chain:
        integ["chain_ok"] = False
    integ["empty_chain"] = empty_chain
    activity = integ.get("disposal_activity") or {}
    events = int(integ.get("disposal_events") or 0)
    persisted = int(activity.get("persisted_non_pending") or 0)
    applied = int(activity.get("actions_applied") or 0)
    integ["cross_check"] = {
        "disposal_events": events,
        "persisted_non_pending": persisted,
        "actions_applied": applied,
        "consistent": (events > 0) == (persisted + applied > 0),
    }
    return integ


@router.get("/cases/{case_id}/audit")
def audit_timeline(
        case_id: str,
        operator: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        action: str | None = None,
        clue_id: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        p: Principal = Depends(get_principal),
        ctx: WebContext = Depends(get_ctx)):
    """W-023 AC-1/2：审计时间线（操作人/状态迁移/法定依据/本体版本 + 筛选分页）。"""
    _get_owned_case(case_id, p, ctx.cases)  # 跨租户 404
    items: list[dict] = []
    total = 0
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            chain = AuditChain.readonly(store.read_conn, case_id)
            result = chain.timeline(operator=operator, from_ts=from_ts,
                                    to_ts=to_ts, action=action,
                                    clue_id=clue_id, limit=page_size,
                                    offset=(page - 1) * page_size)
            items, total = result["items"], result["total"]
        except _EMPTY_ERRORS:
            pass  # 未 BUILD / 链表未建 → 空时间线
    finally:
        if store is not None:
            store.close()
    return ok({"items": items, "total": total,
               "page": page, "page_size": page_size})


@router.post("/cases/{case_id}/audit/verify")
def audit_verify(case_id: str,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """W-023 AC-3/4：链完整性自检 + 空链红线（API 包装层，D-M2-3）+ 处置交叉比对。

    - cross_check：链上处置事件数 vs 库内处置留痕（clue_disposal_status 非待查
      行 + 已本地提交动作），两侧零/非零不一致即接线缺口信号（core 已计入
      chain_ok，此处透传证据供前端展示）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    integ: dict
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            integ = AuditChain.readonly(store.read_conn,
                                        case_id).chain_integrity()
        except _EMPTY_ERRORS:
            integ = dict(_EMPTY_INTEGRITY)
    finally:
        if store is not None:
            store.close()
    return ok(_integrity_dto(integ))


@admin_router.get("/events")
def platform_audit_events(
        tenant_id: str | None = None,
        event: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        p: Principal = Depends(get_principal),
        ctx: WebContext = Depends(get_ctx)):
    """W-024：平台审计事件（登录/登出/鉴权失败）。仅 is_admin=1 或 system 角色。

    非管理员 403（并落 authz_failure 平台事件）；管理员可跨租户查询
    （tenant_id 显式传参），缺省不过滤（全量）。
    """
    user = ctx.repo.get_user(p.operator)
    if not ((user is not None and user.is_admin == 1) or p.role == "system"):
        ctx.repo.record_platform_event(
            "authz_failure", operator=p.operator, tenant_id=p.tenant_id,
            detail={"endpoint": "/audit/events", "reason": "非管理员访问平台审计"})
        raise APIError(ERR_FORBIDDEN, "平台审计事件仅管理员可查", 403)
    rows = ctx.repo.list_platform_events(tenant_id=tenant_id, event=event,
                                         limit=limit)
    return ok({"items": rows, "total": len(rows)})
