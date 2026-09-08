"""
server/app/routers/graph.py
W-P-004 关系图谱（只读语义层；未 BUILD 返回空图 available:false，不 500）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from server.app import graph_view
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["graph"])


@router.get("/cases/{case_id}/graph")
def get_graph(case_id: str,
              node_limit: int = Query(300, ge=1, le=1000),
              edge_limit: int = Query(500, ge=1, le=3000),
              p: Principal = Depends(get_principal),
              ctx: WebContext = Depends(get_ctx)):
    """实体关系图：节点=entity 型对象按度数采样，边=双侧带 ref 链接。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    data = {"available": False,
            "truncated": {"nodes": False, "edges": False, "dropped_edges": 0},
            "nodes": [], "edges": []}
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            data = graph_view.assemble_graph(
                conn=store.read_conn, pack=case.pack_id,
                base_dir=ctx.cases.snapshot_ontology_root(case_id),
                node_limit=node_limit, edge_limit=edge_limit)
        except FileNotFoundError:
            pass  # 未 BUILD（无版本文件）→ 空图
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))
