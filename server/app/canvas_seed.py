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

# ----------------------------------------------------------------------
# 定向镜头（Lens）线索成图上限
# ----------------------------------------------------------------------
# timeline_sequence 的 detail.timeline 可达上百事件；全量灌入会拖垮前端。
# 事件按 date 排序截断（时间轴上"最早的 N 起"语义成立），meta 如实声明
# shown/total；区间节点（聚集簇/碰撞窗）数量天然较小，单独设限。
_MAX_LENS_EVENT_NODES = 150
_MAX_LENS_INTERVAL_NODES = 60
# 定向深挖层最多展开的观察条数（超出按倒序取最近若干条，记 meta）。
# 每条观察自带 150 事件 / 60 区间上限，若不限条数，多次深挖叠加会把发起
# 画布挤爆——与画布规模保护同口径：宁可显式截断并留痕，不静默丢弃也不无限堆叠。
_MAX_ORIGIN_LAYER_OBS = 8


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

    # ---- 定向镜头线索层：skill_id 合成规则 + 事件/主体对象 + 聚集簇/碰撞窗 ----
    # 主产物规则回填优先；镜头线索 detail 无 rule_id/rules，规则身份取顶层
    # skill_id（与线索详情「规则 timeline_*」同源），避免误落历史占位节点。
    lens_layer = build_lens_layer(clue_id, detail)
    if (not rules and lens_layer is not None
            and lens_layer.get("recognized") and lens_layer.get("rule")):
        rules = [lens_layer["rule"]]

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

    # ---- 定向镜头层：对象/区间节点 + 规则挂边 ----
    lens_meta = None
    if lens_layer is not None and lens_layer.get("recognized"):
        lens_node_ids = {n["id"] for n in nodes}
        for n in lens_layer.get("nodes") or []:
            if n["id"] in lens_node_ids:
                continue
            lens_node_ids.add(n["id"])
            nodes.append(n)
        # 规则 ──查询自──▶ 区间；规则 ──涉及──▶ 主体/项目（事件经区间挂接，
        # 避免规则直连一百多个事件节点）
        interval_ids = {
            n["id"] for n in lens_layer.get("nodes") or []
            if n.get("kind") == "function_result"}
        plain_object_ids = {
            n["id"] for n in lens_layer.get("nodes") or []
            if n.get("kind") == "object"
            and (n.get("props") or {}).get("lens_layer") != "event"}
        for rid in rule_ids:
            for iid in sorted(interval_ids):
                add_edge(rid, iid, "查询自")
            for oid in sorted(plain_object_ids):
                add_edge(rid, oid, "涉及")
        # 区间 ──涉及──▶ 事件
        for src, tgt, rel in lens_layer.get("edges") or []:
            add_edge(src, tgt, rel)
        lens_meta = lens_layer.get("meta")

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
    # 定向镜头层幂等标记 + 事件/区间规模声明（非镜头线索也落标记，
    # 表示"已按 lens 口径 seed 过"，reconcile 路径据此跳过重复扫描）
    meta["lens_seeded"] = lens_layer is not None
    if lens_meta is not None:
        meta["lens"] = lens_meta
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
                     lens_layer: dict[str, Any] | None = None,
                     origin_lens_layer: dict[str, Any] | None = None,
                     ) -> tuple[dict[str, Any], int, int]:
    """GET 幂等增量补种：补齐 seed 后新出现的 verify_item/evidence 节点
    与 verify_item-[挂接]→evidence 边；lens_layer 非空时补入定向镜头
    规则/对象/区间节点与挂边（老画布升级，只增不改删）；origin_lens_layer
    非空时补入「本线索发起的定向镜头」代表节点（点击跳转 sub_clue 画布）。

    只增不改删：节点/边形状与过滤口径（status=建议 不成节点，AC-105-2）
    与 seed_canvas 同源；不动既有节点/边/坐标，不重排。新节点 y 按 doc
    内同列已有节点计数排布（fact/verify_item/function_result 共享列）。
    唯一例外：lens 合成规则落位时撤下「未关联规则（历史产物）」占位节点
    及其关联边——它是规则提取缺口的产物，不是用户内容，留着会与真规则
    节点并存造成误导。
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

    # ---- 定向镜头层补种（老画布升级；空层也只落 lens_seeded 幂等标记）----
    removed_placeholder = False
    new_meta: dict[str, Any] | None = None
    if lens_layer is not None:
        recognized = bool(lens_layer.get("recognized"))
        rule_src: str | None = None
        new_nodes_pending: list[dict[str, Any]] = []

        rule_desc = lens_layer.get("rule") if recognized else None
        if isinstance(rule_desc, dict) and rule_desc.get("rule_id"):
            rid = str(rule_desc["rule_id"])
            rule_src = sys_node_id("rule", rid)
            if rule_src not in node_ids:
                new_nodes_pending.append({
                    "id": rule_src, "kind": "rule", "ref": rid,
                    "label": f"规则 {rid}", "system": True, "pinned": False,
                    "props": {
                        "rule_id": rid,
                        "rule_text": str(rule_desc.get("rule_text") or ""),
                        "basis": str(rule_desc.get("basis") or ""),
                    },
                })

        if recognized:
            for n in lens_layer.get("nodes") or []:
                if n["id"] not in node_ids \
                        and not any(x["id"] == n["id"]
                                    for x in new_nodes_pending):
                    new_nodes_pending.append(dict(n))

        # 真规则落位 → 撤下历史占位节点及其关联边（系统占位，非用户内容）。
        # 注意必须改 nodes 本身（out["nodes"] 取的是它），只过滤一个局部
        # 副本会让占位节点在最终 doc 里原样保留。
        if rule_src is not None:
            ph = sys_node_id("rule", _HISTORICAL_RULE_ID)
            if ph in node_ids:
                nodes = [n for n in nodes if n.get("id") != ph]
                node_ids.discard(ph)
                removed = [e for e in edges
                           if e.get("source") == ph or e.get("target") == ph]
                if removed:
                    edges = [e for e in edges
                             if e.get("source") != ph and e.get("target") != ph]
                    edge_ids = {e["id"] for e in edges}
                removed_placeholder = True

        # 新节点按 doc 现状增量定位（不动任何既有坐标）
        _position_incremental(nodes, new_nodes_pending)
        for n in new_nodes_pending:
            nodes.append(n)
            node_ids.add(n["id"])
            added_nodes += 1

        def _add_e(src: str, tgt: str, rel: str) -> None:
            nonlocal added_edges
            eid = f"e:{src}--{rel}--{tgt}"
            if eid in edge_ids:
                return
            edge_ids.add(eid)
            edges.append({"id": eid, "source": src, "target": tgt,
                          "rel": rel, "system": True})
            added_edges += 1

        if recognized and rule_src is not None:
            interval_ids = sorted(
                n["id"] for n in (lens_layer.get("nodes") or [])
                if n.get("kind") == "function_result")
            plain_object_ids = sorted(
                n["id"] for n in (lens_layer.get("nodes") or [])
                if n.get("kind") == "object"
                and (n.get("props") or {}).get("lens_layer") != "event")
            for iid in interval_ids:
                _add_e(rule_src, iid, "查询自")
            for oid in plain_object_ids:
                _add_e(rule_src, oid, "涉及")
            for src, tgt, rel in lens_layer.get("edges") or []:
                _add_e(src, tgt, rel)

        # meta：保留既有键，落 lens_seeded + lens 规模声明
        new_meta = dict(doc.get("meta") or {})
        new_meta["lens_seeded"] = True
        if recognized:
            new_meta["lens"] = lens_layer.get("meta")

    # ---- 由本线索发起的定向镜头代表节点（origin_lens_layer） ----
    # 与 lens_layer 区别：lens_layer 处理「线索本身是 lens 线索」的成图
    # （rule→fact→source_row 主形状之外的 lens 形状）；origin_lens_layer
    # 处理「本线索发起了 lens_run，产出是新线索」的代表节点——B 线索的
    # 完整结构在 B 自己的画布，A 画布只看到代表节点 + 跳转。
    if origin_lens_layer:
        ol_new: list[dict[str, Any]] = []
        for n in origin_lens_layer.get("nodes") or []:
            if (isinstance(n, dict) and n.get("id")
                    and n["id"] not in node_ids):
                ol_new.append(dict(n))
        if ol_new:
            _position_incremental(nodes, ol_new)
            for n in ol_new:
                nodes.append(n)
                node_ids.add(n["id"])
                added_nodes += 1
        # edges：[src, tgt, rel] 三元组 → dict 边（与 lens_layer 同口径）
        for edge in origin_lens_layer.get("edges") or []:
            if not isinstance(edge, list) or len(edge) < 3:
                continue
            src, tgt, rel = str(edge[0]), str(edge[1]), str(edge[2])
            # 源（origin.node_id）可能不在画布——跳过该边，节点仍独立可见
            if src not in node_ids or tgt not in node_ids:
                continue
            eid = f"e:{src}--{rel}--{tgt}"
            if eid in edge_ids:
                continue
            edge_ids.add(eid)
            edges.append({"id": eid, "source": src, "target": tgt,
                          "rel": rel, "system": True})
            added_edges += 1

    if (not added_nodes and not added_edges and not removed_placeholder
            and new_meta is None):
        return doc, 0, 0
    out = dict(doc)
    out["nodes"] = nodes
    out["edges"] = edges
    if new_meta is not None:
        out["meta"] = new_meta
    elif "meta" in doc:
        # verify/material 补种路径也保留原 meta（旧实现会丢）
        out["meta"] = doc["meta"]
    return out, added_nodes, added_edges


# ----------------------------------------------------------------------
# 定向镜头（Lens）线索成图层
# ----------------------------------------------------------------------
# 主产物线索形状是「规则 → fact → source_row」；定向镜头线索（lens_runs 并线
# 产物）是另一种形状：规则身份在顶层 skill_id，证据在 evidence_refs（指向
# obj_* 语义对象），时间研判的事件/聚集簇在 detail.timeline/bursts/events。
# 本层把后者映射为同一套画布语义：
#
#   rule(skill_id) ──查询自──▶ function_result(聚集簇/碰撞窗) ──涉及──▶ object(事件)
#                  └──涉及──▶ object(主体/项目等非事件对象)
#
# 纪律：
#   - 纯函数、不开库；只消费线索产物已有字段（事件对象在语义层真实存在，
#     引用悬空由生产端 skill_invoke 校验硬失败兜底）；
#   - 事件 object 节点 ref 与 canvas_expand._object_node 严格同口径
#     "call:<pk>"（evidence_refs 的 "obj_call#<pk>" 仅作映射输入），
#     M2 展开时同一 id 幂等合流，绝不产生重复节点；
#   - 事件节点带 props.event_time=事件 date，时间轴视角无需特判即可分档；
#   - 区间节点 props 带 start/end/event_time=start（第二批前端渲染轨道带）。
def _obj_ref_to_node_ref(ref: Any) -> tuple[str, str] | None:
    """evidence_refs 的 obj_* 引用 → M2 object 节点 (type, pk)。

    "obj_call#call_834" → ("call", "call_834")，与
    canvas_expand._object_node 的 f"{ot.name}:{pk}" 同口径；
    非 obj_# 引用（aggregate/time_window 等）返回 None。
    """
    if not isinstance(ref, str) or not ref.startswith("obj_") or "#" not in ref:
        return None
    name, pk = ref[4:].split("#", 1)
    name, pk = name.strip(), pk.strip()
    if not name or not pk:
        return None
    return name, pk


def _lens_events(det: dict[str, Any]) -> list[dict[str, Any]]:
    """汇集时间研判事件（timeline / bursts[].events / events），按 (date,pk) 去重排序。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(e: Any) -> None:
        if not isinstance(e, dict):
            return
        src = str(e.get("src_object") or "")
        pk = str(e.get("event_pk") or "")
        if not src or not pk or (src, pk) in seen:
            return
        seen.add((src, pk))
        out.append(e)

    for e in det.get("timeline") or []:
        add(e)
    for b in det.get("bursts") or []:
        if isinstance(b, dict):
            for e in b.get("events") or []:
                add(e)
    for e in det.get("events") or []:
        add(e)
    out.sort(key=lambda e: (
        str(e.get("date") or ""), str(e.get("src_object") or ""),
        str(e.get("event_pk") or "")))
    return out


def _event_object_node(event: dict[str, Any]) -> dict[str, Any]:
    """时间研判事件 → object 节点（ref 对齐 M2 代理键，带 event_time）。"""
    src = str(event.get("src_object") or "")
    pk = str(event.get("event_pk") or "")
    ref = f"{src}:{pk}"
    etype = str(event.get("type") or src)
    brief = str(event.get("brief") or "")
    label = _truncate(f"{etype} {brief}".strip()) or pk
    return {
        "id": sys_node_id("object", ref), "kind": "object", "ref": ref,
        "label": label, "system": True, "pinned": False,
        "props": {
            "type": src, "pk": pk,
            "event_time": str(event.get("date") or ""),
            "event_type": etype,
            "role": str(event.get("role") or ""),
            "brief": brief,
            "lens_layer": "event",
        },
    }


def _plain_object_node(obj_type: str, pk: str, label: str,
                       lens_role: str) -> dict[str, Any]:
    """主体/项目等非事件语义对象 → object 节点（无业务时间）。"""
    ref = f"{obj_type}:{pk}"
    return {
        "id": sys_node_id("object", ref), "kind": "object", "ref": ref,
        "label": _truncate(label) or pk, "system": True, "pinned": False,
        "props": {"type": obj_type, "pk": pk, "lens_role": lens_role},
    }


def _shift_date(iso: str, days: int) -> str:
    """ISO 日期 ±N 天；解析失败返回空串（纯 stdlib，不引第三方）。"""
    from datetime import datetime as _dt, timedelta as _td
    try:
        return (_dt.fromisoformat(str(iso)[:10]).date()
                + _td(days=int(days))).isoformat()
    except (ValueError, TypeError):
        return ""


def _lens_intervals(clue_id: str, det: dict[str, Any]
                    ) -> tuple[list[dict[str, Any]], int]:
    """聚集簇/碰撞窗 → function_result 区间节点（带 start/end）。

    返回 (节点列表（已截断）, 区间总数)。每个节点 props.event_time=start，
    时间轴视角天然落到窗口起点。
    """
    fn_name = str(det.get("function") or "")
    out: list[dict[str, Any]] = []

    for i, b in enumerate(det.get("bursts") or [], start=1):
        if not isinstance(b, dict):
            continue
        start, end = str(b.get("start") or ""), str(b.get("end") or "")
        ref = f"burst:{clue_id}:{i}"
        out.append({
            "id": sys_node_id("function_result", ref),
            "kind": "function_result", "ref": ref,
            "label": _truncate(
                f"聚集簇 {i}｜{start}~{end}"
                f"（{b.get('event_count', len(b.get('events') or []))} 起）"),
            "system": True, "pinned": False,
            "props": {
                "function": fn_name,
                "interval_kind": "burst",
                "start": start, "end": end,
                "event_time": start,
                "event_count": b.get("event_count"),
                "types": list(b.get("types") or []),
            },
        })

    if det.get("anchor_date") and det.get("window_days") is not None:
        anchor = str(det.get("anchor_date") or "")
        win = det.get("window_days")
        idx = det.get("collision_index") or 1
        start = _shift_date(anchor, -int(win))
        end = _shift_date(anchor, int(win))
        n_events = len(det.get("events") or [])
        ref = f"collision:{clue_id}:{idx}"
        out.append({
            "id": sys_node_id("function_result", ref),
            "kind": "function_result", "ref": ref,
            "label": _truncate(
                f"碰撞窗｜{anchor} ±{win} 天（{n_events} 起）"),
            "system": True, "pinned": False,
            "props": {
                "function": fn_name,
                "interval_kind": "collision_window",
                "anchor_date": anchor, "window_days": win,
                "start": start, "end": end,
                "event_time": start,
                "event_count": n_events,
            },
        })

    total = len(out)
    return out[:_MAX_LENS_INTERVAL_NODES], total


# ----------------------------------------------------------------------
# 镜头产出识别
# ----------------------------------------------------------------------
# 镜头 detail 的典型键：任一存在即说明这是镜头产出（形状判据）。
# 覆盖 timeline（timeline/bursts/events）与 relation（nodes/edges），
# 新增镜头包只要沿用这些键就自动识别，不必改识别代码。
_LENS_DETAIL_KEYS = ("timeline", "bursts", "events", "nodes", "edges",
                     "collision_window", "intervals", "paths", "neighbors")


def _looks_like_lens(item: dict[str, Any], det: dict[str, Any],
                     skill_id: str) -> bool:
    """镜头产出识别：归属 → 形状 → 技能包，三选一命中即认。

    为什么换口径
    ------------
    旧口径 `lens_run_id 标记 or detail.function 以 timeline_ 开头` 两处都脆：
      ① 并线标记：定向产出已统一为观察、不再并线进线索，标记不复存在；
      ② 函数名前缀：硬编码 timeline_，relation 包（关系邻域/共同邻居/
         路径）永远认不出，每新增镜头包都要改一次这里。
    """
    # ① 显式归属：观察本体，或调用方已打镜头包标记
    if item.get("observation_id") or item.get("lens_pack"):
        return True
    # ② 结构形状
    if any(k in det for k in _LENS_DETAIL_KEYS):
        return True
    # ③ 技能归属：非内置包即为镜头（换包自动跟随）
    if skill_id:
        try:
            from core.pack_loader import BUILTIN_PACK_ID
            from core.registry import get_registry
            reg = get_registry()
            if skill_id in reg:
                return reg.skill(skill_id).pack_id != BUILTIN_PACK_ID
        except Exception:
            pass
    return False


def build_lens_layer(clue_id: str, item: dict[str, Any] | None
                     ) -> dict[str, Any] | None:
    """定向镜头线索 → 画布 lens 层（seed/reconcile 同源纯函数）。

    识别口径（见 _looks_like_lens）：按**归属 + 形状**判定，不再依赖
    lens_run_id 并线标记或 timeline_ 函数名前缀（那个前缀认不出 relation
    包，每加一个镜头包就要改代码）。非镜头线索返回 recognized=False 的
    空层（调用方据此落 lens_seeded 幂等标记，避免每次 GET 重扫）。

    返回：{recognized, rule, nodes, edges[[src,tgt,rel]], meta}
    """
    empty = {"recognized": False, "rule": None, "nodes": [],
             "edges": [], "meta": {"events": {"shown": 0, "total": 0},
                                   "intervals": {"shown": 0, "total": 0}}}
    if not isinstance(item, dict):
        return empty
    det = item.get("detail") or {}
    if not isinstance(det, dict):
        det = {}
    skill_id = str(item.get("skill_id") or "")
    if not _looks_like_lens(item, det, skill_id):
        return empty

    # ---- 合成规则描述（seed 仅在 _extract_rules 落空时采用）----
    rule = None
    if skill_id:
        rule = {
            "rule_id": skill_id,
            "rule_text": str(det.get("hypothesis") or item.get("title") or ""),
            "basis": str(item.get("title") or det.get("hypothesis") or ""),
        }

    # ---- 事件 object 节点（按 date 排序截断）----
    events = _lens_events(det)
    event_nodes: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for e in events[:_MAX_LENS_EVENT_NODES]:
        n = _event_object_node(e)
        event_nodes.append(n)
        event_ids.add(n["id"])

    # ---- 主体/项目等非事件对象（evidence_refs 的 obj_* 引用，事件引用除外）----
    label_map: dict[str, str] = {}
    subj = det.get("subject")
    if isinstance(subj, dict) and subj.get("type") and subj.get("pk"):
        label_map[f"{subj['type']}:{subj['pk']}"] = str(
            subj.get("name") or subj["pk"])
    proj = det.get("project")
    if isinstance(proj, dict) and proj.get("pk"):
        label_map[f"bid_project:{proj['pk']}"] = str(
            proj.get("name") or proj["pk"])

    plain_nodes: list[dict[str, Any]] = []
    plain_refs_seen: set[str] = set()
    for r in item.get("evidence_refs") or []:
        if not isinstance(r, dict) or r.get("kind") != "node":
            continue
        parsed = _obj_ref_to_node_ref(r.get("ref"))
        if parsed is None:
            continue
        obj_type, pk = parsed
        node_ref = f"{obj_type}:{pk}"
        nid = sys_node_id("object", node_ref)
        if nid in event_ids or node_ref in plain_refs_seen:
            continue
        plain_refs_seen.add(node_ref)
        role = "project" if obj_type == "bid_project" else "subject"
        plain_nodes.append(_plain_object_node(
            obj_type, pk, label_map.get(node_ref, pk), role))

    # ---- 区间节点 ----
    interval_nodes, interval_total = _lens_intervals(clue_id, det)

    # ---- 边：区间 ──涉及──▶ 事件（仅成图事件）----
    edges: list[list[str]] = []
    shown_event_refs = {n["ref"] for n in event_nodes}

    def interval_event_edges(iv_id: str, raw_events: Any) -> None:
        for e in raw_events or []:
            if not isinstance(e, dict):
                continue
            ref = f"{e.get('src_object') or ''}:{e.get('event_pk') or ''}"
            if ref in shown_event_refs:
                edges.append([iv_id, sys_node_id("object", ref), "涉及"])

    bursts = det.get("bursts") or []
    for node, b in zip(interval_nodes, bursts):
        if isinstance(b, dict) and (node.get("props") or {}).get(
                "interval_kind") == "burst":
            interval_event_edges(node["id"], b.get("events"))
    if det.get("events") is not None:
        win_node = next(
            (n for n in interval_nodes
             if (n.get("props") or {}).get("interval_kind")
             == "collision_window"), None)
        if win_node is not None:
            interval_event_edges(win_node["id"], det.get("events"))

    meta = {
        "skill_id": skill_id,
        "events": {"shown": len(event_nodes), "total": len(events)},
        "intervals": {"shown": len(interval_nodes), "total": interval_total},
    }
    return {
        "recognized": True, "rule": rule,
        "nodes": plain_nodes + event_nodes + interval_nodes,
        "edges": edges, "meta": meta,
    }


def _position_incremental(existing_nodes: list[dict[str, Any]],
                          new_nodes: list[dict[str, Any]]) -> None:
    """为只增补种的新节点分配列坐标（与 _apply_positions 同列同口径）。"""
    col_x = {
        "rule": _X_RULE,
        "fact": _X_FACT,
        "verify_item": _X_FACT,
        "function_result": _X_FACT,
        "evidence": _X_EVIDENCE,
        "source_row": _X_ROW,
        "object": _X_FACT + 220,
        "source_file": 960,
        "hypothesis": _X_EVIDENCE,
        "note": _X_EVIDENCE,
    }
    counters: dict[str, int] = {}
    shared = 0
    for n in existing_nodes:
        col = n.get("kind")
        counters[col] = counters.get(col, 0) + 1
        if col in ("fact", "verify_item", "function_result"):
            shared += 1
    for n in new_nodes:
        col = n["kind"]
        if col in ("fact", "verify_item", "function_result"):
            n["x"] = col_x.get(col, 0)
            n["y"] = shared * _Y_GAP
            shared += 1
        else:
            idx = counters.get(col, 0)
            n["x"] = col_x.get(col, 0)
            n["y"] = idx * _Y_GAP
        counters[col] = counters.get(col, 0) + 1


# ----------------------------------------------------------------------
# 定向镜头代表节点层（origin_lens）：本线索发起的深挖结果回画布
# ----------------------------------------------------------------------
# 正兵在 A 画布跑定向镜头 → 产出**观察**落案件级定向档案
# （origin.clue_id=A）。A 画布需要看到「我刚才跑出了什么」——每条定向观察
# 在 A 画布上落一个**代表节点**（不并入完整结构，否则会淹没 A 的研判上下文），
# 点击跳到观察详情；认为构成疑点时再提升为线索（强制指定假设）。
#
# 生灭：定向观察**不随版本失效**（案件级档案），版本前进后仍在发起画布可见。
# 幂等：节点 id 含 run_id+observation_id，同 run 多次 GET 不重复。
def build_origin_lens_layer(case_dir, version: int,
                             clue_id: str) -> dict[str, Any]:
    """本线索发起的定向深挖 → 主画布代表节点层。

    每条定向观察在发起画布落**一个代表节点**（不铺完整结构）。完整结构
    由 build_observation_layer 供「观察图层」使用——图层是独立坐标系，
    默认关闭，开启才渲染。两者职责分离：

      代表节点（本函数）：常驻主画布，回答「我在这条线索上跑过什么」，
                          点击跳观察详情 / 可提升为线索。
      观察图层（另一函数）：外挂独立时间轴，回答「深挖出来的事发生在哪天」。

    为什么不再把完整结构铺进主画布
    ------------------------------
    铺进主画布要与流程列共享坐标系（流程列 x=0/260/480/720 与时间轴
    x=40/250…1510 大量重合），且深挖一次可带来上百节点，主画布的研判
    上下文会被淹没。图层方案下主画布保持干净，观察结构按需唤出。

    version 参数保留仅为兼容调用方，**不再用于过滤**：定向观察落案件级
    档案（artifacts/directed_observations.json），RESCAN 版本前进后仍在
    发起画布可见——正兵显式发起的深挖不该因重扫蒸发。
    """
    empty: dict[str, Any] = {"nodes": [], "edges": []}
    if not clue_id:
        return empty
    try:
        from server.app.clues_artifact import load_directed_observations
        obs = load_directed_observations(case_dir)
    except Exception:
        return empty

    nodes: list[dict[str, Any]] = []
    edges: list[list[str]] = []
    for o in obs:
        origin = getattr(o, "origin", None) or {}
        if not isinstance(origin, dict):
            continue
        if str(origin.get("clue_id") or "") != str(clue_id):
            continue
        oid = str(getattr(o, "observation_id", "") or "")
        if not oid:
            continue
        run_id = str(getattr(o, "run_id", "") or "")
        title = str(getattr(o, "title", "") or "深挖结果")
        nid = f"origin_lens:{run_id}:{oid}"
        nodes.append({
            "id": nid, "kind": "function_result",
            "ref": oid,
            "label": _truncate(title),
            "system": True, "pinned": False,
            "props": {
                "origin_lens": True,
                "observation_id": oid,
                "origin_lens_run_id": run_id,
                "origin_lens_skill_id": str(getattr(o, "skill_id", "") or ""),
                "lens_layer": "origin",
                "directed": True,
            },
        })
        # 边：发起主体 → 代表节点（"深挖" 关系）
        # 没记录 origin.node_id 时不挂边（独立浮节点也比错连强）
        origin_node_id = str(origin.get("node_id") or "")
        if origin_node_id:
            edges.append([origin_node_id, nid, "深挖"])
    return {"nodes": nodes, "edges": edges}


# ----------------------------------------------------------------------
# 观察图层（observation layer）：定向深挖结果的独立时间轴
# ----------------------------------------------------------------------
# 观察不在 canvas doc 里——它们存在案件级定向档案，每次 GET 动态派生。
# 这使它们天然是「外挂层」：开关是纯视图状态，不影响任何持久化数据。
#
# 独立坐标系：观察层用自己的时间轴算 x（区间节点成带），整体 y 偏移到主
# 内容下方。不与流程列共享坐标，因此不会出现坐标重合遮挡。
#
# 节点 id 加 OBS_ID_PREFIX：主画布可能已存在同名 object 节点（深挖引用
# 的实体常常也是主画布上的实体），不加前缀合并时会 id 冲突。
OBS_ID_PREFIX = "obs::"


def build_observation_layer(case_dir, clue_id: str) -> dict[str, Any]:
    """本线索发起的定向深挖 → 观察图层完整结构（不并入主 doc）。

    返回 {nodes, edges, meta}。节点带 props.obs_layer=True 与
    props.obs_of=<observation_id>，前端据此：
      - 参与时间轴布局时用**本层自己的轴**（独立坐标系）；
      - 渲染时给独立描边色，与主画布元素区分；
      - 点击剥掉 OBS_ID_PREFIX 拿到真实 ref 跳转。

    meta 供前端显示层规模与截断情况。
    """
    empty: dict[str, Any] = {"nodes": [], "edges": [], "meta": {}}
    if not clue_id:
        return empty
    try:
        from server.app.clues_artifact import load_directed_observations
        obs = load_directed_observations(case_dir)
    except Exception:
        return empty

    mine = []
    for o in obs:
        origin = getattr(o, "origin", None) or {}
        if not isinstance(origin, dict):
            continue
        if str(origin.get("clue_id") or "") != str(clue_id):
            continue
        if not str(getattr(o, "observation_id", "") or ""):
            continue
        mine.append(o)
    mine.reverse()  # 最近深挖优先（配合截断）

    nodes: list[dict[str, Any]] = []
    edges: list[list[str]] = []
    seen: set[str] = set()
    expanded = 0
    skipped = 0

    def _pid(raw: str) -> str:
        return OBS_ID_PREFIX + str(raw)

    def _push(n: dict[str, Any]) -> None:
        nid = str(n.get("id") or "")
        if not nid or nid in seen:
            return
        seen.add(nid)
        nodes.append(n)

    for o in mine:
        oid = str(getattr(o, "observation_id", "") or "")
        if expanded >= _MAX_ORIGIN_LAYER_OBS:
            skipped += 1
            continue
        det = getattr(o, "detail", None)
        if not isinstance(det, dict) or not det:
            continue
        item = {
            "skill_id": str(getattr(o, "skill_id", "") or ""),
            "title": str(getattr(o, "title", "") or ""),
            "detail": det,
            "evidence_refs": list(getattr(o, "evidence_refs", None) or []),
        }
        layer = build_lens_layer(oid, item)
        if not (layer or {}).get("recognized"):
            continue
        created = str(getattr(o, "created_at", "") or "")
        for n in layer.get("nodes") or []:
            raw_id = str(n.get("id") or "")
            if not raw_id:
                continue
            props = dict(n.get("props") or {})
            # 研判过程时间：观察的创建时刻（process 口径落档用）
            if created and not props.get("created_at"):
                props["created_at"] = created
            props["obs_layer"] = True
            props["obs_of"] = oid
            props["obs_skill_id"] = str(getattr(o, "skill_id", "") or "")
            _push({**n, "id": _pid(raw_id), "props": props})
        for src, tgt, rel in layer.get("edges") or []:
            edges.append([_pid(src), _pid(tgt), rel])
        expanded += 1

    meta = {
        "observations": len(mine),
        "expanded": expanded,
        "skipped": skipped,
        "nodes": len(nodes),
        "edges": len(edges),
    }
    return {"nodes": nodes, "edges": edges, "meta": meta}


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
        # 镜头聚集簇/碰撞窗（function_result）排在事实列：流程读法为
        # 规则(0) → 查询结果(260) → 事件对象(480)；与 fact/verify 共享纵轴
        "function_result": _X_FACT,
        "evidence": _X_EVIDENCE,
        "source_row": _X_ROW,
        "object": _X_FACT + 220,
        "source_file": 960,
        # 补齐与前端 RANK_X 同口径（hypothesis/note 均在 480 列）
        "hypothesis": _X_EVIDENCE,
        "note": _X_EVIDENCE,
    }
    counters: dict[str, int] = {}
    for n in nodes:
        col = n["kind"]
        idx = counters.get(col, 0)
        counters[col] = idx + 1
        n["x"] = col_x.get(col, 0)
        # 同列 fact/verify_item/function_result 共享纵轴：合并计数，避免重叠
        if col in ("fact", "verify_item", "function_result"):
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
