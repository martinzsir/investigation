"""三维交汇层：把关系、时间、空间三类证据归到同一个「人-时-地」锚点下。

为什么要有这一层
----------------
三个维度各自的镜头都能算出东西，但**分开看每一维都是碎片**：

    关系说「张卫国与李志强有 20 条通话边」——不知道在哪见面
    空间说「两人在莫干山路同框 3 次」  ——不知道是不是利益往来
    时间说「张卫国有 31 个事件聚集簇」  ——不知道和谁

三个都指向同一个 (人, 时, 地) 时，才构成侦查里那句完整的话：
**谁、在何时、何地、前后发生了什么**。本模块就是做这个归集。

两条红线（不可越过）
--------------------
1. **不定性。** 三个维度都命中，也只能说「这三点上都有事实」——
   交汇强度是**优先级**，不是**结论**。偏离日常可能是出差、公务、
   采样缺失。产出中不出现「可疑」「疑似」「异常接触」等定性措辞，
   由 `test_convergence.py` 固化。

2. **不静默合并。** 某维度在锚点上没命中，必须如实写「未命中」并给出
   原因（无数据 / 有数据但不含该锚点），绝不能用另外两维的热度盖过去。
   正兵需要知道哪一维是空的——空本身是信息（补数据入口）。

为什么必须按时间精度加权
------------------------
「同框 15 次」与「同框 3 次」不能按次数直接比：

    15 次 = 日期级同框（只知前后一天先后出现在同一路段）= 同地异时
    3 次  = 时刻级同框（能判时间窗真重叠）            = 同期同地

后者的证据分量**远重于**前者。若不加权，系统会把大量低精度配对
堆出的次数排到最前，等于替正兵排了个反序——比不排更糟。
故 `PRECISION_WEIGHT` 是这一层的核心，不是装饰。
"""
from __future__ import annotations

import math
import re
from typing import Any

# ---- 维度权重：三维平权，谁也不天生优先 ----
DIM_WEIGHT: dict[str, float] = {
    "space": 1.0,
    "time": 1.0,
    "relation": 1.0,
}

# ---- 精度权重：本层的核心 ----
# date 档压到 0.2——它只能证明「前后一天先后出现」，是同地异时；
# minute/second 档给满——能证明时间窗真重叠。
# 与 core/geo.py 的四级空间判据同构：判据精度决定证据分量，不按次数。
PRECISION_WEIGHT: dict[str, float] = {
    "second": 1.0,
    "minute": 1.0,
    "hour": 0.5,
    "date": 0.2,
    "unknown": 0.1,
}

# 重复出现的加成：有上限，避免「次数堆砌」压过精度。
REPEAT_STEP = 0.2
REPEAT_CAP = 3.0

_RELATIONAL_SUFFIX = re.compile(r"(配偶|家属|妻|夫|子|女|父|母|兄|弟|姐|妹)$")


# ------------------------------------------------------------------ #
# 主体归一
# ------------------------------------------------------------------ #
# 归一实现收敛到 core/entity_ref.py（与 homonym.py 分工见该模块注释）。
# 这里只做转发，避免两处各写一套代理键规则——那正是之前维度口径分叉
# 的根源。
from core.entity_ref import (  # noqa: E402
    as_name as _as_name,
    person_pk as _person_pk,
    pk_candidates as _pk_candidates,
    resolve_person,
    strip_relational as _strip_relational,
)


# ------------------------------------------------------------------ #
# 锚点提取
# ------------------------------------------------------------------ #
def _as_name(v: Any) -> str:
    """主体字段可能是字符串，也可能是 {'name':..} 之类结构——统一取名字。"""
    if isinstance(v, dict):
        for k in ("name", "person", "subject", "主体"):
            if v.get(k):
                return str(v[k])
        return ""
    return str(v or "")


def _dt_precision(value: Any) -> str:
    try:
        from core.time_semantics import derive_time_precision
        return derive_time_precision(value) or "unknown"
    except Exception:
        return "unknown"


def _weaker(a: str, b: str) -> str:
    try:
        from core.time_semantics import weaker
        return weaker(a, b) or "unknown"
    except Exception:
        return a if a == b else "date"


def _space_anchors(observations: list[dict]) -> list[dict]:
    """空间维锚点：异常停留 / 轨迹分段停留 / 时空伴随配对。

    每个锚点带 (人, 地, 日) 以及**该锚点自身的时间精度**——精度来自
    start/end 是否含真实时刻，不来自字段名。
    """
    out: list[dict] = []
    for o in observations:
        skill = str(o.get("skill_id") or "")
        det = o.get("detail") or {}
        subj = det.get("subject") or o.get("subject")
        if skill == "geo_anomaly":
            for an in det.get("anomalies") or []:
                prec = _weaker(_dt_precision(an.get("start")),
                               _dt_precision(an.get("end")))
                out.append({
                    "person": subj,
                    "location_id": an.get("location_id"),
                    "std_address": an.get("std_address"),
                    "date": (an.get("date") or str(an.get("start") or "")[:10]),
                    "start": an.get("start"), "end": an.get("end"),
                    "precision": prec,
                    "kind": f"anomaly:{an.get('kind')}",
                    "co_present": list(an.get("co_present") or []),
                    "event_pks": list(an.get("event_pks") or []),
                    "obs_id": o.get("observation_id"),
                    "duration_minutes": an.get("duration_minutes"),
                })
        elif skill == "geo_segment":
            for st in det.get("stays") or []:
                prec = _weaker(_dt_precision(st.get("start")),
                               _dt_precision(st.get("end")))
                out.append({
                    "person": subj,
                    "location_id": st.get("location_id"),
                    "std_address": st.get("std_address"),
                    "date": (st.get("date") or str(st.get("start") or "")[:10]),
                    "start": st.get("start"), "end": st.get("end"),
                    "precision": prec,
                    "kind": "segment:stay",
                    "co_present": [],
                    "event_pks": [],
                    "obs_id": o.get("observation_id"),
                    "duration_minutes": st.get("duration_minutes"),
                })
        elif skill == "geo_accompany":
            comp = det.get("companion") or {}
            pa, pb = comp.get("person_a"), comp.get("person_b")
            gran = det.get("time_granularity") or "unknown"
            for an_date in _companion_dates(comp):
                out.append({
                    "person": pa,
                    "location_id": (comp.get("locations") or [None])[0],
                    "std_address": (comp.get("locations") or [None])[0],
                    "date": an_date,
                    "start": None, "end": None,
                    "precision": gran,
                    "kind": "accompany",
                    "co_present": [x for x in (pb,) if x],
                    "event_pks": list(comp.get("event_pks") or []),
                    "obs_id": o.get("observation_id"),
                    "duration_minutes": None,
                })
                out.append({
                    "person": pb,
                    "location_id": (comp.get("locations") or [None])[0],
                    "std_address": (comp.get("locations") or [None])[0],
                    "date": an_date,
                    "start": None, "end": None,
                    "precision": gran,
                    "kind": "accompany",
                    "co_present": [x for x in (pa,) if x],
                    "event_pks": list(comp.get("event_pks") or []),
                    "obs_id": o.get("observation_id"),
                    "duration_minutes": None,
                })
    return [a for a in out if a.get("person") and a.get("date")]


def _companion_dates(comp: dict) -> list[str]:
    """从伴随聚合里取涉及的日期集合。

    聚合只留 first/last，中间日期不可复原——**不插值**，
    只在两端各记一次；若相同则只记一次。
    """
    ds = []
    for k in ("first_date", "last_date"):
        v = comp.get(k)
        if v and v not in ds:
            ds.append(str(v)[:10])
    return ds


def _time_anchors(observations: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """时间维锚点：(人, 日) → 事件列表。

    时间轴事件的 date 是日期级，故精度恒为 date——这是**事实**，
    不是缺陷；它决定了时间维在这一层的权重天然低于时刻级空间证据。
    """
    idx: dict[tuple[str, str], list[dict]] = {}
    for o in observations:
        skill = str(o.get("skill_id") or "")
        if not skill.startswith("timeline_"):
            continue
        det = o.get("detail") or {}
        subj = _as_name(det.get("subject") or det.get("主体") or o.get("subject"))
        events = det.get("events") or det.get("timeline") or []
        for ev in events:
            d = str(ev.get("date") or "")[:10]
            if not subj or not d:
                continue
            idx.setdefault((subj, d), []).append({
                "type": ev.get("type"),
                "event_pk": ev.get("event_pk"),
                "brief": ev.get("brief"),
                "skill": skill,
                "obs_id": o.get("observation_id"),
            })
    return idx


def _relation_index(observations: list[dict]) -> tuple[dict, dict]:
    """关系维索引。

    返回 (边表, 事件→日期表)。
    - 边表：{frozenset({pk_a, pk_b}): [edge...]}，按代理键建索引，
      与空间侧经 `_person_pk` 归一后同一套键，可直接比对。
    - 事件→日期：由时间轴观察回填，使通话/转账这类带主键的边
      也能落到具体日期上。
    """
    edges: dict[frozenset, list[dict]] = {}
    ev_date: dict[str, str] = {}
    for o in observations:
        skill = str(o.get("skill_id") or "")
        det = o.get("detail") or {}
        if skill.startswith("timeline_"):
            for ev in det.get("events") or det.get("timeline") or []:
                pk = ev.get("event_pk")
                d = str(ev.get("date") or "")[:10]
                if pk and d:
                    ev_date.setdefault(str(pk), d)
        if not skill.startswith("relation_"):
            continue
        for e in det.get("edges") or []:
            a, b = e.get("src"), e.get("dst")
            if not a or not b or a == b:
                continue
            edges.setdefault(frozenset((str(a), str(b))), []).append({
                "edge": e.get("edge"),
                "edge_pk": e.get("edge_pk"),
                "kind": e.get("kind"),
                "obs_id": o.get("observation_id"),
            })
    return edges, ev_date


# ------------------------------------------------------------------ #
# 打分
# ------------------------------------------------------------------ #
def _repeat_factor(count: int) -> float:
    if count <= 1:
        return 1.0
    return min(1.0 + REPEAT_STEP * (count - 1), REPEAT_CAP)


def _dim_score(dim: str, precision: str, count: int) -> float:
    return round(DIM_WEIGHT.get(dim, 1.0)
                 * PRECISION_WEIGHT.get(precision or "unknown", 0.1)
                 * _repeat_factor(count), 4)


def build_convergence(observations: list[dict], *,
                      conn: Any = None,
                      min_dims: int = 2,
                      top_n: int = 50) -> dict:
    """构建三维交汇。

    参数
    ----
    observations : 观察档案列表（output/observations.json 的 observations）
    conn         : 可选数据库连接，用于同名异人裁决（不传则不做歧义标记）
    min_dims     : 至少命中几维才进结果（默认 2——单维不是交汇）
    top_n        : 返回条数上限

    返回
    ----
    dict，含 convergences / diagnostics。每条 convergence 明确列出
    三维各自的 hit 与未命中原因，绝不用他维热度填补空缺。
    """
    spaces = _space_anchors(observations)
    time_idx = _time_anchors(observations)
    edges, ev_date = _relation_index(observations)

    names: list[str] = []
    for a in spaces:
        names.append(_as_name(a["person"]))
        names.extend(_as_name(x) for x in (a.get("co_present") or []))
    for (p, _d) in time_idx:
        names.append(_as_name(p))
    persons = resolve_person(sorted({n for n in names if n}), conn=conn)

    # (person_key, date) → 聚合。person_key 歧义时用 "?name" 前缀标记，
    # 不静默并入任一候选。
    buckets: dict[tuple[str, str], dict] = {}

    def _key(name: Any) -> str:
        name = _as_name(name)
        if not name:
            return ""
        info = persons.get(name) or {}
        if info.get("ambiguous"):
            return "?" + str(name)
        return str(info.get("pk") or _person_pk(name))

    def _bucket(pk: str, date: str) -> dict:
        k = (pk, date)
        if k not in buckets:
            buckets[k] = {
                "person_key": pk, "date": date,
                "location_ids": [], "std_addresses": [],
                "space": [], "time": [], "relation": [],
                "co_present": [], "persons": [],
            }
        return buckets[k]

    for a in spaces:
        b = _bucket(_key(a["person"]), a["date"])
        b["space"].append(a)
        if _as_name(a["person"]) not in b["persons"]:
            b["persons"].append(_as_name(a["person"]))
        lid = a.get("location_id")
        if lid and lid not in b["location_ids"]:
            b["location_ids"].append(lid)
        sad = a.get("std_address")
        if sad and sad not in b["std_addresses"]:
            b["std_addresses"].append(sad)
        for cp in (_as_name(x) for x in (a.get("co_present") or [])):
            if cp and cp not in b["co_present"]:
                b["co_present"].append(cp)
    for (p, d), evs in time_idx.items():
        b = _bucket(_key(p), d)
        b["time"].extend(evs)
        pn = _as_name(p)
        if pn and pn not in b["persons"]:
            b["persons"].append(pn)

    # ---- 关系维命中 ----
    # 两条通路，都要记**怎么命中的**，不能只记命中：
    #   A. 边两端主体在该锚点同现（co_present）→ 精度继承空间锚点
    #   B. 边的主键能回填到该日期（通话/转账）→ 精度为日期级
    for b in buckets.values():
        # 歧义主体：主键键（?名）与**全部候选主键**都参与比对。
        # 为什么不干脆不比——那样会因「不猜」而丢掉真实关联；
        # 为什么不能只比候选——那等于静默挑了一个。故两条都走，
        # 命中来源写进 via，正兵看得到这条关联是「确定」还是「候选」。
        direct: set[str] = set()
        cand: set[str] = set()
        for p in b["persons"]:
            k = _key(p)
            (cand if k.startswith("?") else direct).add(k)
            if k.startswith("?"):
                cand |= set(_candidates([p], persons))
        for c in b["co_present"]:
            k = _key(c)
            (cand if k.startswith("?") else direct).add(k)
            if k.startswith("?"):
                cand |= set(_candidates([c], persons))
        pks_here = direct | cand
        hits: list[dict] = []
        for pair, es in edges.items():
            pair_set = set(pair)
            if not (pair_set & pks_here):
                continue
            for e in es:
                via = None
                prec = None
                matched_candidates: list[str] = []
                n_direct = len(pair_set & direct)
                n_cand = len(pair_set & (cand - direct))
                if n_direct >= 2:
                    via = "co_present"
                    prec = _best_precision(b["space"])
                elif n_direct >= 1 and (n_direct + n_cand) >= 2:
                    via = "co_present_candidate"
                    prec = _best_precision(b["space"])
                    matched_candidates = sorted(pair_set & (cand - direct))
                elif n_cand >= 2:
                    via = "co_present_candidate"
                    prec = _best_precision(b["space"])
                    matched_candidates = sorted(pair_set & cand)
                else:
                    d = ev_date.get(str(e.get("edge_pk")))
                    if d and d == b["date"]:
                        via = "dated_edge"
                        prec = "date"
                if via is None:
                    continue
                hits.append({
                    "edge": e.get("edge"), "edge_pk": e.get("edge_pk"),
                    "kind": e.get("kind"), "via": via,
                    "precision": prec or "unknown",
                    "matched_candidates": matched_candidates,
                    "obs_id": e.get("obs_id"),
                })
        b["relation"] = hits

    # ---- 组装 ----
    convs: list[dict] = []
    for b in buckets.values():
        dims: dict[str, dict] = {}
        # 空间
        if b["space"]:
            prec = _best_precision(b["space"])
            dims["space"] = {
                "hit": True, "count": len(b["space"]), "precision": prec,
                "precision_dist": _precision_dist(b["space"]),
                "weight": _dim_score("space", prec, len(b["space"])),
                "reason": None,
                "kinds": sorted({a["kind"] for a in b["space"]}),
                "duration_minutes": _total_duration(b["space"]),
            }
        else:
            dims["space"] = {"hit": False, "count": 0, "precision": None,
                             "weight": 0.0, "reason": "该锚点无空间证据",
                             "kinds": [], "duration_minutes": None}
        # 时间
        if b["time"]:
            dims["time"] = {
                "hit": True, "count": len(b["time"]), "precision": "date",
                "weight": _dim_score("time", "date", len(b["time"])),
                "reason": None,
                "kinds": sorted({t.get("skill") for t in b["time"]}),
                "types": sorted({str(t.get("type")) for t in b["time"]}),
            }
        else:
            dims["time"] = {"hit": False, "count": 0, "precision": None,
                            "weight": 0.0, "reason": "该锚点无时间轴证据",
                            "kinds": [], "types": []}
        # 关系
        if b["relation"]:
            prec = _best_precision_rel(b["relation"])
            dims["relation"] = {
                "hit": True, "count": len(b["relation"]), "precision": prec,
                "weight": _dim_score("relation", prec, len(b["relation"])),
                "reason": None,
                "kinds": sorted({h.get("via") for h in b["relation"]}),
                "edge_kinds": sorted({str(h.get("kind")) for h in b["relation"]}),
            }
        else:
            dims["relation"] = {"hit": False, "count": 0, "precision": None,
                                "weight": 0.0, "reason": "该锚点无关系边证据",
                                "kinds": [], "edge_kinds": []}

        hit_dims = [d for d in ("space", "time", "relation") if dims[d]["hit"]]
        score = round(sum(dims[d]["weight"] for d in hit_dims), 4)
        ambiguous = b["person_key"].startswith("?")
        convs.append({
            "key": f"{b['person_key']}|{b['date']}",
            "person_key": b["person_key"],
            "person_names": b["persons"],
            "person_ambiguous": ambiguous,
            "ambiguous_candidates": _candidates(b["persons"], persons),
            # 裁决结论与"是否已落库"必须分开报：
            # 消歧判 distinct 但语义层仍只有 1 个主键时（分列须人工 accept
            # 后落 entity_mapping），候选会只剩一个——若只给候选数，正兵
            # 会读成"没有重名可裁"，恰好掩盖了真实冲突。故把 verdict、
            # pending_split、reason 一并透出。
            "person_verdict": _ambig_field(b["persons"], persons, "verdict"),
            "person_split_pending": bool(
                _ambig_field(b["persons"], persons, "pending_split")),
            "person_ambiguity_reason": _ambig_field(
                b["persons"], persons, "reason"),
            "date": b["date"],
            "location_ids": b["location_ids"],
            "std_addresses": b["std_addresses"],
            "co_present": b["co_present"],
            "dimensions": dims,
            "dim_hit_count": len(hit_dims),
            "hit_dimensions": hit_dims,
            "score": score,
            "max_score": round(sum(DIM_WEIGHT.values()) * REPEAT_CAP, 4),
            "claims": _claims(b, dims, ambiguous,
                              _candidates(b["persons"], persons),
                              pending_split=bool(
                                  _ambig_field(b["persons"], persons,
                                               "pending_split")),
                              reason=str(_ambig_field(
                                  b["persons"], persons, "reason") or "")),
            "falsification": _falsification(dims),
        })

    convs.sort(key=lambda c: (-c["dim_hit_count"], -c["score"],
                              c["person_key"], c["date"]))
    kept = [c for c in convs if c["dim_hit_count"] >= min_dims][:top_n]
    full = [c for c in convs if c["dim_hit_count"] >= min_dims]

    return {
        "convergences": kept,
        "total_hit": len(full),
        "total_anchors": len(buckets),
        "returned": len(kept),
        "min_dims": min_dims,
        # 口径自陈：让正兵知道分数是怎么来的，权重不是黑箱
        "weight_model": {
            "dim_weight": DIM_WEIGHT,
            "precision_weight": PRECISION_WEIGHT,
            "repeat_step": REPEAT_STEP,
            "repeat_cap": REPEAT_CAP,
            "formula": "score = Σ_dims (dim_weight × precision_weight × repeat_factor)",
        },
        "note": ("交汇强度表示**核查优先级**，不代表风险高低；"
                 "三维均命中亦不构成定性结论。"),
        "diagnostics": {
            "space_anchors": len(spaces),
            "time_pairs": len(time_idx),
            "relation_edges": sum(len(v) for v in edges.values()),
            "dated_events": len(ev_date),
            "ambiguous_persons": sorted({p for p, i in persons.items()
                                         if i.get("ambiguous")}),
            "dropped_single_dim": len(convs) - len(full),
        },
    }


def _best_precision(anchors: list[dict]) -> str:
    """取**最强**的一档——与 geo.py `spatial_level=best` 同口径：
    最准的那条证据决定这组判据的可信上限。

    为什么不取最粗：本层是「这个锚点能支撑多强的说法」，
    有一条时刻级证据就该按时刻级算。但**分布照实报**
    （precision_dist），弱证据不被隐藏，正兵看得到全貌。
    """
    order = ["unknown", "date", "hour", "minute", "second"]
    cur = "unknown"
    for a in anchors:
        p = a.get("precision") or "unknown"
        if order.index(p) > order.index(cur):
            cur = p
    return cur


def _precision_dist(anchors: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for a in anchors:
        p = a.get("precision") or "unknown"
        dist[p] = dist.get(p, 0) + 1
    return dist


def _best_precision_rel(hits: list[dict]) -> str:
    cur = None
    for h in hits:
        p = h.get("precision") or "unknown"
        cur = p if cur is None else _weaker(cur, p)
    return cur or "unknown"


def _total_duration(anchors: list[dict]) -> float | None:
    vals = [a.get("duration_minutes") for a in anchors
            if isinstance(a.get("duration_minutes"), (int, float))]
    return round(sum(vals), 1) if vals else None


def _candidates(names: list[str], persons: dict) -> list[str]:
    out: list[str] = []
    for n in names:
        for c in (persons.get(n) or {}).get("candidates") or []:
            if c not in out:
                out.append(c)
    return out


def _ambig_field(names: list[str], persons: dict, field: str) -> Any:
    """取该锚点上首个"歧义主体"的裁决字段（verdict / pending_split / reason）。"""
    for n in names:
        info = persons.get(n) or {}
        if info.get("ambiguous"):
            return info.get(field)
    return None


def _claims(b: dict, dims: dict, ambiguous: bool,
            candidates: list[str] | None = None,
            pending_split: bool = False,
            reason: str = "") -> list[str]:
    """事实陈述，不含定性词。"""
    out = []
    who = "、".join(b["persons"]) if b["persons"] else "（未指名）"
    if ambiguous:
        cands = candidates or []
        if pending_split:
            # 已判定同名异人、但分列尚未落库——不能只报候选数，
            # 否则"只剩一个候选"会被读成"没有重名可裁"。
            why = f"（{reason}）" if reason else ""
            who += (f"（已判定同名异人{why}，但实体分列尚未落库："
                    f"语义层仍只有 1 个主键，须人工确认分列后重建）")
        else:
            tail = (("；候选主键 " + "、".join(cands)) if cands
                    else "；分列待人工确认")
            who += f"（重名待裁决{tail}）"
    loc = "、".join(str(x) for x in (b.get("std_addresses") or b.get("locations") or [])) or "（无地点锚）"
    out.append(f"{who} 于 {b['date']} 在 {loc} 存在三维锚点")
    if dims["space"]["hit"]:
        d = dims["space"]
        extra = f"，累计停留 {d['duration_minutes']} 分钟" if d["duration_minutes"] else ""
        out.append(f"空间维：{d['count']} 条（{d['precision']} 档，{', '.join(d['kinds'])}{extra}）")
    if dims["time"]["hit"]:
        d = dims["time"]
        out.append(f"时间维：{d['count']} 条事件（{', '.join(t for t in d['types'] if t)}）")
    if dims["relation"]["hit"]:
        d = dims["relation"]
        out.append(f"关系维：{d['count']} 条边（{', '.join(d['kinds'])}）")
    if b["co_present"]:
        out.append(f"同现主体：{'、'.join(b['co_present'])}（是否构成接触待核查）")
    miss = [d for d in ("space", "time", "relation") if not dims[d]["hit"]]
    if miss:
        out.append("未命中维度：" + "；".join(
            f"{m}（{dims[m]['reason']}）" for m in miss))
    return out


def _falsification(dims: dict) -> str:
    parts = []
    if dims["space"]["hit"] and dims["space"]["precision"] in ("date", "hour"):
        parts.append("空间证据仅到日期/小时档，只能证明同地异时，"
                     "若能证实两者不在同一时间窗则不构成同框")
    if dims["space"]["hit"]:
        parts.append("同框地点若为项目现场、会议场所等职务性地点，"
                     "或两主体本就属同一单位/同一项目组，则时空接近系工作常态")
    if dims["relation"]["hit"]:
        parts.append("关系边存在不等于该次接触成立；若边为职务性往来则不构成私下接触")
    parts.append("坐标为区划质心时距离不代表实际间距")
    parts.append("稀疏采样下的停留时长为当日首尾间隔，非连续驻留")
    return "；".join(parts)
