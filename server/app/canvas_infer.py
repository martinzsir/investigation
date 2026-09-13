"""
server/app/canvas_infer.py
M4 画布推断纯函数（RC-105 正向推断与待核实生成 / RC-204 白名单 Function 查询）。

RC-105 建议节点生命周期（画布专属未采纳态，不写 state、不进核查工作台）：
  规则节点「生成手册核实建议」
    → verify_item:pb:<playbook_id>（ref="pb:<id>"，adopted=false，虚线态）
    → 「采纳为核查项」走既有 202 TASK_VERIFY（add_manual / 建议→待核查）
    → 任务终态后 reconcile：节点迁移为 verify_item:<item_id>（adopted=true）
  人工假设「转为待核实」→ 202 add_manual → reconcile 新增 verify_item 节点。

RC-204 扩展查询：
  CANVAS_FUNCTION_WHITELIST 后端配置（资金/通讯/行为追踪类只读 Function）；
  业务化目录不暴露 SQL/impl；参数校验复用 core.functions.check_param_value
  （string 仅 enum 白名单，防注入）；成功产物 function_result 节点 +
  「查询自」系统边，入参快照随节点 props 与审计链留痕。

纪律：纯函数不开库、不读 Parquet、不写 SQL、不生成自由 SQL；
所有落库由 routers/canvas.py 系统通道（_commit_edit）提交。
"""
from __future__ import annotations

from typing import Any

from core.functions import check_param_value

from server.app.canvas_seed import _truncate as truncate_label
from server.app.canvas_seed import sys_node_id

# ----------------------------------------------------------------------
# RC-105：手册建议（playbook）虚节点
# ----------------------------------------------------------------------
PB_PREFIX = "pb:"

# 建议/待核实节点与事实同列；function_result 与对象/书证同列（前端 rank 一致）
X_VERIFY_COL = 260
X_RESULT_COL = 480
Y_GAP = 104

# 手册建议节点 props.status（未采纳）
SUGGESTED_STATUS = "建议"


class CanvasInferError(ValueError):
    """画布推断业务校验失败（路由层转 400 VALIDATION）。"""


def playbook_ref(playbook_id: str) -> str:
    """手册建议节点的 ref 形态（PRD：ref=playbook_id，pb: 前缀与 vi_ 区分）。"""
    return f"{PB_PREFIX}{playbook_id}"


def is_playbook_ref(ref: str) -> bool:
    return isinstance(ref, str) and ref.startswith(PB_PREFIX)


def suggestion_node_id(playbook_id: str) -> str:
    return sys_node_id("verify_item", playbook_ref(playbook_id))


def suggestion_props(item: dict[str, Any]) -> dict[str, Any]:
    """render_suggested item → 建议节点 props（与 state 核查项字段同构）。"""
    props: dict[str, Any] = {
        "text": str(item.get("text") or ""),
        "status": SUGGESTED_STATUS,
        "kind": "suggested",
        "origin": "suggested",
        "channel": str(item.get("channel") or ""),
        "ref_function": str(item.get("ref_function") or ""),
        "falsification": str(item.get("falsification") or ""),
        "playbook_id": str(item.get("playbook_id") or ""),
    }
    external = item.get("external")
    if isinstance(external, dict) and external:
        props["external"] = dict(external)
    return props


def _column_next_y(doc: dict[str, Any], x: int) -> int:
    """同列（±1 容差）节点下方下一纵槽。"""
    ys = [int(n.get("y") or 0) for n in doc.get("nodes", [])
          if abs(float(n.get("x") or 0) - x) < 1]
    return (max(ys) + Y_GAP) if ys else 0


def _sys_edge(source: str, target: str, rel: str) -> dict[str, Any]:
    return {"id": f"e:{source}--{rel}--{target}",
            "source": source, "target": target, "rel": rel, "system": True}


def add_suggestion_nodes(doc: dict[str, Any], *, rule_node_id: str,
                         items: list[dict[str, Any]],
                         now: str) -> dict[str, Any]:
    """把 playbook 建议合并进画布（幂等）。

    去重口径：
      - 同 playbook_id 的 pb: 建议节点已存在 → 跳过；
      - 已存在 verify_item 节点 props.text 相同（已生成/已采纳）→ 跳过。
    失败原因集中在 skipped 返回（不抛异常，匹配落空是正常筛选）。
    """
    rule_node = next((n for n in doc.get("nodes", [])
                      if n.get("id") == rule_node_id), None)
    if rule_node is None:
        raise CanvasInferError(f"画布节点不存在：{rule_node_id}")
    if rule_node.get("kind") != "rule":
        raise CanvasInferError(
            f"仅规则节点可生成手册核实建议，收到 {rule_node.get('kind')!r}")

    existing_pb: set[str] = set()
    existing_texts: set[str] = set()
    for n in doc.get("nodes", []):
        if n.get("kind") != "verify_item":
            continue
        ref = str(n.get("ref") or "")
        if is_playbook_ref(ref):
            existing_pb.add(ref[len(PB_PREFIX):])
        text = ((n.get("props") or {}).get("text"))
        if text:
            existing_texts.add(str(text))

    edge_pairs = {(e.get("source"), e.get("target"), e.get("rel"))
                  for e in doc.get("edges", [])}
    added_nodes: list[dict[str, Any]] = []
    added_edges: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for item in items:
        pb_id = str(item.get("playbook_id") or "")
        text = str(item.get("text") or "")
        if not pb_id or not text:
            skipped.append({"playbook_id": pb_id or "(unknown)",
                            "reason": "建议缺少 playbook_id/text"})
            continue
        if pb_id in existing_pb:
            skipped.append({"playbook_id": pb_id, "reason": "画布已存在该建议"})
            continue
        if text in existing_texts:
            skipped.append({"playbook_id": pb_id,
                            "reason": "画布已存在同文本核查节点"})
            continue
        nid = suggestion_node_id(pb_id)
        node = {
            "id": nid, "kind": "verify_item", "ref": playbook_ref(pb_id),
            "label": truncate_label(text) or "待核实",
            "system": True, "pinned": False, "adopted": False,
            "x": X_VERIFY_COL, "y": _column_next_y(doc, X_VERIFY_COL),
            "props": suggestion_props(item) | {"generated_at": now},
        }
        doc["nodes"].append(node)
        added_nodes.append(node)
        existing_pb.add(pb_id)
        existing_texts.add(text)
        key = (rule_node_id, nid, "手册建议")
        if key not in edge_pairs:
            edge = _sys_edge(rule_node_id, nid, "手册建议")
            doc["edges"].append(edge)
            added_edges.append(edge)
            edge_pairs.add(key)

    return {"added_nodes": added_nodes, "added_edges": added_edges,
            "skipped": skipped}


# ----------------------------------------------------------------------
# 采纳编排：202 入队前计划（决定 transition / add_manual / 已采纳）
# ----------------------------------------------------------------------
def _find_item_by_text(state_items: list[dict[str, Any]],
                       text: str) -> dict[str, Any] | None:
    """按精确文本匹配 state 核查项；建议项优先（采纳既有供给，避免重复成项）。"""
    fallback = None
    for it in state_items:
        if str(it.get("text") or "") != text:
            continue
        if str(it.get("status") or "") == SUGGESTED_STATUS:
            return it
        if fallback is None:
            fallback = it
    return fallback


def plan_adoption(doc: dict[str, Any], node_id: str,
                  state_items: list[dict[str, Any]]) -> dict[str, Any]:
    """采纳计划（纯只读，不入队）：

      pb: 虚节点：state 有同文本建议项 → transition（建议→待核查）；
                  已处理项 → adopted（无需任务，直接 reconcile）；
                  否则 → add_manual（携带 channel/ref_function/external
                  /falsification 路由字段，复用复跑映射）。
      vi_ 节点：必须是 state 中本线索 status=建议 的项 → transition。
    """
    node = next((n for n in doc.get("nodes", [])
                 if n.get("id") == node_id), None)
    if node is None:
        raise CanvasInferError(f"画布节点不存在：{node_id}")
    if node.get("kind") != "verify_item":
        raise CanvasInferError("仅核查项节点可采纳")
    if node.get("stale"):
        raise CanvasInferError("该核查项引用已失效，请刷新画布")
    if node.get("adopted"):
        raise CanvasInferError("该核查项已采纳，请勿重复操作")

    ref = str(node.get("ref") or "")
    props = node.get("props") or {}
    if is_playbook_ref(ref):
        text = str(props.get("text") or "")
        item = _find_item_by_text(state_items, text)
        if item is None:
            plan: dict[str, Any] = {
                "mode": "add_manual", "text": text,
                "channel": str(props.get("channel") or ""),
                "ref_function": str(props.get("ref_function") or ""),
                "falsification": str(props.get("falsification") or "")}
            external = props.get("external")
            if isinstance(external, dict):
                plan["external"] = external
            return plan
        if str(item.get("status") or "") == SUGGESTED_STATUS:
            return {"mode": "transition",
                    "item_id": str(item.get("item_id") or ""), "text": text}
        return {"mode": "adopted", "item_id": str(item.get("item_id") or ""),
                "text": text}

    # 已落 state 的建议项（vi_ 前缀）
    item = next((it for it in state_items
                 if str(it.get("item_id") or "") == ref), None)
    if item is None:
        raise CanvasInferError("建议项不存在或不属于本线索，请刷新画布")
    if str(item.get("status") or "") != SUGGESTED_STATUS:
        raise CanvasInferError("该建议项已被处理，请刷新画布")
    return {"mode": "transition", "item_id": ref,
            "text": str(item.get("text") or "")}


# ----------------------------------------------------------------------
# 任务终态后协调：pb→vi 迁移 / vi 刷新 / 假设转待核实生成节点
# ----------------------------------------------------------------------
def _vi_props(item: dict[str, Any]) -> dict[str, Any]:
    props = {
        "text": str(item.get("text") or ""),
        "status": str(item.get("status") or ""),
        "kind": str(item.get("kind") or ""),
        "origin": str(item.get("origin") or ""),
        "channel": str(item.get("channel") or ""),
        "ref_function": str(item.get("ref_function") or ""),
        "falsification": str(item.get("falsification") or ""),
    }
    external = item.get("external")
    if isinstance(external, dict) and external:
        props["external"] = dict(external)
    return props


def _migrate_node_id(doc: dict[str, Any], old_id: str, new_id: str,
                     item: dict[str, Any]) -> None:
    """pb 建议节点 → vi 已采纳节点：改 id/ref/props 并重写关联边端点与边 id。"""
    nodes = doc["nodes"]
    idx = next(i for i, n in enumerate(nodes) if n.get("id") == old_id)
    node = nodes[idx]
    node["id"] = new_id
    node["ref"] = str(item.get("item_id") or "")
    node["adopted"] = str(item.get("status") or "") != SUGGESTED_STATUS
    node["label"] = truncate_label(str(item.get("text") or "")) or "待核实"
    preserved = {k: node.get("props", {}).get(k)
                 for k in ("playbook_id", "generated_at")}
    node["props"] = {k: v for k, v in preserved.items() if v} | _vi_props(item)

    kept_edges: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for e in doc.get("edges", []):
        if e.get("source") == old_id:
            e["source"] = new_id
        if e.get("target") == old_id:
            e["target"] = new_id
        incident = e.get("source") == new_id or e.get("target") == new_id
        if incident and e.get("rel") == "手册建议" and not e.get("system"):
            # 「手册建议」是规则节点→核查项的系统溯源边（人工连线矩阵本就
            # 不含该关系）：迁移后仅保留规则侧系统边，丢弃人工/异常来源
            # 的同关系边，避免已采纳核查项挂到假设节点的伪溯源上
            continue
        if incident:
            # 端点迁移后系统/人工边 id 均按确定性公式重算；与既有边撞 id
            # （如同关系人工边已存在）则丢弃重复边
            e["id"] = (f"e:{e.get('source')}--{e.get('rel')}--{e.get('target')}")
        if e["id"] in seen_ids:
            continue
        seen_ids.add(e["id"])
        kept_edges.append(e)
    doc["edges"] = kept_edges


def _refresh_vi_node(node: dict[str, Any], item: dict[str, Any]) -> str:
    """已落 state 节点同步最新字段；返回 adopted/pending/missing 判定态。"""
    status = str(item.get("status") or "")
    node["adopted"] = status != SUGGESTED_STATUS
    node["stale"] = False
    node["label"] = truncate_label(str(item.get("text") or "")) or "待核实"
    props = node.get("props") or {}
    props.update(_vi_props(item))
    node["props"] = props
    return "adopted" if status != SUGGESTED_STATUS else "pending"


def reconcile(doc: dict[str, Any], *, state_items: list[dict[str, Any]],
              targets: list[dict[str, Any]]) -> dict[str, Any]:
    """202 任务终态后的画布协调（原地修改 doc）。

    targets=[{"node_id", "text"?}]；每个目标返回：
      adopted : pb 建议已采纳，节点迁移为 vi（含 item_id/new_node_id）；
      created : 人工假设已转待核实，新增 verify_item 节点（new_node_id）；
      exists  : 假设对应的核查节点画布上已存在（new_node_id）；
      pending : state 中尚未出现目标核查项（任务未完成/失败，前端可重试）；
      missing : 画布节点/state 项不存在。
    """
    by_id = {n.get("id"): n for n in doc.get("nodes", [])}
    by_item = {str(it.get("item_id") or ""): it for it in state_items}
    results: list[dict[str, Any]] = []
    changed = False

    for t in targets or []:
        nid = str(t.get("node_id") or "")
        node = by_id.get(nid)
        if node is None:
            # 容错：调用方传裸 item_id（如 "vi_1"）时，按 verify_item
            # 完整 id 与 ref 回退定位（前端正常路径始终传完整节点 id）
            node = by_id.get(sys_node_id("verify_item", nid))
            if node is None:
                node = next((n for n in doc.get("nodes", [])
                             if n.get("kind") == "verify_item"
                             and str(n.get("ref") or "") == nid), None)
        if node is None:
            results.append({"node_id": nid, "status": "missing"})
            continue
        kind = node.get("kind")
        ref = str(node.get("ref") or "")

        if kind == "verify_item" and is_playbook_ref(ref):
            text = str(t.get("text") or (node.get("props") or {}).get("text")
                       or "").strip()
            item = _find_item_by_text(state_items, text)
            if item is None or str(item.get("status") or "") == SUGGESTED_STATUS:
                results.append({"node_id": nid, "status": "pending"})
                continue
            item_id = str(item.get("item_id") or "")
            new_id = sys_node_id("verify_item", item_id)
            if new_id in by_id and new_id != nid:
                # 极端情况：vi 节点已在画布（并发采纳）——移除 pb 虚节点及其边
                doc["nodes"] = [n for n in doc["nodes"] if n.get("id") != nid]
                doc["edges"] = [e for e in doc.get("edges", [])
                                if e.get("source") != nid
                                and e.get("target") != nid]
            else:
                _migrate_node_id(doc, nid, new_id, item)
                by_id[new_id] = by_id.pop(nid)
            changed = True
            results.append({"node_id": nid, "status": "adopted",
                            "item_id": item_id, "new_node_id": new_id})
            continue

        if kind == "verify_item":
            item = by_item.get(ref)
            if item is None:
                node["stale"] = True
                changed = True
                results.append({"node_id": nid, "status": "missing"})
                continue
            status = _refresh_vi_node(node, item)
            changed = changed or status == "adopted"
            results.append({"node_id": nid, "status": status,
                            "item_id": ref, "new_node_id": nid})
            continue

        if kind == "hypothesis":
            text = str(t.get("text") or "").strip()
            if not text:
                raise CanvasInferError(
                    f"假设转待核实缺少文本：{nid}")
            item = _find_item_by_text(state_items, text)
            if item is None or str(item.get("status") or "") == SUGGESTED_STATUS:
                results.append({"node_id": nid, "status": "pending"})
                continue
            item_id = str(item.get("item_id") or "")
            new_id = sys_node_id("verify_item", item_id)
            if new_id in by_id:
                results.append({"node_id": nid, "status": "exists",
                                "item_id": item_id, "new_node_id": new_id})
                continue
            vn = {
                "id": new_id, "kind": "verify_item", "ref": item_id,
                "label": truncate_label(text) or "待核实",
                "system": True, "pinned": False,
                "adopted": str(item.get("status") or "") != SUGGESTED_STATUS,
                "x": X_VERIFY_COL, "y": _column_next_y(doc, X_VERIFY_COL),
                "props": _vi_props(item),
            }
            doc["nodes"].append(vn)
            by_id[new_id] = vn
            changed = True
            results.append({"node_id": nid, "status": "created",
                            "item_id": item_id, "new_node_id": new_id})
            continue

        results.append({"node_id": nid, "status": "missing"})

    return {"changed": changed, "results": results}


# ----------------------------------------------------------------------
# RC-204：白名单 Function 查询
# ----------------------------------------------------------------------
CANVAS_FUNCTION_WHITELIST = frozenset({
    # 资金追踪类
    "integer_transfer_aggregates",
    "quarter_end_integer_deposits",
    "time_window_collision",
    "overpass_two_hop",
    # 通讯/行为类
    "call_frequency_spike",
    "co_located_pairs",
})

PREVIEW_ROW_LIMIT = 20


def function_forms(functions_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """白名单 → 业务化表单目录（不暴露 sql/impl_ref 等技术细节）。

    输出 [{name, title, description, output_type,
          params: [{key, label, type, enum, default, required}]}]；
    包内未声明的白名单函数静默跳过（案件包差异，不报错）。
    """
    forms: list[dict[str, Any]] = []
    for name in sorted(CANVAS_FUNCTION_WHITELIST):
        spec = functions_spec.get(name)
        if spec is None:
            continue
        params: list[dict[str, Any]] = []
        for key, ps in (getattr(spec, "parameters", None) or {}).items():
            if not isinstance(ps, dict):
                continue
            has_default = "default" in ps
            ptype = str(ps.get("type") or "string")
            if "required" in ps:
                required = bool(ps["required"])
            elif ptype == "string":
                # string 形参仅允许 enum 白名单取值：业务表单默认要求显式选择
                # （即使声明了 default，也不把下拉框渲染成可留空项）
                required = True
            else:
                required = not has_default
            params.append({
                "key": key,
                "label": str(ps.get("description") or key),
                "type": ptype,
                "enum": list(ps["enum"]) if isinstance(
                    ps.get("enum"), list) else None,
                "default": ps.get("default") if has_default else None,
                "required": required,
            })
        forms.append({
            "name": spec.name, "title": spec.title,
            "description": spec.description or "",
            "output_type": spec.output_type, "params": params,
        })
    return forms


def merge_query_params(spec, supplied: dict[str, Any]) -> dict[str, Any]:
    """合并默认值并逐参数做类型/enum 校验（复用模板参数同一决策点）。

    - 未声明参数一律拒绝（审计面不接受噪声键）；
    - string 自由文本在 check_param_value 硬失败（仅 enum 白名单）；
    - 无默认值且调用未提供 → ValueError。
    """
    declared = getattr(spec, "parameters", None) or {}
    unknown = set(supplied or {}) - set(declared)
    if unknown:
        raise CanvasInferError(
            f"函数 {spec.name} 不接受参数：{sorted(unknown)}")
    merged: dict[str, Any] = {}
    for key, ps in declared.items():
        if key in (supplied or {}) and supplied[key] is not None:
            merged[key] = supplied[key]
        elif "default" in ps:
            merged[key] = ps["default"]
        else:
            raise CanvasInferError(f"缺少必填参数：{key}")
        try:
            check_param_value(key, ps, merged[key],
                              ctx=f"画布扩展查询 '{spec.name}'")
        except ValueError as exc:
            # check_param_value 抛原生 ValueError（enum/类型），边界统一翻译
            # 为 CanvasInferError（路由层转 400，不穿透成 500）
            raise CanvasInferError(str(exc)) from exc
    return merged


def _jsonable(v: Any) -> Any:
    """节点 props 入 state.sqlite（JSON）前的标量收敛（Decimal/date → str）。"""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def summarize_output(out: dict[str, Any]) -> dict[str, Any]:
    """Function 执行结果 → 节点/抽屉可展示摘要（前 20 行预览，全量不进画布）。"""
    result = out.get("result")
    rows = out.get("rows")
    if rows is None and isinstance(result, list):
        rows = result
    if rows is None and isinstance(result, dict) \
            and isinstance(result.get("rows"), list):
        # py 函数自描述形态（如 overpass_two_hop：{rows, subject}）
        rows = result["rows"]
    if isinstance(rows, list):
        columns: list[str] = []
        if rows and isinstance(rows[0], dict):
            columns = [str(k) for k in rows[0].keys()]
        preview = [_jsonable(r) for r in rows[:PREVIEW_ROW_LIMIT]]
        summary = {"kind": "rows", "row_count": len(rows),
                   "columns": columns, "preview_rows": preview}
        if isinstance(result, dict):
            meta = {k: _jsonable(v) for k, v in result.items()
                    if k != "rows" and not isinstance(v, (list, dict))}
            if meta:
                summary["meta"] = meta
        return summary
    if result is not None:
        return {"kind": "report", "report": _jsonable(result)}
    return {"kind": "empty", "row_count": 0}


def build_function_result(doc: dict[str, Any], *, spec, out: dict[str, Any],
                          params_used: dict[str, Any],
                          source_node_id: str | None, operator: str,
                          now: str, result_id: str) -> dict[str, Any]:
    """构造 function_result 节点并追加「查询自」系统边（原地改 doc）。

    返回 {node, edge, summary}；source_node_id 为 None/不存在时不建边。
    """
    node_id = sys_node_id("function_result", result_id)
    summary = summarize_output(out)
    node = {
        "id": node_id, "kind": "function_result", "ref": result_id,
        "label": truncate_label(spec.title) or spec.name,
        "system": True, "pinned": False,
        "x": X_RESULT_COL, "y": _column_next_y(doc, X_RESULT_COL),
        "props": {
            "function": spec.name,
            "function_title": spec.title,
            "params": _jsonable(params_used),
            "executed_at": now,
            "executed_by": operator,
            "output_type": spec.output_type,
            **summary,
        },
    }
    doc["nodes"].append(node)

    edge = None
    if source_node_id:
        exists = next((n for n in doc["nodes"]
                       if n.get("id") == source_node_id
                       and n.get("id") != node_id), None)
        if exists is not None:
            edge = _sys_edge(source_node_id, node_id, "查询自")
            doc["edges"].append(edge)
    return {"node": node, "edge": edge, "summary": summary}
