"""
server/app/graph_view.py
W-P-004 关系图谱组装（只读语义层 obj_*/lnk_*，不直读 Parquet）。

口径（M6_plan W-P-004）：
  - 节点 = kind=entity 且非 runtime 的对象（person/account/org/...），
    每类取 pk+name_property，按度数（边计数）排序取 top node_limit；
  - 边 = endpoints 双侧带 ref 的链接（无 ref 的如 time_window 跳过），
    SELECT <from.col>,<to.col> FROM lnk_<name>；端点不在节点集则补
    id-only 节点（计入 node 上限），超出上限的边计入 dropped_edges；
  - 节点 id 与边端点同域："<obj>:<pk>"；
  - 表缺失/未 BUILD → available:false 空图，不抛错。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.ontology_loader import load_pack

_MAX_FETCH_PER_TYPE = 2000  # 单类节点探测上限（防大表全扫；度数排序在池内进行）


def _table_exists(conn, table: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table]).fetchall()
    return bool(rows)


def _q(name: str) -> str:
    """标识符引用（表名/列名均来自 loader 校验过的声明，仅含安全字符）。"""
    return '"' + str(name).replace('"', '""') + '"'


def assemble_graph(*, conn, pack: str, base_dir: str | Path,
                   node_limit: int = 300, edge_limit: int = 500) -> dict[str, Any]:
    """组装图谱视图。conn=CaseStore.read_conn（DuckDB 只读连接）。"""
    empty = {"available": False,
             "truncated": {"nodes": False, "edges": False, "dropped_edges": 0},
             "nodes": [], "edges": []}
    try:
        spec = load_pack(pack, base_dir=Path(base_dir))
    except (FileNotFoundError, ValueError):
        return empty

    entity_objs = [o for o in spec.objects
                   if o.kind == "entity" and not o.runtime]

    # ---- 1) 候选节点：每类取 pk + name_property（表存在才取）----
    candidates: dict[str, dict] = {}   # id → node dict
    type_title: dict[str, str] = {}
    type_jian: dict[str, list[str]] = {}
    present_types: list[str] = []
    for o in entity_objs:
        tbl = f"obj_{o.name}"
        if not _table_exists(conn, tbl):
            continue
        present_types.append(o.name)
        type_title[o.name] = o.title
        type_jian[o.name] = [o.jian] if o.jian else []
        cols = f"{_q(o.pk)} AS pk, {_q(o.name_property)} AS label"
        try:
            rows = conn.execute(
                f"SELECT {cols} FROM {_q(tbl)} LIMIT {_MAX_FETCH_PER_TYPE}"
            ).fetchall()
        except Exception:
            continue
        for pk, label in rows:
            if pk is None:
                continue
            nid = f"{o.name}:{pk}"
            candidates[nid] = {
                "id": nid, "label": str(label) if label is not None else str(pk),
                "type": o.name, "type_title": o.title,
                "jian": [o.jian] if o.jian else []}

    if not present_types:
        return empty

    # ---- 2) 边行：仅双侧带 ref 且表存在的链接 ----
    degree: dict[str, int] = {}
    edge_rows: list[dict] = []   # {source, target, label, type}
    for lk in spec.links:
        if lk.runtime:
            continue
        eps = lk.endpoints or {}
        f_ep, t_ep = eps.get("from") or {}, eps.get("to") or {}
        f_ref, t_ref = f_ep.get("ref"), t_ep.get("ref")
        if not f_ref or not t_ref:
            continue  # 端点无对象引用（如 time_window/owns 单侧）→ 跳过
        tbl = f"lnk_{lk.name}"
        if not _table_exists(conn, tbl):
            continue
        try:
            rows = conn.execute(
                f"SELECT {_q(f_ep['col'])} AS f, {_q(t_ep['col'])} AS t "
                f"FROM {_q(tbl)} WHERE {_q(f_ep['col'])} IS NOT NULL "
                f"AND {_q(t_ep['col'])} IS NOT NULL LIMIT {edge_limit}"
            ).fetchall()
        except Exception:
            continue
        for fv, tv in rows:
            if fv is None or tv is None:
                continue
            sid = f"{f_ref['object']}:{fv}"
            tid = f"{t_ref['object']}:{tv}"
            edge_rows.append({"source": sid, "target": tid,
                              "label": lk.title, "type": lk.name})
            degree[sid] = degree.get(sid, 0) + 1
            degree[tid] = degree.get(tid, 0) + 1

    # ---- 3) 节点按度数排序取 top node_limit ----
    ordered = sorted(candidates.values(),
                     key=lambda n: (-degree.get(n["id"], 0), n["id"]))
    nodes = ordered[:node_limit]
    node_ids = {n["id"] for n in nodes}
    nodes_truncated = len(ordered) > node_limit

    # ---- 4) 边发射：端点缺失则补 id-only 节点（计入上限），超上限丢弃 ----
    edges: list[dict] = []
    dropped = 0
    for e in edge_rows:
        budget_hit = False
        for ep in (e["source"], e["target"]):
            if ep not in node_ids:
                if len(nodes) >= node_limit:
                    budget_hit = True
                    break
                obj_name = ep.split(":", 1)[0]
                nodes.append({"id": ep, "label": ep.split(":", 1)[-1],
                              "type": obj_name,
                              "type_title": type_title.get(obj_name, obj_name),
                              "jian": type_jian.get(obj_name, [])})
                node_ids.add(ep)
        if budget_hit:
            dropped += 1
            continue
        if len(edges) >= edge_limit:
            dropped += 1  # 边上限：后续边全部计为丢弃
            continue
        edges.append(e)

    return {
        "available": True,
        "truncated": {"nodes": nodes_truncated,
                      "edges": dropped > 0,
                      "dropped_edges": dropped},
        "nodes": nodes,
        "edges": edges[:edge_limit],
    }
