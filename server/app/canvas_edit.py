"""
server/app/canvas_edit.py
线索研判画布人工编辑纯函数（M3：RC-202 人工节点 / RC-203 连线矩阵 /
RC-206 回滚失效标记）。

纪律（与 canvas_seed.py 一致）：
  - 纯函数、不开库、不读 Parquet、不生成 SQL；只在传入的 CanvasDoc 上做
    受控变更（调用方负责深拷贝/落库）；
  - 系统节点（system=true）仅可移动（坐标/钉住走 M1 PATCH 白名单），
    人工增删改一律拒绝；系统边不可改/删；
  - 人工关系合法性矩阵是唯一真相（前后端各一份穷举单测固定）：
      fact / object          → hypothesis：推断为
      verify_item / evidence → hypothesis：证实、查否
      note                   → 任意节点  ：补充说明（方向固定）
      任意节点               → note      ：不允许
      其他任意组合                         ：全部拒绝
    通用拒绝：自连；同两端同关系重复边；目标为 source_row/source_file
    （这些节点只能有系统边）。
  - 人工节点 id 服务端签发（cn_ 前缀），ref 与 id 同值；人工边 id 与
    seed/expand 同口径 e:{source}--{rel}--{target}（重复边天然撞 id）。
"""
from __future__ import annotations

import uuid
from typing import Any

# 人工节点只有两类：假设 / 备注
MANUAL_NODE_KINDS = frozenset({"hypothesis", "note"})

# 人工关系 4 类（与 canvas_seed.SYSTEM_RELS 互斥）
MANUAL_RELS = frozenset({"推断为", "证实", "查否", "补充说明"})

# 推断为：事实/实体 → 假设
_INFER_SOURCES = frozenset({"fact", "object"})
# 证实/查否：待核实/书证 → 假设
_VERDICT_SOURCES = frozenset({"verify_item", "evidence"})
# 人工关系禁入目标：只能承载系统边
_FORBIDDEN_TARGETS = frozenset({"source_row", "source_file"})

HYPOTHESIS_TITLE_MAX = 50
CONTENT_MAX = 500
EDGE_NOTE_MAX = 200
SNAPSHOT_LABEL_MAX = 100

_LABEL_LIMIT = 28


class CanvasEditError(ValueError):
    """人工编辑业务校验失败：路由层统一转 400 VALIDATION。"""


# ----------------------------------------------------------------------
# id 签发
# ----------------------------------------------------------------------
def manual_node_id() -> str:
    """人工节点画布内稳定 id（cn_ 前缀 + 随机段）。"""
    return f"cn_{uuid.uuid4().hex[:12]}"


def manual_edge_id(source: str, rel: str, target: str) -> str:
    """人工边 id：与 seed/expand 同口径，同两端同关系天然幂等撞键。"""
    return f"e:{source}--{rel}--{target}"


# ----------------------------------------------------------------------
# RC-203：合法性矩阵（穷举纯函数）
# ----------------------------------------------------------------------
def can_connect(src_kind: str, tgt_kind: str, rel: str) -> tuple[bool, str]:
    """返回 (是否允许, 业务原因)；原因仅在拒绝时非空，供前端拒连提示。

    本函数只判定 kind×kind×rel 矩阵与通用拒绝（自连/禁入目标/备注方向）；
    端点存在性、重复边由 add_manual_edge 在具体文档上判定。
    """
    if rel not in MANUAL_RELS:
        return False, "非法人工关系类型"
    if src_kind == tgt_kind and src_kind == "note" and tgt_kind == "note":
        # 落在「任意节点 → note 不允许」（备注方向固定为源）
        return False, "备注节点只能作为连线起点"
    if tgt_kind == "note":
        return False, "备注节点只能作为连线起点"
    if src_kind == tgt_kind:
        return False, "不能连接节点自身"
    if tgt_kind in _FORBIDDEN_TARGETS:
        return False, "数据行/数据源节点仅可由系统溯源连线关联"

    if rel == "补充说明":
        # note → 任意（已排除 note 目标、自连、数据行/数据源禁入）
        if src_kind == "note":
            return True, ""
        return False, "「补充说明」只能由备注节点发起"

    if tgt_kind != "hypothesis":
        return False, "该两类节点不能建立该关系"
    if rel == "推断为" and src_kind in _INFER_SOURCES:
        return True, ""
    if rel in ("证实", "查否") and src_kind in _VERDICT_SOURCES:
        return True, ""
    return False, "该两类节点不能建立该关系"


def allowed_rels(src_kind: str, tgt_kind: str) -> list[str]:
    """矩阵正向枚举：给定两端允许的人工关系（关系选择气泡用）。"""
    return [rel for rel in ("推断为", "证实", "查否", "补充说明")
            if can_connect(src_kind, tgt_kind, rel)[0]]


# ----------------------------------------------------------------------
# RC-202：字段校验（前后端同口径，后端兜底硬拒绝）
# ----------------------------------------------------------------------
def _clean_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CanvasEditError(f"{field}必须是文本")
    return value.strip()


def validate_node_props(kind: str, props: Any) -> dict[str, str]:
    """校验并清洗人工节点 props，返回清洗后的 props；失败抛 CanvasEditError。

    hypothesis：title 1..50（去空格后非空）、content 1..500 非空
    note      ：content 1..500 非空
    """
    if kind not in MANUAL_NODE_KINDS:
        raise CanvasEditError(f"非法人工节点类型：{kind}")
    if not isinstance(props, dict):
        raise CanvasEditError("节点内容必须是对象")

    cleaned: dict[str, str] = {}
    if kind == "hypothesis":
        title = _clean_text(props.get("title"), "假设标题")
        if not title:
            raise CanvasEditError("请填写假设标题")
        if len(title) > HYPOTHESIS_TITLE_MAX:
            raise CanvasEditError(
                f"假设标题不超过 {HYPOTHESIS_TITLE_MAX} 字")
        cleaned["title"] = title
    content = _clean_text(props.get("content"),
                          "假设内容" if kind == "hypothesis" else "备注内容")
    field_label = "假设内容" if kind == "hypothesis" else "备注内容"
    if not content:
        raise CanvasEditError(f"请填写{field_label}")
    if len(content) > CONTENT_MAX:
        raise CanvasEditError(f"{field_label}不超过 {CONTENT_MAX} 字")
    cleaned["content"] = content
    return cleaned


def validate_edge_note(note: Any) -> str:
    """人工边备注：可选，去空格后 ≤200；空串归一为 ''。"""
    if note is None:
        return ""
    if not isinstance(note, str):
        raise CanvasEditError("连线备注必须是文本")
    note = note.strip()
    if len(note) > EDGE_NOTE_MAX:
        raise CanvasEditError(f"备注不超过 {EDGE_NOTE_MAX} 字")
    return note


def validate_snapshot_label(label: Any) -> str:
    """手动快照备注：必填 1..100（去空格后非空）。"""
    if not isinstance(label, str):
        raise CanvasEditError("快照备注必须是文本")
    label = label.strip()
    if not label:
        raise CanvasEditError("请填写快照备注（不超过 100 字）")
    if len(label) > SNAPSHOT_LABEL_MAX:
        raise CanvasEditError(f"快照备注不超过 {SNAPSHOT_LABEL_MAX} 字")
    return label


def _coordinate(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanvasEditError(f"{field}必须是数值")
    return float(value)


def _truncate(text: str, limit: int = _LABEL_LIMIT) -> str:
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _manual_label(kind: str, props: dict[str, str]) -> str:
    if kind == "hypothesis":
        return _truncate(props["title"])
    return _truncate(props["content"])


# ----------------------------------------------------------------------
# 文档受控变更（调用方保证 doc 已存在；函数内就地修改）
# ----------------------------------------------------------------------
def _find_node(doc: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    return next((n for n in doc.get("nodes", [])
                 if n.get("id") == node_id), None)


def _require_manual_node(doc: dict[str, Any], node_id: str,
                         *, action: str) -> dict[str, Any]:
    node = _find_node(doc, node_id)
    if node is None:
        raise CanvasEditError(f"画布节点不存在：{node_id}")
    if node.get("system") is True:
        raise CanvasEditError(
            f"系统节点不可{action}（仅可移动/钉住）")
    if node.get("kind") not in MANUAL_NODE_KINDS:
        # 防御：非系统节点却不是人工类型，同样拒绝
        raise CanvasEditError(f"该节点类型不支持{action}")
    return node


def add_manual_node(doc: dict[str, Any], *, kind: str, props: dict[str, str],
                    node_id: str, x: Any, y: Any,
                    operator: str, now: str) -> dict[str, Any]:
    """新增人工节点（幂等性由随机 id 保证；ref=id）。"""
    if _find_node(doc, node_id) is not None:
        raise CanvasEditError(f"节点 id 已存在：{node_id}")
    node = {
        "id": node_id,
        "kind": kind,
        "ref": node_id,
        "label": _manual_label(kind, props),
        "system": False,
        "pinned": False,
        "x": _coordinate(x, "x"),
        "y": _coordinate(y, "y"),
        "props": props,
        "created_by": operator,
        "created_at": now,
        "updated_at": now,
    }
    doc.setdefault("nodes", []).append(node)
    return node


def update_manual_node(doc: dict[str, Any], *, node_id: str,
                       props: dict[str, str], now: str) -> dict[str, Any]:
    """编辑人工节点标题/内容（label 同步重算；created_by/at 不动）。"""
    node = _require_manual_node(doc, node_id, action="编辑")
    # 按该节点既有 kind 重新校验（不允许借编辑改类型）
    cleaned = validate_node_props(node["kind"], props)
    node["props"] = cleaned
    node["label"] = _manual_label(node["kind"], cleaned)
    node["updated_at"] = now
    return node


def delete_manual_node(doc: dict[str, Any], *, node_id: str) -> list[str]:
    """删除人工节点；级联删除其**人工边**，系统边一律保留。

    返回被删除的人工边 id 列表（供审计与前端文案）。
    """
    _require_manual_node(doc, node_id, action="删除")
    doc["nodes"] = [n for n in doc["nodes"] if n.get("id") != node_id]
    removed: list[str] = []
    kept: list[dict[str, Any]] = []
    for e in doc.get("edges", []):
        incident = e.get("source") == node_id or e.get("target") == node_id
        if incident and e.get("system") is False:
            removed.append(e["id"])
            continue
        if incident and e.get("system") is True:
            # 矩阵上人工节点不应存在系统边；防御性保留并继续
            continue
        kept.append(e)
    doc["edges"] = kept
    return removed


def add_manual_edge(doc: dict[str, Any], *, source: str, target: str,
                    rel: str, note: str, operator: str,
                    now: str) -> dict[str, Any]:
    """新增人工边：端点存在 → 矩阵 → 重复边，任一不过抛 CanvasEditError。"""
    src_node = _find_node(doc, source)
    tgt_node = _find_node(doc, target)
    if src_node is None or tgt_node is None:
        raise CanvasEditError("连线端点不存在，请刷新画布后重试")
    ok, reason = can_connect(str(src_node.get("kind")),
                             str(tgt_node.get("kind")), str(rel))
    if not ok:
        raise CanvasEditError(reason)
    eid = manual_edge_id(source, rel, target)
    if any(e.get("id") == eid
           or (e.get("source") == source and e.get("target") == target
               and e.get("rel") == rel)
           for e in doc.get("edges", [])):
        raise CanvasEditError("该关系已存在")
    edge = {
        "id": eid,
        "source": source,
        "target": target,
        "rel": rel,
        "system": False,
        "created_by": operator,
        "created_at": now,
    }
    if note:
        edge["note"] = note
    doc.setdefault("edges", []).append(edge)
    return edge


def delete_manual_edge(doc: dict[str, Any], *, edge_id: str) -> dict[str, Any]:
    """删除人工边；系统边/不存在一律拒绝（RC-203 AC-3）。"""
    edge = next((e for e in doc.get("edges", [])
                 if e.get("id") == edge_id), None)
    if edge is None:
        raise CanvasEditError(f"连线不存在：{edge_id}")
    if edge.get("system") is True:
        raise CanvasEditError("系统推断关系不可修改或删除")
    doc["edges"] = [e for e in doc["edges"] if e.get("id") != edge_id]
    return edge


def incident_manual_edge_ids(doc: dict[str, Any], node_id: str) -> list[str]:
    """节点关联的人工边 id（删除确认文案「将同时删除 N 条连线」）。"""
    return [e["id"] for e in doc.get("edges", [])
            if e.get("system") is False
            and (e.get("source") == node_id or e.get("target") == node_id)]


# ----------------------------------------------------------------------
# RC-206：回滚后失效引用标记（仅标记表达层，不改业务事实）
# ----------------------------------------------------------------------
def mark_stale_refs(doc: dict[str, Any], *,
                    live_item_ids: set[str],
                    live_material_ids: set[str]) -> list[str]:
    """把快照文档中业务对象已不存在的 verify_item/evidence 节点置 stale。

    回滚只还原画布表达层：被引用的核查项/书证若已被删除，节点灰态
    「引用已失效」而不是让整图报错。返回被标记的节点 id。
    """
    stale_ids: list[str] = []
    for n in doc.get("nodes", []):
        kind = n.get("kind")
        if kind not in ("verify_item", "evidence"):
            continue
        alive = (n.get("ref") in live_item_ids
                 if kind == "verify_item"
                 else n.get("ref") in live_material_ids)
        if alive:
            n.pop("stale", None)
        else:
            n["stale"] = True
            stale_ids.append(str(n.get("id")))
    return stale_ids
