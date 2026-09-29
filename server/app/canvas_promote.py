"""证据实体 → 研判主体的提升（CAN-11 / CAN-12）。

为什么不是"原地改"
------------------
既有 object 节点 ``system=true``，人工增删改一律拒绝；其 props 只有
type/pk/label，写死不可变。所以提升只能是：**新建一个锚定真实主键的
subject 节点，与原节点建系统边**，原节点保留在溯源层（灰卡，可随时钻回去）。

三条红线
--------
R-1 重名不自裁：候选多主键且正兵未指定时，pk 置 None + ambiguous。
    绝不"挑第一个"——图长得越漂亮，证据错配越难发现。
R-2 不允许伪造锚定：``requested_pk`` 必须落在候选集合内，否则拒绝。
    否则正兵随手传一个键就能绕过消歧，红线形同虚设。
R-3 不静默丢弃：无论能否锚定都要给出可读状态与原因。查不到就是查不到，
    不退回"按名派生键"假装锚定成功。
"""
from __future__ import annotations

from typing import Any

from server.app.canvas_case import (
    PROMOTE_REL,
    build_subject_node,
    case_node_id,
    promote_edge,
)

# object 节点里承载"人"的对象类型
_PERSON_TYPES = frozenset({"person", "人员", "主体"})
# 子类型推断：object 类型 → subject 子类型
_SUB_TYPE_OF = {
    "person": "person", "人员": "person", "主体": "person",
    "org": "organization", "organization": "organization", "单位": "organization",
    "project": "project", "项目": "project",
    "item": "item", "物品": "item",
}


class PromoteError(ValueError):
    """提升业务校验失败：路由层统一转 400。"""


def _looks_like_pk(value: Any) -> bool:
    """是否像代理键（person_/obj_ 等前缀 + 十六进制段）。"""
    s = str(value or "").strip()
    if not s:
        return False
    return s.startswith(("person_", "obj_", "loc_", "item_", "org_"))


def object_subject_name(node: dict[str, Any]) -> str:
    """从 object 节点取主体名。

    label 是截断过的（_LABEL_LIMIT），不能直接当名字用——截断命中时
    查语义层必然落空。优先取 props.name，其次 label，最次 pk。
    """
    props = node.get("props") or {}
    for key in ("name", "raw_name", "主体", "subject"):
        v = props.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    label = str(node.get("label") or "").strip()
    return label


def resolve_object_pks(node: dict[str, Any],
                       conn: Any) -> tuple[list[str], str]:
    """解析 object 节点的主键候选，返回 ``(候选列表, 来源)``。

    来源取值（**必须区分**，否则会踩下面这个坑）：
      · semantic           —— 语义层核对命中，可信
      · node_pk_unverified —— 无语义层连接，退回节点自带键，未经核对
      · node_pk_unmatched  —— 有连接但语义层查无此人，**不得据此锚定**
      · none               —— 无从取得任何键

    坑（本轮实测踩到）：最初把"语义层查不到"也退回节点自带键，于是
    ``FakeConn({})`` 这种"库里没人"的情形照样判成 promoted。锚定成功却
    跑不出任何证据，且因为"已锚定"不会有任何提示——正兵会以为是工具坏了。
    这正是 R-3 要防的"用看起来像主键的东西假装锚定成功"。
    """
    from core.entity_ref import pk_candidates

    name = object_subject_name(node)
    pks: list[str] = []
    if conn is not None and name:
        pks = list(pk_candidates(conn, name) or [])
    if pks:
        return _dedup(pks), "semantic"

    pk = (node.get("props") or {}).get("pk")
    if _looks_like_pk(pk):
        # 有连接时说明语义层确实没这个人：保留作候选供人工确认，但不锚定
        return [str(pk)], ("node_pk_unverified" if conn is None
                           else "node_pk_unmatched")
    return [], "none"


def _dedup(pks: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in pks:
        s = str(p or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def promote_object_to_subject(
    *,
    case_id: str,
    object_node: dict[str, Any],
    conn: Any = None,
    requested_pk: str | None = None,
    sub_type: str | None = None,
) -> dict[str, Any]:
    """把证据实体节点提升为研判主体节点。

    返回 ``{status, subject, edge, reason}``。status 取值：
      · promoted       —— 已锚定唯一主键
      · ambiguous      —— 多候选且未指定，pk 置空待裁决（R-1）
      · unanchored     —— 语义层查无此人，节点仍建但不可用（R-3）
      · rejected       —— 输入非法或 requested_pk 不在候选内（R-2）

    ambiguous / unanchored **照样返回节点与边**——主体指代不明不等于
    这条提升关系不存在，丢弃会让正兵以为"系统没认出这个人"。
    """
    if not isinstance(object_node, dict):
        raise PromoteError("提升源必须是节点对象")
    if object_node.get("kind") != "object":
        raise PromoteError(
            f"只有证据实体节点可提升为研判主体，当前类型：{object_node.get('kind')}")

    otype = str((object_node.get("props") or {}).get("type") or "").strip()
    st = sub_type or _SUB_TYPE_OF.get(otype, "person")
    if st not in ("person", "organization", "project", "item"):
        raise PromoteError(f"非法主体子类型：{st}")

    name = object_subject_name(object_node)
    pks, pk_source = resolve_object_pks(object_node, conn)

    # R-2：指定键必须在候选内，否则拒绝（不允许绕过消歧）
    if requested_pk:
        if str(requested_pk) not in pks:
            raise PromoteError(
                "指定的主键不在该主体的候选集合内，无法锚定；"
                f"候选共 {len(pks)} 个" + (f"：{', '.join(pks[:5])}" if pks else "（无）"))

    # 锚定判定。node_pk_unmatched（语义层查无此人）时**不自动锚定**——
    # 正兵显式指定除外（那等于人工确认，例如数据尚未导入）。
    chosen = None
    if requested_pk:
        chosen = str(requested_pk)
    elif pk_source != "node_pk_unmatched" and len(pks) == 1:
        chosen = pks[0]

    subject = build_subject_node(
        case_id=case_id, name=name, sub_type=st, conn=conn)
    if chosen:
        # 指定锚定：覆盖 build_subject_node 的自裁规避结果
        subject["id"] = case_node_id("subject", case_id, chosen)
        subject["props"]["person_pk"] = chosen
        subject["props"]["person_pk_ambiguous"] = False
        subject["props"]["anchored"] = True
        subject["props"]["pk_candidates"] = pks
        subject["props"]["pk_resolution"] = (
            f"已按人工指定锚定主键（候选共 {len(pks)} 个，已裁决）")
    else:
        subject["props"]["pk_candidates"] = pks

    # 提升来源自陈：正兵随时能知道这个主体是从哪条证据来的。
    # refs 用列表而非单值——同一个「张卫国」常出现在多条线索里，单值会被
    # 后一次提升覆盖，"哪些线索提到他"这个信息就静默丢了，而它恰恰是
    # 判断要不要继续查的依据。
    _oid = str(object_node.get("id") or "")
    subject["props"]["promoted_from"] = _oid
    subject["props"]["promoted_from_refs"] = [_oid] if _oid else []
    subject["props"]["origin_count"] = 1 if _oid else 0
    subject["props"]["promoted_from_type"] = otype
    subject["props"]["pk_source"] = pk_source
    # 无语义层连接时退回节点自带键：可锚定但**未经核对**，必须看得见
    subject["props"]["pk_verified"] = pk_source in ("semantic",)

    ambiguous = bool(subject["props"].get("person_pk_ambiguous"))
    anchored = bool(subject["props"].get("anchored"))

    if ambiguous:
        status = "ambiguous"
        reason = (f"「{name}」存在 {len(pks)} 个候选主键（同名异人），"
                  f"未锚定；已建节点待裁决，暂不可用于研判")
    elif not anchored:
        status = "unanchored"
        if not name:
            reason = "主体名为空，未锚定"
        elif pk_source == "node_pk_unmatched":
            reason = (f"语义层未收录「{name}」，该主体未锚定，无可用研判"
                      f"（节点自带标识已保留为候选，可人工确认后再锚定）")
        else:
            reason = f"语义层未收录「{name}」，该主体未锚定，无可用研判"
    elif not subject["props"]["pk_verified"]:
        status = "promoted"
        reason = ("已按节点自带标识锚定，但**未经语义层核对**"
                  "（未连接语义层）；研判结果需人工复核")
    else:
        status = "promoted"
        reason = "已锚定唯一主键，可发起研判"

    return {
        "status": status,
        "subject": subject,
        "edge": promote_edge(case_id=case_id,
                             object_node_id=str(object_node.get("id") or ""),
                             subject_node=subject),
        "keeps_source": True,          # 原 object 保留在溯源层
        "candidates": pks,
        "reason": reason,
    }


def _merge_promotion_source(existing: dict[str, Any], incoming: dict[str, Any],
                            object_node_id: str) -> None:
    """同主体二次提升：追加来源证据，并在本次已裁决时更新锚定状态。

    为什么不能只判重复就跳过：``promoted_from_refs`` 若只剩第一条线索，
    正兵点开溯源标记只会看到一个来源——而"这个人被几条线索提到"正是判断
    要不要继续查的依据。丢了它，标记就成了摆设。

    为什么不整体覆盖：既有节点可能已被移动过/挂过研判结果，用第二次提升
    的结果整体替换会把正兵已经做过的事抹掉。所以只补来源，并且只在
    "本次已裁决、既有未锚定"这种明确更优的情形下更新锚定字段。
    """
    props = existing.setdefault("props", {})
    refs = props.setdefault("promoted_from_refs", [])
    if object_node_id and object_node_id not in refs:
        refs.append(object_node_id)
    props["origin_count"] = len(refs)

    inc = incoming.get("props") or {}
    if inc.get("anchored") and not props.get("anchored"):
        for key in ("person_pk", "anchored", "person_pk_ambiguous",
                    "pk_status", "pk_resolution", "pk_source", "pk_verified"):
            if key in inc:
                props[key] = inc[key]


def promote_all(*, case_id: str, nodes: list[dict[str, Any]],
                conn: Any = None,
                requested: dict[str, str] | None = None) -> dict[str, Any]:
    """批量提升：同一 object 只提升一次（幂等由节点 id 天然保证）。

    ``requested`` 形如 ``{object_node_id: pk}``——正兵在候选里指定过的才传。
    """
    requested = requested or {}
    out_nodes: list[dict[str, Any]] = []
    out_edges: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    reports: list[dict[str, Any]] = []

    # 同一主体的多次提升必须落到同一张卡上。若各按自己算出的 id 建节点，
    # "第一次未锚定（id 源于名字哈希）+ 第二次已裁决（id 源于主键）"
    # 会建出两张同名的卡——正是要消掉的重复。所以先按名字预扫一遍裁决
    # 结果，让同一名字的所有证据实体用同一个主键建卡。
    resolved_by_name: dict[str, str] = {}
    for n in nodes or []:
        if not isinstance(n, dict) or n.get("kind") != "object":
            continue
        otype = str((n.get("props") or {}).get("type") or "").strip()
        if otype and otype not in _SUB_TYPE_OF:
            continue
        _pk = requested.get(str(n.get("id") or ""))
        _nm = object_subject_name(n)
        if _pk and _nm:
            resolved_by_name[_nm] = str(_pk)

    for n in nodes or []:
        if not isinstance(n, dict) or n.get("kind") != "object":
            continue
        otype = str((n.get("props") or {}).get("type") or "").strip()
        if otype and otype not in _SUB_TYPE_OF:
            continue          # 交易、通话等非主体对象不参与提升
        nid = str(n.get("id") or "")
        # 同名同主体：沿用该名字已被裁决出的主键，避免建出第二张卡
        nm = object_subject_name(n)
        eff_pk = requested.get(nid) or resolved_by_name.get(nm or "")
        try:
            res = promote_object_to_subject(
                case_id=case_id, object_node=n, conn=conn,
                requested_pk=eff_pk)
        except PromoteError as exc:
            reports.append({"object_node_id": nid, "status": "rejected",
                            "reason": str(exc)})
            continue
        sub = res["subject"]
        if sub["id"] in seen:          # 幂等：同主体不重复建，但**追加来源**
            _merge_promotion_source(seen[sub["id"]], sub, nid)
            reports.append({"object_node_id": nid, "status": "duplicate",
                            "subject_node_id": sub["id"],
                            "reason": "该主体已在研判层，已追加为来源证据"})
            continue
        seen[sub["id"]] = sub
        out_nodes.append(sub)
        out_edges.append(res["edge"])
        reports.append({"object_node_id": nid, "status": res["status"],
                        "subject_node_id": sub["id"],
                        "reason": res["reason"]})

    return {"nodes": out_nodes, "edges": out_edges, "reports": reports}
