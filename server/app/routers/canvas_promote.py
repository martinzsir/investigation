"""研判主体端点：证据提升（CAN-11/12）与主体名录（CAN-13）。

POST /cases/{cid}/canvas/promote    证据实体 → 研判主体
GET  /cases/{cid}/canvas/subjects   案件主体名录（供选择器）

为什么提升走 POST 带节点载荷
----------------------------
案件级画布尚无服务端持久化，服务端无法按 node_id 反查节点，只能由客户端
把 object 节点带过来。与 canvas/window 端点同一取舍。

语义层不可达时怎么办
--------------------
**照常处理，但状态如实标注**（conn=None → 各主体 pk_status='none' 或
退回节点自带键标 unverified），绝不返回 500 把整页打挂。正兵看到的是
"未连接语义层，无法核对"，而不是"提升失败"。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field

from server.app import canvas_promote as promote_mod
from server.app import canvas_subjects_view
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["canvas-subject"])

_MAX_NODES = 500


def _semantic_conn(ctx: WebContext, case_id: str) -> Any:
    """取语义层只读连接；任何失败返回 None（由调用方如实标注）。

    不准用 ``with``：CaseStore.__exit__ 会 close() 掉底层 duckdb 连接，
    本函数若从 with 块里把 read_conn 带出去，调用方拿到的是**已关闭连接**
    ——查询抛错被 canvas_subjects_view._rows 吞掉，名录静默为空，
    界面对任何名字都报「不在主体名录中」（张卫国明明在 obj_person）。
    与 routers/canvas_case.py 的 _case_read_conn 同径：工厂登记租约，
    读者连接由工厂统一回收。
    """
    try:
        version = ctx.repo.current_version(case_id)
        if not version:
            return None
        store = ctx.factory.for_case(case_id, mode="read", version=version)
        return getattr(store, "read_conn", None)
    except Exception:
        return None


class PromoteIn(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    requested: dict[str, str] = Field(default_factory=dict)


@router.post("/cases/{case_id}/canvas/promote")
def promote_objects(case_id: str, body: PromoteIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """把证据实体节点批量提升为研判主体（CAN-11），并给出提升边（CAN-12）。"""
    _get_owned_case(case_id, p, ctx.cases)
    nodes = list(body.nodes or [])
    if not nodes:
        raise APIError(ERR_VALIDATION,
                       "无可提升节点：请先选择证据实体节点"
                       "（交易、通话等非主体对象不支持提升）")
    if len(nodes) > _MAX_NODES:
        raise APIError(ERR_VALIDATION,
                       f"单次提升节点数不得超过 {_MAX_NODES}（当前 {len(nodes)} 个）")

    conn = _semantic_conn(ctx, case_id)
    res = promote_mod.promote_all(case_id=case_id, nodes=nodes, conn=conn,
                                  requested=dict(body.requested or {}))
    reports = res.get("reports") or []
    return ok({
        "nodes": res.get("nodes") or [],
        "edges": res.get("edges") or [],
        "reports": reports,
        "keeps_source": True,          # 原证据节点保留在溯源层
        "semantic_ready": conn is not None,
        "summary": {
            "promoted": sum(1 for r in reports if r.get("status") == "promoted"),
            "ambiguous": sum(1 for r in reports if r.get("status") == "ambiguous"),
            "unanchored": sum(1 for r in reports
                              if r.get("status") == "unanchored"),
            "rejected": sum(1 for r in reports if r.get("status") == "rejected"),
            "duplicate": sum(1 for r in reports
                             if r.get("status") == "duplicate"),
        },
    })


@router.get("/cases/{case_id}/canvas/subjects")
def list_subjects(case_id: str, q: str = Query(""),
                  limit: int = Query(50, ge=1, le=200),
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    """案件主体名录（CAN-13）：只给语义层已收录的主体，建出即锚定。"""
    _get_owned_case(case_id, p, ctx.cases)
    conn = _semantic_conn(ctx, case_id)
    try:
        res = canvas_subjects_view.list_subjects(
            conn=conn, q=q, limit=limit, case_id=case_id)
    except Exception as exc:                       # 名录读失败也要说得清
        return ok({"semantic_ready": False,
                   "reason": f"主体名录读取失败：{exc}", "subjects": []})
    res["semantic_ready"] = bool(conn is not None) and bool(
        res.get("semantic_ready"))
    return ok(res)
