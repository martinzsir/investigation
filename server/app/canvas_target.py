"""靶心自动带入（CAN-15）与未锚定阻断（CAN-20）。

业务起点
--------
正兵在画布上选中一个主体，点某个研判镜头——**不该再选一遍人**。过去每个
镜头各自声明 target 参数，选一次只对一个镜头生效，于是"顺着图往下查"这条
最自然的路径走不通：图根本长不起来。

两条红线
--------
R-1 重名不代选：主体 ambiguous 时**不填任何 pk**，返回阻断 + 候选。
    挑一个填进去是最危险的静默失效——跑出来的证据看着完整，其实可能
    配在另一个人身上，且正兵完全无从察觉。
R-2 未锚定不静默返回空（CAN-20）：无主键时必须给出可读原因。空结果会被
    读成"确实查无此事"，而真相是"这个人还没在数据里"，两者处理方式
    完全不同。
"""
from __future__ import annotations

from typing import Any

# 主体类参数名（镜头 params_schema 的键）
SUBJECT_PARAM_HINTS: tuple[str, ...] = (
    "target_subject", "subject", "subject_a", "subject_b",
    "person", "target_person", "target",
)
# 地点类参数名
PLACE_PARAM_HINTS: tuple[str, ...] = (
    "location_id", "target_location", "place", "target_place",
)

# 阻断状态（前端据此决定禁用按钮还是弹候选选择）
BLOCKED_UNANCHORED = "blocked_unanchored"
BLOCKED_AMBIGUOUS = "blocked_ambiguous"


def _is_string_param(spec: Any) -> bool:
    return isinstance(spec, dict) and str(
        spec.get("type") or "string").strip().lower() == "string"


def target_params(skill: dict[str, Any]) -> tuple[list[str], list[str]]:
    """挑出镜头中可被靶心带入的参数：返回 (主体参数, 地点参数)。

    只读 params_schema，不读 handler 实现——镜头换实现不影响带入口径。
    """
    schema = skill.get("params_schema") or {}
    subjects = [k for k in schema
                if str(k).lower() in {h.lower() for h in SUBJECT_PARAM_HINTS}
                and _is_string_param(schema.get(k))]
    places = [k for k in schema
              if str(k).lower() in {h.lower() for h in PLACE_PARAM_HINTS}
              and _is_string_param(schema.get(k))]
    # 稳定顺序：按声明顺序，保证多次调用结果一致
    return sorted(subjects, key=lambda k: list(schema).index(k)), \
        sorted(places, key=lambda k: list(schema).index(k))


def autofill_target(*, node: dict[str, Any] | None,
                    skill: dict[str, Any]) -> dict[str, Any]:
    """按选中节点为镜头预填靶心参数。

    返回 ``{status, params, target_pk, candidates, reason, fillable}``。
      · filled             —— 已填入主键/坐标，可直接跑
      · blocked_ambiguous  —— 同名异人待裁决（R-1）
      · blocked_unanchored —— 未锚定，无可用研判（R-2 / CAN-20）
      · no_target_param    —— 该镜头不需要靶心（如全量扫描类）
      · no_selection       —— 未选中任何节点
      · wrong_kind         —— 选中节点类型与镜头所需参数不匹配
    """
    skill_id = str(skill.get("skill_id") or skill.get("lens_id") or "")
    subj_params, place_params = target_params(skill)
    empty: dict[str, Any] = {}

    if not node:
        return {"status": "no_selection", "params": empty, "target_pk": None,
                "candidates": [], "skill_id": skill_id,
                "reason": "未选中节点，请先在画布上选中一个研判主体",
                "fillable": subj_params + place_params}

    kind = str(node.get("kind") or "")
    props = node.get("props") or {}
    name = str(props.get("name") or node.get("label") or "").strip()

    # --- 主体靶心 ---
    if subj_params and kind == "subject":
        pk = props.get("person_pk")
        ambiguous = bool(props.get("person_pk_ambiguous"))
        cands = list(props.get("pk_candidates") or [])

        if ambiguous:
            # R-1：绝不代选。把候选交给正兵，由他在选择里裁决。
            return {"status": BLOCKED_AMBIGUOUS, "params": empty,
                    "target_pk": None, "candidates": cands,
                    "skill_id": skill_id, "fillable": subj_params,
                    "reason": (f"「{name}」同名异人（{len(cands)} 个候选主键），"
                               f"须先裁决再研判；系统不代为选择")}
        if not pk:
            # R-2 / CAN-20：未锚定——写清"为什么跑不出"，不静默空转
            why = str(props.get("pk_resolution") or "").strip()
            return {"status": BLOCKED_UNANCHORED, "params": empty,
                    "target_pk": None, "candidates": cands,
                    "skill_id": skill_id, "fillable": subj_params,
                    "reason": why or (
                        f"「{name}」未锚定语义层主键，无可用研判"
                        f"（需先补录人员数据或人工确认身份）")}

        # 只填主靶心（第一个主体参数）；其余留空由正兵指定，避免
        # 把同一个 pk 填进 subject_a/subject_b 造成"自己与自己同框"
        params = {subj_params[0]: str(pk)}
        return {"status": "filled", "params": params, "target_pk": str(pk),
                "candidates": cands, "skill_id": skill_id,
                "fillable": subj_params,
                "reason": f"已带入靶心：{name}"}

    # --- 地点靶心 ---
    if place_params and kind == "place":
        loc = props.get("location_id") or props.get("pk")
        if not loc:
            return {"status": BLOCKED_UNANCHORED, "params": empty,
                    "target_pk": None, "candidates": [],
                    "skill_id": skill_id, "fillable": place_params,
                    "reason": "该地点节点无语义层标识，无法作为靶心"}
        params = {place_params[0]: str(loc)}
        return {"status": "filled", "params": params, "target_pk": str(loc),
                "candidates": [], "skill_id": skill_id,
                "fillable": place_params,
                "reason": f"已带入地点靶心：{name or loc}"}

    if not subj_params and not place_params:
        return {"status": "no_target_param", "params": empty,
                "target_pk": None, "candidates": [],
                "skill_id": skill_id, "fillable": [],
                "reason": "该镜头不需要靶心，可直接在画布上运行"}

    need = "研判主体" if subj_params else "地点"
    return {"status": "wrong_kind", "params": empty, "target_pk": None,
            "candidates": [], "skill_id": skill_id,
            "fillable": subj_params + place_params,
            "reason": f"该镜头需要选中{need}作为靶心，当前选中类型为「{kind}」"}


def fillable_skills(node: dict[str, Any] | None,
                    skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列出当前选中节点下各镜头的可跑状态（工具箱灰显/禁用用）。

    逐镜头调用 autofill_target，不做批量特判——避免"批量走另一套口径"
    这类分叉（前面踩过多次）。
    """
    out = []
    for s in skills or []:
        r = autofill_target(node=node, skill=s)
        out.append({
            "skill_id": str(s.get("skill_id") or s.get("lens_id") or ""),
            "name": str(s.get("name") or ""),
            "status": r["status"],
            "runnable": r["status"] in ("filled", "no_target_param"),
            "params": r["params"],
            "reason": r["reason"],
            "candidates": r["candidates"],
        })
    return out
