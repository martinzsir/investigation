"""
server/app/canvas_writeback.py
案件画布镜头观察回写（P3 生长能力，PRD V1.0.0 功能 4）。

定向镜头（origin.surface == "case_canvas"）完成后，把观察产物回写为
案件级画布 analysis_result 节点：

  - 幂等：节点 id 确定性派生 `case#ar:{observation_id}`（observation_id
    本身由 skill_id+靶心+参数指纹稳定派生，core/observation.py），worker
    重试 / 同靶心重跑不重复上图，已存在节点仅补缺失连线后计数跳过；
  - 挂靶心：origin.node_id 指向的画布节点存在时挂「涉及」系统边
    （system=True，不可人工删）；靶心已不在画布（被删/版本演进）则
    跳过挂边不报错——观察节点本身仍上图，人工可再补连线；
  - 挂假设：pack.json 技能声明 assumption（如 "H6"）且画布存在
    props.assumption_id 匹配的 hypothesis 节点时，自动挂「支撑」人工边
    （system=False，可删可补挂）；未声明 = 业务未确认归属 → 仅挂靶心
    不自动挂假设（PRD 开放项③，避免系统替正兵归因）；
  - 后写覆盖（expected_version=None）：回写是机器动作不与人抢版本；
    人的编辑以服务端最终文档为准（前端轮询刷新后可见）。

纪律与 canvas_edit.py 一致：build_writeback_doc 纯函数受控变更、不开库、
不读 Parquet；writeback_observations 是唯一落库入口（StateStore）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from server.app.canvas_case import case_canvas_id, seed_case_doc
from server.app.store.state_store import StateStore

# 回写节点 id 前缀（案件域 case# 下再分 ar: 段，与人工 case#cn_ 隔离）
AR_NODE_PREFIX = "case#ar:"
# 挂靶心系统边（与 SYSTEM_RELS「涉及」同口径）
REL_FOCAL = "涉及"
# 挂假设人工边（研判边，用户可删可补挂）
REL_SUPPORT = "支撑"

LENS_ID_MAX = 64
LABEL_MAX = 28


def analysis_result_node_id(observation_id: str) -> str:
    """回写节点确定性 id：同观察重跑天然幂等撞键。"""
    return f"{AR_NODE_PREFIX}{observation_id}"


def _label(text: Any, limit: int = LABEL_MAX) -> str:
    raw = str(text or "").strip().replace("\n", " ")
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def build_writeback_doc(doc: dict[str, Any],
                        observations: list[Any], *,
                        assumption_of: Callable[[str], str] | None = None,
                        operator: str, now: str) -> dict[str, int]:
    """把观察回写进画布文档（就地变更），返回摘要计数。

    observations：core.observation.Observation 列表（含 origin 上下文）；
    assumption_of：skill_id → 庙算假设编号（如 "H6"），未声明返回 ""。
    """
    summary = {"nodes_added": 0, "edges_added": 0,
               "skipped": 0, "hypothesis_linked": 0}
    nodes = doc.setdefault("nodes", [])
    edges = doc.setdefault("edges", [])
    by_id: dict[str, dict[str, Any]] = {
        str(n.get("id")): n for n in nodes if isinstance(n, dict)}
    edge_keys = {(str(e.get("source")), str(e.get("rel")),
                  str(e.get("target"))) for e in edges if isinstance(e, dict)}

    def _add_edge(source: str, rel: str, target: str, *,
                  system: bool) -> None:
        eid = f"e:{source}--{rel}--{target}"
        key = (source, rel, target)
        if key in edge_keys:
            return
        edge: dict[str, Any] = {
            "id": eid, "source": source, "target": target,
            "rel": rel, "system": system,
            "created_by": operator, "created_at": now,
        }
        edges.append(edge)
        edge_keys.add(key)
        summary["edges_added"] += 1

    for idx, obs in enumerate(observations):
        oid = str(getattr(obs, "observation_id", "") or "")
        if not oid:
            continue
        nid = analysis_result_node_id(oid)
        origin = getattr(obs, "origin", None)
        origin = origin if isinstance(origin, dict) else {}
        focal_id = str(origin.get("node_id") or "").strip()

        if nid in by_id:
            # 幂等：同观察重跑不重建节点，仅确保连线齐全
            summary["skipped"] += 1
        else:
            focal = by_id.get(focal_id)
            if focal is not None:
                x = float(focal.get("x", 0) or 0) + 300
                y = float(focal.get("y", 0) or 0) + idx * 80 - 40
            else:
                x, y = 320.0 + idx * 40.0, 120.0 + idx * 80.0
            props: dict[str, Any] = {
                "lens_id": str(getattr(obs, "skill_id", ""))[:LENS_ID_MAX],
                "observation_id": oid,
                "subject": str(getattr(obs, "subject", "")
                               or getattr(obs, "project", "")),
                "degraded": bool(getattr(obs, "degraded", False)),
            }
            dr = str(getattr(obs, "degraded_reason", "") or "")
            if dr:
                props["degraded_reason"] = dr
            node = {
                "id": nid,
                "kind": "analysis_result",
                "ref": oid,
                "label": _label(getattr(obs, "title", "")) or "镜头结论",
                # 系统产物但属用户交互对象（可连线/钉住/移动）；
                # 编辑与删除不在 CASE_MANUAL_NODE_KINDS 白名单内，
                # 移除靠重跑覆盖或后续批次放开
                "system": False,
                "pinned": False,
                "x": x, "y": y,
                "props": props,
                "created_by": operator,
                "created_at": now,
                "updated_at": now,
            }
            nodes.append(node)
            by_id[nid] = node
            summary["nodes_added"] += 1

        # 挂靶心：靶心节点已不在画布则跳过（不报错，人工可再补）
        if focal_id and focal_id in by_id:
            _add_edge(nid, REL_FOCAL, focal_id, system=True)

        # 挂假设：仅当技能声明 assumption 且画布存在匹配 hypothesis 节点
        assumption = (assumption_of or (lambda _sid: ""))(
            str(getattr(obs, "skill_id", "")))
        if assumption:
            hyp = next(
                (n for n in nodes
                 if isinstance(n, dict) and n.get("kind") == "hypothesis"
                 and str((n.get("props") or {}).get("assumption_id") or "")
                 == assumption),
                None)
            if hyp is not None and hyp.get("id"):
                _add_edge(nid, REL_SUPPORT, str(hyp["id"]), system=False)
                summary["hypothesis_linked"] += 1
    return summary


def writeback_observations(
        case_id: str, *, state_path: Any, observations: list[Any],
        assumption_of: Callable[[str], str] | None = None,
        operator: str, now: str | None = None) -> dict[str, int] | None:
    """观察回写落库唯一入口（worker 调用）；零观察返回 None。

    画布不存在时惰性 seed（与 GET 端点同语义）；有变更才推进版本
    （后写覆盖，expected_version=None）。
    """
    if not observations:
        return None
    now = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    canvas_id = case_canvas_id(case_id)
    store = StateStore(case_id, state_path)
    try:
        row = store.get_canvas(canvas_id)
        if row is None:
            store.insert_canvas(clue_id=canvas_id, doc=seed_case_doc(),
                                created_by=operator, created_at=now)
            row = store.get_canvas(canvas_id)
        doc = dict(row["doc"]) if row and isinstance(row.get("doc"), dict) \
            else seed_case_doc()
        summary = build_writeback_doc(
            doc, observations, assumption_of=assumption_of,
            operator=operator, now=now)
        if summary["nodes_added"] or summary["edges_added"]:
            store.update_canvas_doc(canvas_id, doc, operator=operator,
                                    updated_at=now)
        return summary
    finally:
        store.close()
