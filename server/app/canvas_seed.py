"""
server/app/canvas_seed.py
线索研判画布种子成图（RC-101，后端纯函数，可单测）。

输入 assemble_detail 产物（detail 三栏/溯源视图 + state 核查项/书证），
输出 CanvasDoc（nodes/edges，前后端共享契约见 PRD 第五章）：

  rule ──命中──▶ fact ──来源行──▶ source_row
   │                 （M1 对象层跳过：fact 直连数据行保连通；
   │                  M2 expand 接入后改为 fact-涉及-object-来源行-row）
   └──手册建议──▶ verify_item ──挂接──▶ evidence(书证)

纪律：
  - 纯函数、不开库、不读 Parquet、不生成 SQL；幂等（同输入同输出）；
  - 系统节点 id = "<kind>:<ref>"，全部 system=true；
  - fact ref = sha1('{clue_id}|fact|{序号}')[:12]（PRD：clue_id+序号哈希）；
  - 数据行无 row_uri 时与 evidence_builder._make_source_ref 同口径 local URI；
  - 无规则 → 占位「未关联规则（历史产物）」；无 source_rows 不抛异常；
  - M1 不生成 object/source_file 层（M2 经 expand 懒加载接入）。

PATCH 整文档校验（RC-205/AC-202 前置）：M1 只允许系统节点坐标/钉住变动，
节点集合、系统边、系统节点 ref/kind/label/props 一律不可变；人工节点/边
端点在 M3 开放后在此扩校验。
"""
from __future__ import annotations

import hashlib
from typing import Any

from server.app.evidence_builder import _extract_rules, _make_source_ref

NODE_KINDS = frozenset({
    "rule", "fact", "object", "source_row", "source_file",
    "verify_item", "evidence", "hypothesis", "note", "function_result",
})

# 系统边关系枚举（人工 4 类：推断为/证实/查否/补充说明，M3）
SYSTEM_RELS = frozenset({
    "命中", "涉及", "来源行", "所属文件", "手册建议", "挂接", "查询自",
})

# 左→右分层列坐标（PRD：规则 0/事实 240/对象 480/数据行 720/文件 960；
# 待核实挂事实列下侧，书证挂对象列）
_X_RULE = 0
_X_FACT = 260
_X_EVIDENCE = 480
_X_ROW = 720
_Y_GAP = 104
_LABEL_LIMIT = 28

_HISTORICAL_RULE_ID = "unlinked"
_HISTORICAL_RULE_LABEL = "未关联规则（历史产物）"

# ----------------------------------------------------------------------
# 节点规模上限（画布成图保护）
# ----------------------------------------------------------------------
# 真实案件单条线索的溯源行可达数千、语义层主体可达上万（实测候选枚举 2.5w）。
# 全量成图会拖垮前端渲染，且人眼根本无法在几千个节点里做研判——画布的价值是
# **关系概览**，完整明细应去溯源抽屉看。
#
# 口径：截断而非丢弃——未成图的行仍可展开补齐（只增不改删，与 reconcile 同源）；
# meta 如实声明 shown/total，不允许"静默少画"让用户误以为数据只有这些。
_MAX_ROW_NODES = 60      # source_row 首屏上限
_MAX_FACT_NODES = 40     # fact 首屏上限
_ROW_EXPAND_STEP = 120   # 每次"展开更多"追加到的上限


def sys_node_id(kind: str, ref: str) -> str:
    """系统节点画布内稳定 id。"""
    return f"{kind}:{ref}"


def fact_ref(clue_id: str, idx: int) -> str:
    """事实节点业务锚点：clue_id+事实序号哈希（PRD 种子映射表）。"""
    return hashlib.sha1(
        f"{clue_id}|fact|{idx}".encode("utf-8")).hexdigest()[:12]


def seed_canvas(*, clue_id: str, detail: dict[str, Any],
                evidence: list[dict[str, Any]],
                verify_items: list[dict[str, Any]] | None = None,
                materials: list[dict[str, Any]] | None = None,
                pack_id: str = "default", base_dir=None,
                row_limit: int | None = None,
                fact_limit: int | None = None,
                ) -> dict[str, Any]:
    """（续参数说明）
      row_limit    : source_row 成图上限（缺省 _MAX_ROW_NODES）
      fact_limit   : fact 成图上限（缺省 _MAX_FACT_NODES）

    返回 doc 含 **meta 键**（截断声明，路由层取出后单独下发、不进用户可编辑
    的 nodes/edges）：{"truncated": {"source_row": {"shown":n,"total":m}, ...}}。
    meta 不参与 validate_doc_shape（该校验只看 nodes/edges）。
    """
    """把线索详情/三栏证据/核查项/书证 → 初始 CanvasDoc。

    参数：
      clue_id      : 线索 ID（fact 稳定键域）
      detail       : assemble_detail 产物（含 detail 原始判据、source_rows）
      evidence     : build_evidence 三栏列表
      verify_items : state.list_verify_items（含建议/已采纳/人工项）
      materials    : state.list_evidence(clue_id)（已挂接书证）
      pack_id      : ontology 案件包名（数据行登记源反查用）
      base_dir     : 案件快照 ontology 根
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()

    def add_edge(source: str, target: str, rel: str) -> None:
        eid = f"e:{source}--{rel}--{target}"
        if eid in seen_edges:
            return
        seen_edges.add(eid)
        edges.append({
            "id": eid, "source": source, "target": target,
            "rel": rel, "system": True,
        })

    det = detail.get("detail") or {}
    rules = _extract_rules(det, detail)

    # ---- 规则层 ----
    rule_ids: list[str] = []
    if rules:
        for r in rules:
            rid = r["rule_id"]
            node_id = sys_node_id("rule", rid)
            rule_ids.append(node_id)
            nodes.append({
                "id": node_id, "kind": "rule", "ref": rid,
                "label": f"规则 {rid}", "system": True, "pinned": False,
                "props": {
                    "rule_id": rid,
                    "rule_text": r["rule_text"],
                    "basis": r["basis"],
                },
            })
    else:
        node_id = sys_node_id("rule", _HISTORICAL_RULE_ID)
        rule_ids.append(node_id)
        nodes.append({
            "id": node_id, "kind": "rule",
            "ref": _HISTORICAL_RULE_ID,
            "label": _HISTORICAL_RULE_LABEL,
            "system": True, "pinned": False,
            "props": {"historical": True, "rule_text": det.get("rule_text", ""),
                      "basis": str(detail.get("basis") or "")},
        })

    # ---- 数据行层（以 source_rows 为准，与三栏事实同源；去重 by row_uri）----
    row_specs = _row_specs(clue_id, detail, evidence, pack_id=pack_id,
                           base_dir=base_dir)
    # 规模保护：溯源行是唯一可能爆量的层（实测可达数千）。截断后如实声明
    # shown/total——不静默少画，让用户知道"还有更多、去溯源抽屉看全量"。
    row_total = len(row_specs)
    _row_cap = int(row_limit) if row_limit and row_limit > 0 else _MAX_ROW_NODES
    shown_rows = row_specs[:_row_cap]
    row_ids: dict[str, str] = {}
    row_y_index: dict[str, int] = {}
    for spec in shown_rows:
        uri = spec["row_uri"]
        node_id = sys_node_id("source_row", uri)
        row_ids[uri] = node_id
        row_y_index[uri] = len(row_y_index)
        nodes.append({
            "id": node_id, "kind": "source_row", "ref": uri,
            "label": _row_label(spec), "system": True, "pinned": False,
            "props": {
                "row_uri": uri, "source": spec["source"],
                "granularity": spec.get("granularity") or "",
                # 业务发生时间（本体 semantic:event_time 声明的字段值）。
                # 空串 = 该行无业务时间（本体未声明或该行缺值）→
                # 业务时间轴应把它归入「无时间」档，而不是猜一个时间。
                "event_time": spec.get("event_time") or "",
                "registered": bool(spec.get("registered", False)),
            },
        })

    # ---- 事实层（evidence.fact 栏，按栏内序号给稳定 ref）----
    facts = [it for it in evidence if it.get("kind") == "fact"]
    fact_total = len(facts)
    _fact_cap = int(fact_limit) if fact_limit and fact_limit > 0 else _MAX_FACT_NODES
    shown_facts = facts[:_fact_cap]
    fact_ids: list[str] = []
    for idx, f in enumerate(shown_facts):
        ref = fact_ref(clue_id, idx)
        node_id = sys_node_id("fact", ref)
        fact_ids.append(node_id)
        text = str(f.get("text") or "")
        nodes.append({
            "id": node_id, "kind": "fact", "ref": ref,
            "label": _truncate(text) or "事实",
            "system": True, "pinned": False,
            "props": {
                "text": text,
                # 存下引用的行 uri：溯源行被截断时 fact-[来源行]→row 边会跳过，
                # 展开补齐后 expand_canvas_rows 据此重连（只增不改删）
                "source_rows_uris": [
                    str((sr or {}).get("row_uri") or "")
                    for sr in (f.get("source_rows") or [])
                    if str((sr or {}).get("row_uri") or "")
                ],
            },
        })
        # rule -[命中]-> fact（含历史占位规则；无规则线索也保留归属链路）
        for rid in rule_ids:
            add_edge(rid, node_id, "命中")
        # M1 对象层跳过：fact 直连数据行（rel=来源行），保证 AC-101 连通性；
        # M2 对象层接入后该直连边由 fact-涉及-object-来源行-row 替代
        for sr in f.get("source_rows") or []:
            uri = str((sr or {}).get("row_uri") or "")
            target = row_ids.get(uri)
            if target is not None:
                add_edge(node_id, target, "来源行")

    # ---- 核查项层（已采纳/人工；M4 RC-105：未采纳建议不自动成节点）----
    # status=建议 的手册建议项仅在用户点「生成手册核实建议」后以
    # ref=pb:<playbook_id> 的虚线虚节点出现（见 server/app/canvas_infer.py），
    # seed 阶段不渲染——未采纳建议仅画布显式生成、不进核查工作台（AC-105-2）。
    item_nodes: dict[str, str] = {}
    for it in verify_items or []:
        item_id = str(it.get("item_id") or "")
        if not item_id:
            continue
        text = str(it.get("text") or "")
        status = str(it.get("status") or "")
        if status == "建议":
            continue
        kind = str(it.get("kind") or "")
        node_id = sys_node_id("verify_item", item_id)
        item_nodes[item_id] = node_id
        nodes.append({
            "id": node_id, "kind": "verify_item", "ref": item_id,
            "label": _truncate(text) or "待核实",
            "system": True, "pinned": False,
            "adopted": True,
            "props": {
                "text": text, "status": status, "kind": kind,
                "origin": str(it.get("origin") or ""),
                "channel": str(it.get("channel") or ""),
                "ref_function": str(it.get("ref_function") or ""),
            },
        })

    # ---- 书阶层（state clue_evidence；挂接 item_id → verify_item-[挂接]）----
    for m in materials or []:
        mid = str(m.get("material_id") or "")
        if not mid:
            continue
        node_id = sys_node_id("evidence", mid)
        nodes.append({
            "id": node_id, "kind": "evidence", "ref": mid,
            "label": _truncate(str(m.get("orig_name") or mid)),
            "system": True, "pinned": False,
            "props": {
                "material_id": mid,
                "material_type": str(m.get("material_type") or ""),
                "uploaded_by": str(m.get("uploaded_by") or ""),
                "uploaded_at": str(m.get("uploaded_at") or ""),
            },
        })
        owner = str(m.get("item_id") or "")
        if owner and owner in item_nodes:
            add_edge(item_nodes[owner], node_id, "挂接")

    _apply_positions(nodes)
    # meta：截断声明（路由层取出单独下发，doc 存储时可一并保留以便 GET 回读
    # 仍知情；validate_doc_shape 只校验 nodes/edges，不受影响）
    meta = {
        "truncated": {
            "source_row": {"shown": len(shown_rows), "total": row_total},
            "fact": {"shown": len(shown_facts), "total": fact_total},
        },
        "limits": {"row_limit": _row_cap, "fact_limit": _fact_cap},
        # 完整明细的去处——画布只做关系概览，全量溯源行在详情抽屉
        "hint": "画布仅渲染关系概览；完整溯源行请见线索详情的溯源抽屉",
    }
    return {"nodes": nodes, "edges": edges, "meta": meta}


def expand_canvas_rows(doc: dict[str, Any], *,
                       row_specs: list[dict[str, Any]],
                       row_limit: int | None = None,
                       ) -> tuple[dict[str, Any], int, int]:
    """按需展开溯源行：把未成图的 row 补进画布（只增不改删）。

    与 reconcile_canvas 同源口径——已存在的节点/边/坐标一律不动，只追加缺失的
    source_row 节点，并重连 fact-[来源行]→row 边（截断时被跳过、现已可连的边）。

    参数 row_specs 须为**全量**（未截断）行清单，由调用方用与 seed 相同的
    `_row_specs()` 口径重算，否则展开出来的不是同一批行。

    返回 (doc, 新增节点数, 新增边数)；无缺失时原 doc 原样返回。
    """
    nodes = list(doc.get("nodes") or [])
    edges = list(doc.get("edges") or [])
    node_ids = {str(n.get("id") or "") for n in nodes}
    edge_ids = {str(e.get("id") or "") for e in edges}

    cap = int(row_limit) if row_limit and row_limit > 0 else _ROW_EXPAND_STEP
    # 已画的行（按 ref 反查），避免重复加
    existing_refs = {
        str(n.get("ref") or "")
        for n in nodes if n.get("kind") == "source_row"
    }

    added_nodes = 0
    added_edges = 0
    # 目标：把已画行数补到 cap（不是再加 cap 个）
    room = max(0, cap - len(existing_refs))
    if room <= 0:
        return doc, 0, 0

    row_ids: dict[str, str] = {
        str(n.get("ref") or ""): str(n.get("id") or "")
        for n in nodes if n.get("kind") == "source_row"
    }
    # 新节点纵轴从同列已有节点数继续排（不动既有节点坐标——"只增不改"）
    row_y = sum(1 for n in nodes if n.get("kind") == "source_row")
    for spec in row_specs:
        if room <= 0:
            break
        uri = str(spec.get("row_uri") or "")
        if not uri or uri in existing_refs:
            continue
        node_id = sys_node_id("source_row", uri)
        if node_id in node_ids:
            continue
        nodes.append({
            "id": node_id, "kind": "source_row", "ref": uri,
            "label": _row_label(spec), "system": True, "pinned": False,
            # x/y 必填（validate_doc_shape 校验），与 _apply_positions 同口径
            "x": _X_ROW, "y": row_y * _Y_GAP,
            "props": {
                "row_uri": uri, "source": spec.get("source") or "未知数据源",
                "granularity": str(spec.get("granularity") or ""),
                "registered": bool(spec.get("registered", False)),
                # 与 seed 同口径：补齐的行也要带业务时间，否则展开出来的
                # 行在业务时间轴上会全部落进「无时间」档。
                "event_time": str(spec.get("event_time") or ""),
            },
        })
        row_y += 1
        node_ids.add(node_id)
        row_ids[uri] = node_id
        added_nodes += 1
        room -= 1

    # 补边：fact-[来源行]→row（截断时这些边被跳过，现在端点齐了可补）
    if added_nodes:
        for n in nodes:
            if n.get("kind") != "fact":
                continue
            for uri in (n.get("props") or {}).get("source_rows_uris") or []:
                target = row_ids.get(str(uri))
                if not target:
                    continue
                eid = f"e:{n['id']}--来源行--{target}"
                if eid in edge_ids:
                    continue
                edge_ids.add(eid)
                edges.append({"id": eid, "source": n["id"],
                              "target": target, "rel": "来源行",
                              "system": True})
                added_edges += 1

    if not added_nodes:
        return doc, 0, 0

    doc = dict(doc)
    doc["nodes"] = nodes
    doc["edges"] = edges
    # 同步 meta：shown 增加，total 不变（仍是全量行数）
    meta = doc.get("meta")
    if isinstance(meta, dict):
        meta = dict(meta)
        trunc = dict(meta.get("truncated") or {})
        sr = dict(trunc.get("source_row") or {})
        sr["shown"] = len(row_ids)
        sr["total"] = max(int(sr.get("total") or 0), len(row_specs))
        trunc["source_row"] = sr
        meta["truncated"] = trunc
        meta["limits"] = {**(meta.get("limits") or {}), "row_limit": cap}
        doc["meta"] = meta
    return doc, added_nodes, added_edges


def reconcile_canvas(doc: dict[str, Any], *,
                     verify_items: list[dict[str, Any]] | None = None,
                     materials: list[dict[str, Any]] | None = None,
                     ) -> tuple[dict[str, Any], int, int]:
    """GET 幂等增量补种：补齐 seed 后新出现的 verify_item/evidence 节点
    与 verify_item-[挂接]→evidence 边。

    只增不改删：节点/边形状与过滤口径（status=建议 不成节点，AC-105-2）
    与 seed_canvas 同源；不动既有节点/边/坐标，不重排。新节点 y 按 doc
    内同列已有节点计数排布（fact/verify_item 共享 _fact_col 口径）。
    返回 (doc, 新增节点数, 新增边数)；无缺失时原 doc 原样返回。
    """
    nodes = list(doc.get("nodes") or [])
    edges = list(doc.get("edges") or [])
    node_ids = {str(n.get("id") or "") for n in nodes}
    edge_ids = {str(e.get("id") or "") for e in edges}

    added_nodes = 0
    added_edges = 0
    fact_col = sum(1 for n in nodes
                   if n.get("kind") in ("fact", "verify_item"))
    ev_col = sum(1 for n in nodes if n.get("kind") == "evidence")

    # ---- 缺失核查项（同 seed：已采纳/人工；建议不成节点）----
    for it in verify_items or []:
        item_id = str(it.get("item_id") or "")
        if not item_id:
            continue
        node_id = sys_node_id("verify_item", item_id)
        if node_id in node_ids or str(it.get("status") or "") == "建议":
            continue
        text = str(it.get("text") or "")
        nodes.append({
            "id": node_id, "kind": "verify_item", "ref": item_id,
            "label": _truncate(text) or "待核实",
            "system": True, "pinned": False,
            "x": _X_FACT, "y": fact_col * _Y_GAP,
            "adopted": True,
            "props": {
                "text": text, "status": str(it.get("status") or ""),
                "kind": str(it.get("kind") or ""),
                "origin": str(it.get("origin") or ""),
                "channel": str(it.get("channel") or ""),
                "ref_function": str(it.get("ref_function") or ""),
            },
        })
        node_ids.add(node_id)
        fact_col += 1
        added_nodes += 1

    # ---- 缺失书证 + 挂接边（两端点均在 doc 才补，同 seed 口径）----
    for m in materials or []:
        mid = str(m.get("material_id") or "")
        if not mid:
            continue
        node_id = sys_node_id("evidence", mid)
        if node_id not in node_ids:
            nodes.append({
                "id": node_id, "kind": "evidence", "ref": mid,
                "label": _truncate(str(m.get("orig_name") or mid)),
                "system": True, "pinned": False,
                "x": _X_EVIDENCE, "y": ev_col * _Y_GAP,
                "props": {
                    "material_id": mid,
                    "material_type": str(m.get("material_type") or ""),
                    "uploaded_by": str(m.get("uploaded_by") or ""),
                    "uploaded_at": str(m.get("uploaded_at") or ""),
                },
            })
            node_ids.add(node_id)
            ev_col += 1
            added_nodes += 1
        owner = str(m.get("item_id") or "")
        if not owner:
            continue
        src = sys_node_id("verify_item", owner)
        if src not in node_ids:
            continue
        eid = f"e:{src}--挂接--{node_id}"
        if eid in edge_ids:
            continue
        edges.append({"id": eid, "source": src, "target": node_id,
                      "rel": "挂接", "system": True})
        edge_ids.add(eid)
        added_edges += 1

    if not added_nodes and not added_edges:
        return doc, 0, 0
    return {"nodes": nodes, "edges": edges}, added_nodes, added_edges


# ----------------------------------------------------------------------
# PATCH 校验（M1：只允许系统节点坐标/钉住变动）
# ----------------------------------------------------------------------
def validate_doc_shape(doc: Any) -> list[str]:
    """文档结构基础校验，返回错误信息列表（空=通过）。"""
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["画布文档必须是对象 {nodes, edges}"]
    nodes = doc.get("nodes")
    edges = doc.get("edges")
    if not isinstance(nodes, list):
        errs.append("nodes 必须是数组")
    if not isinstance(edges, list):
        errs.append("edges 必须是数组")
    if errs:
        return errs
    node_ids: set[str] = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            errs.append(f"nodes[{i}] 必须是对象")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            errs.append(f"nodes[{i}] 缺少 id")
        elif nid in node_ids:
            errs.append(f"节点 id 重复：{nid}")
        else:
            node_ids.add(nid)
        if n.get("kind") not in NODE_KINDS:
            errs.append(f"nodes[{i}] 非法 kind：{n.get('kind')!r}")
        for c in ("x", "y"):
            if not isinstance(n.get(c), (int, float)):
                errs.append(f"nodes[{i}].{c} 必须是数值")
        if not isinstance(n.get("pinned"), bool):
            errs.append(f"nodes[{i}].pinned 必须是布尔")
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            errs.append(f"edges[{i}] 必须是对象")
            continue
        if not isinstance(e.get("id"), str) or not e["id"]:
            errs.append(f"edges[{i}] 缺少 id")
        if e.get("source") not in node_ids:
            errs.append(f"edges[{i}] source 不存在：{e.get('source')!r}")
        if e.get("target") not in node_ids:
            errs.append(f"edges[{i}] target 不存在：{e.get('target')!r}")
    return errs


def validate_patch(old_doc: dict[str, Any], new_doc: Any) -> list[str]:
    """M1 自动保存白名单校验：

    - 节点集合不可增删（人工节点端点 M3 开放）；
    - 系统节点 kind/ref/system/label/props/adopted 不可变，仅 x/y/pinned 可变；
    - 边集合与属性完全不可变（人工边端点 M3 开放）；
    - 任何人工内容篡改系统节点（ref/label/props）一律拒绝。
    """
    errs = validate_doc_shape(new_doc)
    if errs:
        return errs
    old_nodes = {n["id"]: n for n in old_doc.get("nodes", [])}
    new_nodes = {n["id"]: n for n in new_doc["nodes"]}
    if set(old_nodes) != set(new_nodes):
        added = sorted(set(new_nodes) - set(old_nodes))
        removed = sorted(set(old_nodes) - set(new_nodes))
        if added:
            errs.append(f"M1 不允许新增节点：{', '.join(added)}")
        if removed:
            errs.append(f"系统节点不可删除：{', '.join(removed)}")
    _IMMUTABLE_NODE_FIELDS = ("kind", "ref", "system", "label", "props",
                              "adopted", "stale")
    for nid in sorted(set(old_nodes) & set(new_nodes)):
        o, n = old_nodes[nid], new_nodes[nid]
        for f in _IMMUTABLE_NODE_FIELDS:
            if o.get(f) != n.get(f):
                errs.append(f"节点 {nid} 的 {f} 不可变（系统节点仅可移动/钉住）")
                break
    old_edges = {e["id"]: e for e in old_doc.get("edges", [])}
    new_edges = {e["id"]: e for e in new_doc["edges"]}
    if set(old_edges) != set(new_edges):
        errs.append("M1 不允许新增/删除连线（人工连线在后续版本开放）")
    else:
        for eid in sorted(set(old_edges) & set(new_edges)):
            if old_edges[eid] != new_edges[eid]:
                errs.append(f"系统边 {eid} 不可修改")
    return errs


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _event_time_keys(pack_id: str = "default", base_dir=None) -> tuple[str, ...]:
    """业务事件时间属性名（本体 semantic:event_time 声明）。

    画布业务时间轴据此定位「行数据里哪个字段是业务发生时间」。
    本体未声明 → 返回空元组，调用方回落研判过程时间，绝不硬编码中文列名。
    """
    try:
        from core.ontology_loader import load_semantic_roles
        roles = load_semantic_roles(pack_id, base_dir)
        return tuple(roles.get("event_time") or ())
    except Exception:
        return ()


def _row_event_time(sr: dict[str, Any],
                    keys: tuple[str, ...]) -> str:
    """从行数据取业务时间值（ISO 字符串）；取不到返回空串。"""
    if not isinstance(sr, dict) or not keys:
        return ""
    for k in keys:
        v = sr.get(k)
        if v is None:
            continue
        # 日期对象/字符串都接受；空值跳过
        s = v.isoformat() if hasattr(v, "isoformat") else str(v).strip()
        if s:
            return s
    return ""


def _row_specs(clue_id: str, detail: dict[str, Any],
               evidence: list[dict[str, Any]], *,
               pack_id: str = "default", base_dir=None) -> list[dict[str, Any]]:
    """收集去重数据行（row_uri, source, granularity, registered, event_time）。

    口径：优先 detail.source_rows（assemble_detail 视图行，含回填），
    无视图行时回落三栏 fact.source_rows 引用。行 URI 与
    evidence_builder._make_source_ref 完全一致（含表级汇总 #table/ 形态）。
    """
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(uri: str, source: str, granularity: str = "",
             registered: bool = False, event_time: str = "") -> None:
        if not uri or uri in seen:
            return
        seen.add(uri)
        specs.append({"row_uri": uri, "source": source,
                      "granularity": granularity,
                      "registered": registered,
                      "event_time": event_time})

    # 业务时间属性名（本体声明），用于从行数据定位业务发生时间
    et_keys = _event_time_keys(pack_id, base_dir)

    view_rows = detail.get("source_rows")
    if isinstance(view_rows, list) and view_rows:
        for idx, sr in enumerate(view_rows):
            if not isinstance(sr, dict):
                continue
            ref = _make_source_ref(sr, idx, clue_id, pack_id=pack_id,
                                   base_dir=base_dir)
            push(ref["row_uri"], ref.get("source") or "未知数据源",
                 granularity=str(sr.get("粒度") or ""),
                 event_time=_row_event_time(sr, et_keys))
    else:
        for f in evidence:
            if f.get("kind") != "fact":
                continue
            for sr in f.get("source_rows") or []:
                push(str((sr or {}).get("row_uri") or ""),
                     str((sr or {}).get("source") or "未知数据源"),
                     event_time=_row_event_time(sr or {}, et_keys))
    return specs


def _row_label(spec: dict[str, Any]) -> str:
    source = str(spec.get("source") or "数据源")
    if spec.get("granularity") == "表级汇总":
        return f"{source}（表级汇总）"
    return source


def _apply_positions(nodes: list[dict[str, Any]]) -> None:
    """按 kind 分层列分配确定性初始坐标（列内按出现顺序）。"""
    col_x = {
        "rule": _X_RULE,
        "fact": _X_FACT,
        "verify_item": _X_FACT,
        "evidence": _X_EVIDENCE,
        "source_row": _X_ROW,
        "object": _X_FACT + 220,
        "source_file": 960,
        # 补齐与前端 RANK_X 同口径（hypothesis/note/function_result 均在 480 列）
        "hypothesis": _X_EVIDENCE,
        "note": _X_EVIDENCE,
        "function_result": _X_EVIDENCE,
    }
    counters: dict[str, int] = {}
    for n in nodes:
        col = n["kind"]
        idx = counters.get(col, 0)
        counters[col] = idx + 1
        n["x"] = col_x.get(col, 0)
        # 同列 fact/verify_item 共享纵轴：合并计数，避免重叠
        if col in ("fact", "verify_item"):
            shared = counters.get("_fact_col", 0)
            counters["_fact_col"] = shared + 1
            n["y"] = shared * _Y_GAP
        else:
            n["y"] = idx * _Y_GAP
    # 确定性序（x, y, id），方便单测/快照比对
    nodes.sort(key=lambda n: (n["x"], n["y"], n["id"]))


def _truncate(text: str, limit: int = _LABEL_LIMIT) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"
