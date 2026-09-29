"""节点窗口端点。

POST /cases/{cid}/canvas/window

为什么是 POST 而不是 GET
------------------------
端点需要节点的 kind/props（lens_id、observation_id、dims）才能定位支撑观察，
而**案件级画布目前没有服务端持久化**（canvas_case 只提供构造层，不落盘），
服务端无从按 node_id 反查节点。所以由客户端把节点载荷带过来。

GET 也可行（把 lens_id/dims 摊成 query），但每加一个匹配条件就要改一次
签名；POST 让匹配口径随节点演进而不用动路由。

只有 evidence 窗口需要它
------------------------
关系/地图/时间三个窗口读画布内存数据（边、坐标、时刻），本端点对非
analysis_result 节点直接返回 server_sourced=false，不发无谓请求。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query

from server.app import canvas_window_view
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["canvas-window"])


@router.post("/cases/{case_id}/canvas/window")
def node_window(case_id: str,
                body: dict = Body(default_factory=dict),
                support_limit: int = Query(20, ge=1, le=200),
                fact_limit: int = Query(5, ge=1, le=50),
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """按节点装配窗口数据；只有研判结论节点需要回查观察档案。"""
    _get_owned_case(case_id, p, ctx.cases)
    node = body.get("node") if isinstance(body, dict) else None
    if not isinstance(node, dict) or not node:
        raise APIError(ERR_VALIDATION, "缺少 node 载荷（窗口需要节点 kind/props）", 400)
    if not isinstance(node.get("props"), dict):
        node = dict(node)
        node["props"] = {}
    version = ctx.repo.current_version(case_id)
    data = canvas_window_view.assemble_window(
        case_dir=ctx.factory.case_dir(case_id), version=version,
        node=node, node_id=str(node.get("id") or ""),
        support_limit=support_limit, fact_limit=fact_limit,
    )
    return ok(data)
