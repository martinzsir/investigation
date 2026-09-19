"""
packs/relation/impl.py
P4 关系研判镜头包实现（经 pack_loader 的 "impl:func" 路径加载）。

镜头只做编排与线索化：
  - 计算全部委托给 ontology Function（relation_*，语义层只读图算法），
    不自己写 SQL、不直接读 Parquet；
  - 把 Function 结果转为 LineageClue，证据引用全部指向真实 obj_*/lnk_* 行
    （skill_invoke 后处理按 P1/P3 契约校验，悬空引用硬失败）；
  - 零命中不造线索，返回 []；数据源缺口在 Function 层落降级诊断，
    有结果但不完整时线索 detail 带 gaps。
"""
from __future__ import annotations

from core.functions import FunctionExecutor
from core.graph import NODE_PK_COLUMN
from core.registry import LineageClue

_NODE_REF_LIMIT = 15
_EDGE_REF_LIMIT = 15
_COMMON_NODE_LIMIT = 10

_SOURCE_TYPE = "关系研判"


def _invoke(store, fn_name: str, fn_params: dict, health) -> dict:
    return FunctionExecutor(store, health=health).invoke(fn_name, fn_params)


def _clean(params: dict, keys: list[str]) -> dict:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _node_ref(node: dict) -> dict:
    # obj_* 表无统一 "pk" 列，引用必须带声明的行键列（person_id/account_id/...）
    obj_type = node["type"]
    return {"kind": "node", "ref": f"obj_{obj_type}#{node['pk']}",
            "key_column": NODE_PK_COLUMN[obj_type]}


def _edge_ref(edge: dict) -> dict:
    # Function 结果的 edge.ref 已含 kind/ref/key_column（graph.SEdge.ref）
    return edge["ref"]


def neighborhood_lens(miao=None, store=None, ctx=None, params=None,
                      health=None) -> list:
    """目标主体 N 跳关系圈层 → 1 条聚合线索（节点/边/聚合量引用）。"""
    params = params or {}
    fn_params = _clean(params, ["target_type", "depth", "edge_kinds"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "relation_neighborhood", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        return []

    subject, nodes, edges = r["subject"], r["nodes"], r["edges"]
    refs = [_node_ref(n) for n in nodes[: _NODE_REF_LIMIT + 1]]
    seen_edges: set = set()
    for e in edges:
        sig = (e["edge"], e["edge_pk"])
        if sig not in seen_edges and len(
                [x for x in refs if x["kind"] == "edge"]) < _EDGE_REF_LIMIT:
            refs.append(_edge_ref(e))
            seen_edges.add(sig)
    refs.append({"kind": "aggregate", "metric": "neighbor_nodes",
                 "value": len(nodes) - 1})
    refs.append({"kind": "aggregate", "metric": "tree_edges",
                 "value": len(edges)})

    depth = fn_params.get("depth", 2)
    clue = LineageClue(
        skill_id="relation_neighborhood",
        title=f"{subject['name']} 的 {depth} 跳关系圈层："
              f"{len(nodes) - 1} 个关联主体、{len(edges)} 条关系",
        evidence_refs=refs,
        detail={
            "function": "relation_neighborhood",
            "source_type": _SOURCE_TYPE,
            "hypothesis": f"{subject['name']} 在 {depth} 跳内与上述主体存在"
                          f"资金/通讯/持有/中标/同框关联，圈层关系待正兵核查",
            "evidence_level": "观察",
            "subject": subject,
            "depth": depth,
            "edge_kinds": fn_params.get("edge_kinds", "all"),
            "nodes": nodes,
            "edges": edges,
            "diagnostics": r.get("diagnostics", {}),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def common_neighbors_lens(miao=None, store=None, ctx=None, params=None,
                          health=None) -> list:
    """两主体共同关系邻居 → 1 条聚合线索。"""
    params = params or {}
    fn_params = _clean(params, ["edge_kinds"])
    fn_params["subject_a"] = params.get("subject_a", "")
    fn_params["subject_b"] = params.get("subject_b", "")
    out = _invoke(store, "relation_common_neighbors", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        return []

    sa, sb, common = r["subject_a"], r["subject_b"], r["common"]
    refs = [_node_ref(sa), _node_ref(sb)]
    for item in common[:_COMMON_NODE_LIMIT]:
        refs.append(_node_ref(item))
    seen_edges: set = set()
    for item in common:
        for side in ("via_a", "via_b"):
            e = item[side]
            sig = (e["edge"], e["edge_pk"])
            if sig not in seen_edges and len(
                    [x for x in refs if x["kind"] == "edge"]) < _EDGE_REF_LIMIT:
                refs.append(_edge_ref(e))
                seen_edges.add(sig)
    refs.append({"kind": "aggregate", "metric": "common_neighbor_count",
                 "value": r["count"]})

    names = "、".join(c["name"] for c in common[:5])
    if r["count"] > 5:
        names += f" 等 {r['count']} 个"
    clue = LineageClue(
        skill_id="relation_common_neighbors",
        title=f"{sa['name']} 与 {sb['name']} 存在 {r['count']} 个"
              f"共同关系邻居（{names}）",
        evidence_refs=refs,
        detail={
            "function": "relation_common_neighbors",
            "source_type": _SOURCE_TYPE,
            "hypothesis": f"{sa['name']} 与 {sb['name']} 共享关系圈层，"
                          f"可能存在共同关联方/过桥节点，待正兵核查",
            "evidence_level": "观察",
            "subject_a": sa,
            "subject_b": sb,
            "common": common,
            "count": r["count"],
            "diagnostics": r.get("diagnostics", {}),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def paths_lens(miao=None, store=None, ctx=None, params=None,
               health=None) -> list:
    """两主体间关系路径 → 每条简单路径 1 条线索。"""
    params = params or {}
    fn_params = _clean(params, ["depth", "max_paths", "edge_kinds"])
    fn_params["subject_a"] = params.get("subject_a", "")
    fn_params["subject_b"] = params.get("subject_b", "")
    out = _invoke(store, "relation_paths", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        return []

    sa, sb = r["subject_a"], r["subject_b"]
    clues = []
    for i, path in enumerate(r["paths"], start=1):
        chain = " → ".join(n["name"] for n in path["nodes"])
        edge_names = "、".join(
            f"{e['edge']}({'反向' if e.get('reversed_traversal') else '正向'})"
            for e in path["edges"])
        refs = [_node_ref(n) for n in path["nodes"]]
        refs += [_edge_ref(e) for e in path["edges"]]
        refs.append({"kind": "aggregate", "metric": "path_length",
                     "value": path["length"]})
        clues.append(LineageClue(
            skill_id="relation_paths",
            title=f"{sa['name']} 与 {sb['name']} 的关系链 {i}"
                  f"（{path['length']} 跳）：{chain}",
            evidence_refs=refs,
            detail={
                "function": "relation_paths",
                "source_type": _SOURCE_TYPE,
                "hypothesis": f"{sa['name']} 经 {edge_names} 与 {sb['name']} 连通，"
                              f"关系路径待正兵逐跳核查（只出关系，不作定性）",
                "evidence_level": "观察",
                "path_index": i,
                "subject_a": sa,
                "subject_b": sb,
                "nodes": path["nodes"],
                "edges": path["edges"],
                "length": path["length"],
                "diagnostics": r.get("diagnostics", {}),
                "degraded": bool(r.get("degraded")),
                "degraded_reason": r.get("degraded_reason"),
            },
        ))
    return clues
