"""主体引用归一：把三个维度各自的标识符对齐到同一套代理键。

为什么需要
----------
三个维度产出的「人」用了不同的标识符：

    空间  → 姓名字符串  "张卫国"
    关系  → 代理键      "person_ecb52c3719fc"
    时间  → 两者混用（同一维度内部都不统一）

同一把钥匙开不了三把锁，于是「点地图上的点 → 关系视图高亮对应的人」
只能靠手工 join——联动是假的。本模块把归一固化成函数，让联动变成
结构性引用。

代理键规则 `person_<sha1(姓名)[:12]>` 与关系侧**完全一致**，故不需要
查表 join，一次哈希即可对齐。

红线：重名不猜
--------------
同名异人（两个张卫国）时 `pk` 置 None 并标 `ambiguous`，附候选主键与
裁决理由，**绝不静默挑一个**。系统一旦替正兵决定「这个张卫国是哪个
张卫国」，后面所有联动都建立在可能错误的地基上。

与 core/homonym.py 的分工
--------------------------
两者都处理同名，但**介入时机不同**，互不替代：

    homonym.py   研判前置，读语义层；额外含空间互斥证据
                 （同一时刻出现在物理不可达的两地）
    本模块       导入期/产出期，读原始表与身份表；证据为证号/手机互斥

前者回答「这个名字背后是几个人」，后者回答「该用哪个 ID 引用他」。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_RELATIONAL_SUFFIX = re.compile(r"(配偶|家属|妻|夫|子|女|父|母|兄|弟|姐|妹)$")


def person_pk(name: str) -> str:
    """姓名 → 代理键。规则与关系侧一致，无需 join。"""
    return "person_" + hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:12]


def as_name(v: Any) -> str:
    """主体字段可能是字符串，也可能是 {'name':..} 结构——统一取名字。

    为什么需要：时间轴观察的 subject 是 `{pk, type, name}` 字典，
    直接 `str()` 会把整个字典当名字，导致键对不上（这条踩过）。
    """
    if isinstance(v, dict):
        for k in ("name", "person", "subject", "主体"):
            if v.get(k):
                return str(v[k])
        return ""
    return str(v or "")


def strip_relational(name: str) -> tuple[str, bool]:
    """剥离关系型指代（张卫国配偶）。

    它既非同一人亦非同名异人：被字面相似判为别名会**错误合并**；
    留在人审队列里又会**稀释**真正的同名歧义裁决。
    """
    try:
        from core.homonym import strip_relational as _sr
        return _sr(name)
    except Exception:
        s = str(name or "").strip()
        if _RELATIONAL_SUFFIX.search(s):
            return _RELATIONAL_SUFFIX.sub("", s), True
        return s, False


def pk_candidates(conn: Any, name: str) -> list[str]:
    """取该姓名在语义层中的真实主键列表（obj_person 优先）。

    查不到返回空列表——由调用方决定退回派生键，绝不在这里编主键。
    """
    if conn is None or not name:
        return []
    for tbl, col in (("obj_person", "person_id"),
                     ("obj_person_identity", "identity_id")):
        try:
            rows = conn.execute(
                f"SELECT {col} FROM {tbl} WHERE raw_name = ?", [name]).fetchall()
        except Exception:
            continue
        pks = [str(r[0]) for r in rows if r and r[0]]
        if pks:
            return sorted(set(pks))
    return []


def resolve_person(names: list[str], *, conn: Any = None) -> dict[str, dict]:
    """把一批姓名解析成 {name: {pk, ambiguous, candidates, reason, ...}}。

    无 conn 时不做歧义裁决（一律按名归一），但**结构不变**——调用方
    无需分支处理，避免「传不传 conn 走两套逻辑」这类分叉。
    """
    out: dict[str, dict] = {}
    verdicts: dict[str, dict] = {}
    if conn is not None:
        try:
            from core import homonym
            res = homonym.detect_homonyms(conn, names=names)
            # 键名是 groups（不是 names）——读错键会让所有裁决落空、
            # 红线静默失效，这种失效最危险：代码在跑、有输出、看不出问题。
            for item in res.get("groups") or []:
                nm = item.get("name")
                if nm:
                    verdicts[nm] = item
        except Exception:
            verdicts = {}

    for nm in names:
        if not nm:
            continue
        base, is_relational = strip_relational(nm)
        v = verdicts.get(base) or {}
        verdict = v.get("verdict")
        cands = pk_candidates(conn, base) if conn is not None else []
        if verdict in ("distinct", "pending") and not cands:
            cands = [person_pk(base) + "_1", person_pk(base) + "_2"]
        # 裁决为 distinct 但语义层仍只有 1 个主键 = 分列尚未落
        # entity_mapping（须人工 accept）。如实说明，不假装已分列。
        pending_split = (verdict in ("distinct", "pending") and len(cands) < 2)
        if verdict in ("distinct", "pending"):
            out[nm] = {
                "pk": None, "name": nm, "ambiguous": True,
                "candidates": cands, "verdict": verdict,
                "pending_split": pending_split,
                "reason": v.get("reason") or (
                    "同名异人：存在互斥强证据" if verdict == "distinct"
                    else "同名待裁决"),
                "relational_ref": is_relational,
            }
        else:
            out[nm] = {
                "pk": (cands[0] if len(cands) == 1 else person_pk(base)),
                "name": nm, "ambiguous": False,
                "candidates": cands, "verdict": verdict or None,
                "pending_split": False,
                "reason": v.get("reason"),
                "relational_ref": is_relational,
            }
    return out


def attach_person_ref(target: dict, name: Any, *, persons: dict) -> dict:
    """往任意产出条目上挂三件套（纯新增字段，向后兼容）。

        person_pk              代理键；歧义时 None
        person_name            原名，保留用于展示
        person_pk_ambiguous    是否重名待裁决

    为什么不只写 pk：原名要展示，且 pk 为空时前端得知道是「待裁决」
    而不是「查无此人」。
    """
    nm = as_name(name)
    info = persons.get(nm) or {}
    target["person_pk"] = info.get("pk")
    target["person_name"] = nm or None
    target["person_pk_ambiguous"] = bool(info.get("ambiguous"))
    if info.get("ambiguous"):
        target["person_pk_candidates"] = list(info.get("candidates") or [])
        target["person_pk_reason"] = info.get("reason")
    return target


def person_ref_dict(name: Any, *, persons: dict) -> dict:
    """单个主体的引用字典——用于列表字段（如 co_present_refs）。"""
    nm = as_name(name)
    info = persons.get(nm) or {}
    d = {"person_name": nm or None,
         "person_pk": info.get("pk"),
         "person_pk_ambiguous": bool(info.get("ambiguous"))}
    if info.get("ambiguous"):
        d["person_pk_candidates"] = list(info.get("candidates") or [])
        d["person_pk_reason"] = info.get("reason")
    return d
