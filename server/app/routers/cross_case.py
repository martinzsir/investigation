"""
server/app/routers/cross_case.py
W-026/027：跨案件查询通道（ATTACH READ_ONLY + 全有或全无鉴权）。

红线（W-027 AC-1/2）：
  - 全有或全无：用户对请求的所有案件都有权限才允许 ATTACH；
    任一案件无权限 → 整体 403，**拒绝发生在 ATTACH 之前**（不读数据）。
  - 拒绝事件进审计（含被拒案件列表与原因）。

查询纪律（W-026 AC-4/5/6）：
  - 禁 DDL/DML（CrossCaseStore 首词白名单 + READ_ONLY 双保险）；
  - 强制 max_rows 上限与超时；
  - 查询本身进审计链（案件列表 + SQL 原文 + reason）。

routers 不写 SQL：ATTACH 在 CrossCaseStore（store 层），router 只编排
鉴权 → 工厂 → query → 审计。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store import UnsupportedOperation

router = APIRouter(prefix="/cross-case", tags=["cross-case"])

# 默认上限（W-026 AC-5）
_DEFAULT_MAX_ROWS = 1000
_MAX_MAX_ROWS = 10000
_DEFAULT_TIMEOUT_MS = 30000


class CrossCaseQueryIn(BaseModel):
    case_ids: list[str] = Field(min_length=2, max_length=50)
    sql: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    max_rows: int = Field(_DEFAULT_MAX_ROWS, ge=1, le=_MAX_MAX_ROWS)
    timeout_ms: int = Field(_DEFAULT_TIMEOUT_MS, ge=100, le=600000)


def _authorize_all(case_ids: list[str], p: Principal,
                   ctx: WebContext) -> list[str]:
    """W-027 全有或全无鉴权：遍历校验每个案件的租户归属。

    返回已授权案件列表；任一失败抛 403（含被拒案件列表）。
    鉴权在 ATTACH 之前完成——不调用 factory.for_cross_case。
    """
    authorized: list[str] = []
    denied: list[str] = []
    for cid in case_ids:
        case = ctx.cases.get_case(cid)
        if case is None or case.tenant_id != p.tenant_id:
            denied.append(cid)
        else:
            authorized.append(cid)
    if denied:
        # W-027 AC-5：拒绝事件进审计
        ctx.repo.record_ops(
            "cross_case_denied", case_id="*cross-case*",
            payload={
                "operator": p.operator,
                "requested_cases": list(case_ids),
                "denied_cases": denied,
                "reason": "部分案件无权限（全有或全无鉴权）",
            })
        raise APIError(
            ERR_FORBIDDEN,
            f"跨案件查询被拒：无权限访问案件 {denied}"
            f"（全有或全无鉴权，共请求 {len(case_ids)} 个）",
            403)
    return authorized


@router.post("/query")
def cross_case_query(
        body: CrossCaseQueryIn,
        p: Principal = Depends(get_principal),
        ctx: WebContext = Depends(get_ctx)):
    """W-026/027：跨案件只读查询。

    全有或全无鉴权 → ATTACH READ_ONLY → 执行 → 审计。
    """
    if len(body.case_ids) != len(set(body.case_ids)):
        raise APIError(ERR_VALIDATION, "case_ids 不得重复", 400)

    # 1. 全有或全无鉴权（ATTACH 之前，W-027 AC-2）
    authorized = _authorize_all(body.case_ids, p, ctx)

    # 2. ATTACH + 查询（store 层负责 READ_ONLY + 禁 DDL/DML + max_rows + 超时）
    store = ctx.factory.for_cross_case(authorized)
    try:
        try:
            rows = store.query(
                body.sql, max_rows=body.max_rows,
                timeout_ms=body.timeout_ms)
        except UnsupportedOperation as e:
            raise APIError(ERR_VALIDATION, str(e), 400)
    finally:
        store.close()

    # 3. 审计（W-026 AC-6：案件列表 + SQL 原文 + reason）
    ctx.repo.record_ops(
        "cross_case_query", case_id="*cross-case*",
        payload={
            "operator": p.operator,
            "case_ids": authorized,
            "sql": body.sql,
            "reason": body.reason,
            "max_rows": body.max_rows,
            "result_rows": len(rows),
        })

    return ok({"rows": rows, "total": len(rows),
               "case_ids": authorized})


@router.get("/history")
def cross_case_history(
        limit: int = 50,
        p: Principal = Depends(get_principal),
        ctx: WebContext = Depends(get_ctx)):
    """W-026：本用户跨案件查询记录。"""
    events = ctx.repo.list_ops(kind="cross_case_query", limit=limit)
    items: list[dict] = []
    for ev in events:
        try:
            payload = json.loads(ev.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        # 仅返回本用户的记录
        if payload.get("operator") != p.operator:
            continue
        items.append({
            "id": ev.get("id"),
            "ts": ev.get("ts"),
            "case_ids": payload.get("case_ids", []),
            "sql": payload.get("sql", ""),
            "reason": payload.get("reason", ""),
            "result_rows": payload.get("result_rows", 0),
        })
    return ok({"items": items, "total": len(items)})
