"""研判主体名录（CAN-13）：让正兵**挑**一个已收录主体建到画布上。

为什么必须走名录而不是自由输入
------------------------------
自由输入一个名字，系统只能按名派生键；派生键与语义层主键对不上，
后面所有研判（落脚点、通话、异常轨迹）都查不到东西。界面显示"建好了"，
一跑全是空——正兵只会以为工具坏了。名录里的每一条都来自语义层，
建出来即锚定。

三条红线
--------
R-1 与画布节点**同口径**：名录判定直接复用 ``build_subject_node``，
    不另写一套。否则选择器里看着是"已锚定唯一主键"，建到画布上却变成
    "同名异人待裁决"，正兵会以为是建的时候出了错。
R-2 不可用要明说：语义层不可达时返回 ``semantic_ready=false`` + 原因，
    **不返回空数组**——空数组会被读成"本案没有主体"。
R-3 不露敏感明文：身份证号/手机号属敏感数据元，候选区分信息只给**性质**
    （"2 个身份证号互斥"），不给号码本身。
"""
from __future__ import annotations

from typing import Any

# 名录来源表：子类型 → (表, 名字列, 键列)
_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("person", "obj_person", "raw_name", "person_id"),
    ("organization", "obj_org", "raw_name", "org_id"),
)

_DEFAULT_TYPES = ("person", "organization")


def _rows(conn: Any, table: str, name_col: str,
          key_col: str) -> list[tuple[str, str]]:
    try:
        rs = conn.execute(
            f"SELECT {name_col}, {key_col} FROM {table}").fetchall()
    except Exception:
        return []                      # 表不存在 → 该子类型无候选，不炸
    out: list[tuple[str, str]] = []
    for r in rs or []:
        nm = str(r[0] or "").strip() if r and r[0] else ""
        if nm:
            out.append((nm, str(r[1] or "").strip()))
    return out


def list_subjects(*, conn: Any, q: str = "", limit: int = 50,
                  sub_types: tuple[str, ...] | list[str] = _DEFAULT_TYPES,
                  case_id: str = "") -> dict[str, Any]:
    """案件主体名录：按名字聚合，逐条给出指代状态。

    同名多行在库里聚成一个条目（正兵看到的是"一个人"），但指代状态
    照实说——张卫国在库里就是两个身份证号。
    """
    if conn is None:
        return {
            "semantic_ready": False,
            "reason": "未连接语义层，无法取主体名录；此时建出的主体均无可用研判",
            "subjects": [],
        }

    kw = str(q or "").strip()
    seen: dict[str, dict[str, Any]] = {}
    for st, tbl, ncol, kcol in _SOURCES:
        if st not in tuple(sub_types or _DEFAULT_TYPES):
            continue
        rows = _rows(conn, tbl, ncol, kcol)
        if not rows and st == "person":
            continue
        for nm, _key in rows:
            if kw and kw not in nm:
                continue
            if nm in seen:
                continue
            # 直接复用画布节点构造，保证名录与画布同口径（R-1）
            from server.app.canvas_case import build_subject_node
            node = build_subject_node(case_id=case_id or "", name=nm,
                                      sub_type=st, conn=conn)
            p = node.get("props") or {}
            # 关系型指代（"张卫国配偶"）既非独立主体亦非同名异人：
            # 当组织建去研判会查出一堆不属于"这个人"的东西。不静默过滤——
            # 标出来并写明原因，否则正兵会疑惑"这家单位怎么从名录里没了"。
            try:
                from core.entity_ref import strip_relational
                _base, is_rel = strip_relational(nm)
            except Exception:
                is_rel = False
            seen[nm] = {
                "name": nm,
                "sub_type": st,
                "relational": bool(is_rel),
                "person_pk": p.get("person_pk"),
                "person_pk_ambiguous": bool(p.get("person_pk_ambiguous")),
                "pk_status": p.get("pk_status"),
                "pk_candidates": list(p.get("pk_candidates") or []),
                "identity_rows": int(p.get("identity_rows") or 0),
                "identity_evidence": str(p.get("identity_evidence") or ""),
                "pk_resolution": (f"「{nm}」是关系型指代（非独立主体），"
                                  f"不作为研判主体"
                                  if is_rel else str(p.get("pk_resolution") or "")),
                "usable": bool(p.get("anchored")) and not is_rel,
            }

    subs = sorted(seen.values(),
                  key=lambda d: (not bool(d["usable"]), d["name"]))
    if limit and limit > 0:
        subs = subs[:int(limit)]
    return {"semantic_ready": True, "reason": "", "subjects": subs}
