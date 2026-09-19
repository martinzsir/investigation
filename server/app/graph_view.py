"""
server/app/graph_view.py
W-P-004 关系图谱组装（只读语义层 obj_*/lnk_*，不直读 Parquet）。

口径（M6_plan W-P-004；P7 参数化）：
  - 节点 = kind=entity 且非 runtime 的对象（person/account/org/...），
    每类取 pk+name_property，按度数（边计数）排序取 top node_limit；
  - 边 = endpoints 双侧带 ref 的链接（无 ref 的如 time_window 跳过），
    SELECT * FROM lnk_<name>；端点不在节点集则补
    id-only 节点（计入 node 上限），超出上限的边计入 dropped_edges；
  - 节点 id 与边端点同域："<obj>:<pk>"；
  - edge_kinds 参数：link 名白名单投影（资金链路图=["transfers"]；
    关系图谱=None 全部或子集），同一后端同一节点口径，不另建取数；
  - highlight_clues：携带 evidence_refs 的线索 dict 列表，被引用的
    节点/边标 hit + clue_ids（可溯源）；边引用 lnk_<name>#<edge_pk>
    与该行任意列值匹配（边表无统一行键声明）；
  - 表缺失/未 BUILD → available:false 空图，不抛错。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.ontology_loader import load_pack

_MAX_FETCH_PER_TYPE = 2000  # 单类节点探测上限（防大表全扫；度数排序在池内进行）

# 边上可携带进 DTO 的附加属性白名单（来自 build_sql 实际输出列）
_EDGE_PROP_WHITELIST = ("amount", "date")


def _table_exists(conn, table: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table]).fetchall()
    return bool(rows)


def _q(name: str) -> str:
    """标识符引用（表名/列名均来自 loader 校验过的声明，仅含安全字符）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _node_id_from_ref(ref: str) -> str | None:
    """evidence_refs 节点引用 → 图节点 id。obj_person#p1 → person:p1。"""
    if not isinstance(ref, str) or not ref.startswith("obj_") or "#" not in ref:
        return None
    tbl, key = ref[4:].split("#", 1)
    if not tbl or not key:
        return None
    return f"{tbl}:{key}"


def _build_highlight_index(
    highlight_clues: list[dict] | None,
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, str]]], int]:
    """线索 evidence_refs → (节点 id→clue_ids, link 名→[(edge_pk, clue_id)], 线索数)。"""
    hl_nodes: dict[str, list[str]] = {}
    hl_edges: dict[str, list[tuple[str, str]]] = {}
    clue_ids: set[str] = set()
    for c in highlight_clues or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("clue_id") or "")
        if cid:
            clue_ids.add(cid)
        for r in c.get("evidence_refs") or []:
            if not isinstance(r, dict):
                continue
            kind = r.get("kind")
            ref = r.get("ref") or ""
            if kind == "node":
                nid = _node_id_from_ref(ref)
                if nid is not None:
                    hl_nodes.setdefault(nid, [])
                    if cid and cid not in hl_nodes[nid]:
                        hl_nodes[nid].append(cid)
            elif kind in ("edge", "time_window"):
                # lnk_transfers#edge_pk → (transfers, edge_pk)
                if isinstance(ref, str) and ref.startswith("lnk_") and "#" in ref:
                    name, pk = ref[4:].split("#", 1)
                    if name and pk:
                        hl_edges.setdefault(name, []).append((pk, cid))
    return hl_nodes, hl_edges, len(clue_ids)


def assemble_graph(*, conn, pack: str, base_dir: str | Path,
                   node_limit: int = 300, edge_limit: int = 500,
                   edge_kinds: list[str] | None = None,
                   highlight_clues: list[dict] | None = None,
                   image_evidence_rows: list[dict] | None = None) -> dict[str, Any]:
    """组装图谱视图。conn=CaseStore.read_conn（DuckDB 只读连接）。

    edge_kinds: 仅保留这些 link 名的边；None=全部入图链接。
    highlight_clues: 线索 dict（含 evidence_refs），命中节点/边打高亮。
    image_evidence_rows: P8 state.sqlite 人验通过的图像证据行——版本库不含
        runtime 表，由 router 从 StateStore.list_image_evidence 取来注入；
        合成 image_evidence 节点 + image_for_* 边（核验后自动入图）。
    """
    empty = {"available": False,
             "truncated": {"nodes": False, "edges": False, "dropped_edges": 0},
             "edge_kinds": [],
             "highlights": {"clue_count": 0, "nodes": 0, "edges": 0},
             "nodes": [], "edges": []}
    try:
        spec = load_pack(pack, base_dir=Path(base_dir))
    except (FileNotFoundError, ValueError):
        return empty

    # edge_kinds 必须是已声明 link（未声明参数不得下发）
    declared_link_names = {lk.name for lk in spec.links}
    if edge_kinds is not None:
        unknown = sorted(set(edge_kinds) - declared_link_names)
        if unknown:
            raise ValueError(f"edge_kinds 未在 links 声明：{unknown}")

    hl_nodes, hl_edges, hl_clue_count = _build_highlight_index(
        highlight_clues)

    # P6：节点间类装饰来自五间词汇映射（不再读 o.jian）
    from core.wujian import load_wujian
    wj = load_wujian(pack)
    type_to_jians = wj.type_to_jians if wj is not None else {}

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
        type_jian[o.name] = type_to_jians.get(o.name, [])
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
                "jian": type_to_jians.get(o.name, []),
                "hit": nid in hl_nodes,
                "clue_ids": hl_nodes.get(nid, [])}

    if not present_types:
        return empty

    # ---- 2) 边行：仅双侧带 ref 且表存在的链接 ----
    degree: dict[str, int] = {}
    edge_rows: list[dict] = []   # {source, target, label, type, hit, clue_ids, props}
    edge_kind_catalog: list[dict] = []
    for lk in spec.links:
        if lk.runtime:
            continue
        eps = lk.endpoints or {}
        f_ep, t_ep = eps.get("from") or {}, eps.get("to") or {}
        f_ref, t_ref = f_ep.get("ref"), t_ep.get("ref")
        if not f_ref or not t_ref:
            continue  # 端点无对象引用（如 time_window/owns 单侧）→ 跳过
        tbl = f"lnk_{lk.name}"
        present = _table_exists(conn, tbl)
        edge_kind_catalog.append({"name": lk.name, "title": lk.title,
                                  "present": present})
        if not present:
            continue
        if edge_kinds is not None and lk.name not in edge_kinds:
            continue
        # SELECT * 取全行：端点外的列值用于 edge_pk 匹配与附加属性
        try:
            res = conn.execute(
                f"SELECT * FROM {_q(tbl)} WHERE {_q(f_ep['col'])} IS NOT NULL "
                f"AND {_q(t_ep['col'])} IS NOT NULL LIMIT {edge_limit}"
            )
            col_names = [d[0] for d in (res.description or [])]
            raw_rows = res.fetchall()
        except Exception:
            continue
        wanted = hl_edges.get(lk.name, [])
        for vals in raw_rows:
            row = dict(zip(col_names, vals))
            fv, tv = row.get(f_ep["col"]), row.get(t_ep["col"])
            if fv is None or tv is None:
                continue
            sid = f"{f_ref['object']}:{fv}"
            tid = f"{t_ref['object']}:{tv}"
            # edge_pk 与该行任意列值匹配 → 命中
            hit_ids: list[str] = []
            if wanted:
                cell_values = {str(v) for v in row.values() if v is not None}
                for pk_val, cid in wanted:
                    if pk_val in cell_values and cid and cid not in hit_ids:
                        hit_ids.append(cid)
            props = {k: str(row[k]) for k in _EDGE_PROP_WHITELIST
                     if k in row and row[k] is not None}
            edge_rows.append({"source": sid, "target": tid,
                              "label": lk.title, "type": lk.name,
                              "hit": bool(hit_ids), "clue_ids": hit_ids,
                              "props": props})
            degree[sid] = degree.get(sid, 0) + 1
            degree[tid] = degree.get(tid, 0) + 1

    # ---- 2.5) P8 图像证据（state.sqlite 人验产物）合成节点/边 ----
    # 版本库不含 runtime 表：图像节点/边由 router 从 state 注入后在此合并，
    # 与普通边同走度数排序/端点补节点/上限逻辑（核验后自动入图）。
    _SUBJECT_LINK_NAME = {"person": "image_for_person",
                          "org": "image_for_org",
                          "bid_project": "image_for_project"}
    image_present_kinds: set[str] = set()
    for r in image_evidence_rows or []:
        stype = str(r.get("subject_type") or "").strip()
        link_name = _SUBJECT_LINK_NAME.get(stype)
        if not link_name:
            continue
        if edge_kinds is not None and link_name not in edge_kinds:
            continue
        iid = str(r.get("image_evidence_id") or "")
        subject_id = str(r.get("subject_id") or "").strip()
        if not iid or not subject_id:
            continue
        nid = f"image_evidence:{iid}"
        uri = str(r.get("image_uri") or "")
        label = Path(uri).name if uri else iid
        candidates.setdefault(nid, {
            "id": nid, "label": label, "type": "image_evidence",
            "type_title": "图像证据", "jian": [], "hit": False,
            "clue_ids": []})
        edge_rows.append({"source": nid,
                          "target": f"{stype}:{subject_id}",
                          "label": "图像证据指向", "type": link_name,
                          "hit": False, "clue_ids": [], "props": {}})
        image_present_kinds.add(link_name)
    for link_name in sorted(image_present_kinds):  # 补 catalog（runtime 边）
        edge_kind_catalog.append({"name": link_name,
                                  "title": "图像证据指向",
                                  "present": True})

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
                              "jian": type_jian.get(obj_name, []),
                              "hit": ep in hl_nodes,
                              "clue_ids": hl_nodes.get(ep, [])})
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
        "edge_kinds": edge_kind_catalog,
        "highlights": {"clue_count": hl_clue_count,
                       "nodes": sum(1 for n in nodes if n["hit"]),
                       "edges": sum(1 for e in edges if e["hit"])},
        "nodes": nodes,
        "edges": edges[:edge_limit],
    }
