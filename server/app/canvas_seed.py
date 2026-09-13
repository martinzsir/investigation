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
                ) -> dict[str, Any]:
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
    row_ids: dict[str, str] = {}
    row_y_index: dict[str, int] = {}
    for spec in row_specs:
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
                "registered": bool(spec.get("registered", False)),
            },
        })

    # ---- 事实层（evidence.fact 栏，按栏内序号给稳定 ref）----
    facts = [it for it in evidence if it.get("kind") == "fact"]
    fact_ids: list[str] = []
    for idx, f in enumerate(facts):
        ref = fact_ref(clue_id, idx)
        node_id = sys_node_id("fact", ref)
        fact_ids.append(node_id)
        text = str(f.get("text") or "")
        nodes.append({
            "id": node_id, "kind": "fact", "ref": ref,
            "label": _truncate(text) or "事实",
            "system": True, "pinned": False,
            "props": {"text": text},
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
    return {"nodes": nodes, "edges": edges}


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
def _row_specs(clue_id: str, detail: dict[str, Any],
               evidence: list[dict[str, Any]], *,
               pack_id: str = "default", base_dir=None) -> list[dict[str, Any]]:
    """收集去重数据行（row_uri, source, granularity, registered）。

    口径：优先 detail.source_rows（assemble_detail 视图行，含回填），
    无视图行时回落三栏 fact.source_rows 引用。行 URI 与
    evidence_builder._make_source_ref 完全一致（含表级汇总 #table/ 形态）。
    """
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(uri: str, source: str, granularity: str = "",
             registered: bool = False) -> None:
        if not uri or uri in seen:
            return
        seen.add(uri)
        specs.append({"row_uri": uri, "source": source,
                      "granularity": granularity,
                      "registered": registered})

    view_rows = detail.get("source_rows")
    if isinstance(view_rows, list) and view_rows:
        for idx, sr in enumerate(view_rows):
            if not isinstance(sr, dict):
                continue
            ref = _make_source_ref(sr, idx, clue_id, pack_id=pack_id,
                                   base_dir=base_dir)
            push(ref["row_uri"], ref.get("source") or "未知数据源",
                 granularity=str(sr.get("粒度") or ""))
    else:
        for f in evidence:
            if f.get("kind") != "fact":
                continue
            for sr in f.get("source_rows") or []:
                push(str((sr or {}).get("row_uri") or ""),
                     str((sr or {}).get("source") or "未知数据源"))
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
