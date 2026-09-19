"""
server/app/routers/graph.py
W-P-004 关系图谱（只读语义层；未 BUILD 返回空图 available:false，不 500）。

P7：同一取数端点参数化——
  edge_kinds：逗号分隔 link 名白名单（资金链路图=transfers；
              关系图谱缺省全部），不新增取数 API；
  clue_id：仅高亮该线索；缺省时高亮最新产物中本角色可见的全部线索。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from core.access import can_see_jian_types

from server.app import graph_view
from server.app.clues_view import _load_raw
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.ontology_meta import jian_clearances
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["graph"])


def _parse_edge_kinds(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


@router.get("/cases/{case_id}/graph")
def get_graph(case_id: str,
              node_limit: int = Query(300, ge=1, le=1000),
              edge_limit: int = Query(500, ge=1, le=3000),
              edge_kinds: str | None = Query(
                  None, description="逗号分隔 link 名；缺省全部入图边"),
              clue_id: str | None = Query(
                  None, description="仅高亮该线索；缺省高亮全部可见线索"),
              p: Principal = Depends(get_principal),
              ctx: WebContext = Depends(get_ctx)):
    """实体关系图：节点=entity 型对象按度数采样，边=双侧带 ref 链接。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    data = {"available": False,
            "truncated": {"nodes": False, "edges": False, "dropped_edges": 0},
            "edge_kinds": [],
            "highlights": {"clue_count": 0, "nodes": 0, "edges": 0},
            "nodes": [], "edges": []}
    kinds = _parse_edge_kinds(edge_kinds)
    store = None
    state = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            # P8 人验图像证据（state.sqlite）——版本库不含 runtime 表，
            # 读 state 注入 assemble_graph（核验后自动入图）。
            from server.app.store.state_store import StateStore
            state = StateStore(case_id,
                               ctx.factory.case_dir(case_id) / "state.sqlite")
            image_rows = state.list_image_evidence(case_id=case_id)
            # 高亮线索来自案件线索产物（随版本不可变），按角色间类可见性过滤，
            # 不可见线索不进高亮（防泄漏）。
            snapshot_root = ctx.cases.snapshot_ontology_root(case_id)
            clearances = jian_clearances(case.pack_id, snapshot_root)
            raws, _ = _load_raw(ctx.factory.case_dir(case_id), None)
            highlight_clues: list[dict] = []
            for r in raws:
                if clue_id is not None and r.get("clue_id") != clue_id:
                    continue
                jts = r.get("jian_types") or []
                if not can_see_jian_types(jts, role=p.role,
                                          jian_clearances=clearances):
                    continue
                highlight_clues.append(r)
            data = graph_view.assemble_graph(
                conn=store.read_conn, pack=case.pack_id,
                base_dir=snapshot_root,
                node_limit=node_limit, edge_limit=edge_limit,
                edge_kinds=kinds or None,
                highlight_clues=highlight_clues,
                image_evidence_rows=image_rows)
        except FileNotFoundError:
            pass  # 未 BUILD（无版本文件）→ 空图
        except ValueError as exc:
            # 未声明 edge_kinds 等参数错误 = 请求方问题（400，不 500）
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if store is not None:
            store.close()
        if state is not None:
            state.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))
