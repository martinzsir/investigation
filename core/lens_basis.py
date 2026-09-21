"""镜头判据（basis）生成 —— 让镜头产出「说人话」，而不是摊一堆数据。

为什么需要
----------
镜头产出的是结构化材料（nodes/edges/timeline/bursts/rows），全是 fact 层。
用户面对 148 个事件、32 个聚集簇，看不出「这说明什么」。

判据（basis）回答的就是这句：**这批材料合起来意味着什么**。
它跟 evidence 三栏里的 inference 栏是同一个位置——规则用 rule["basis"]
（如「张卫国→李志强 通话 12 次，为常态中位数 3 的 4.0 倍」），
镜头此前没有对应的东西。

写法遵循规则判据的四段结构：
    主体是谁 · 观测值多少 · 跟什么比 · 比对结果

三条硬约束
----------
1. **只陈述观测，不做定性**——不写「所以他们在搞利益输送」。
   定性红线归正兵（LineageClue.定性_policy）。
2. **降级必须说出来**——数据不足或结果被截断时，判据要明说口径变了，
   不能装作完整（与 rules 的 diagnostics 同口径）。
3. **不编造未计算的量**——只使用函数实际返回的字段。函数没算的基线
   （如「相对公示日的密度」）不写，宁可少说，也不编。

返回
----
{"basis": str, "falsification": str, "claims": list[str]}
claims 是判据里每个可核对的断言（供前端逐条挂证据引用）。
"""

from __future__ import annotations

from typing import Any


def _name(sub: Any) -> str:
    if isinstance(sub, dict):
        return str(sub.get("name") or sub.get("pk") or "")
    return str(sub or "")


def _types(tc: dict | None) -> str:
    if not tc:
        return "0 类"
    items = sorted(tc.items(), key=lambda kv: -kv[1])
    return "、".join(f"{k} {v}" for k, v in items)


def _degraded_note(out: dict) -> str:
    """降级/截断说明。有就说，没有就不加——不编、不省略。"""
    if not out.get("degraded"):
        return ""
    reason = str(out.get("degraded_reason") or "").strip()
    return f"；判定受限：{reason}" if reason else "；判定受限（函数返回 degraded）"


# ----------------------------------------------------------------------
# 各镜头判据
# ----------------------------------------------------------------------

def _basis_sequence(out: dict) -> tuple[str, list[str]]:
    """timeline_sequence：事件序列的**规模与构成**，不声称密度。

    注意：相对项目公示日的密度需要 project 锚点，本函数没有该输入，
    因此**不写**「公示前 N 天集中 M 起」这类断言——那是编不出来的。
    """
    tl = out.get("timeline") or []
    if not tl:
        return "未检出该主体的跨类型事件序列。", []
    sub = _name(out.get("subject"))
    first, last = tl[0].get("date"), tl[-1].get("date")
    span = out.get("span_days")
    tc = out.get("type_counts") or {}
    text = (f"{sub} 在 {first} 至 {last} 期间共 {len(tl)} 起事件，"
            f"覆盖 {len(tc)} 类（{_types(tc)}），跨度 {span} 天")
    text += _degraded_note(out)
    claims = [
        f"事件总数 {len(tl)}",
        f"类型构成 {_types(tc)}",
        f"时间跨度 {span} 天（{first} ~ {last}）",
    ]
    return text, claims


def _basis_rhythm(out: dict) -> tuple[str, list[str]]:
    """timeline_rhythm：聚集簇与间隔节奏。最大簇是实测可算的。"""
    bursts = out.get("bursts") or []
    if not bursts:
        return "未检出事件聚集簇。", []
    sub = _name(out.get("subject"))
    biggest = max(bursts, key=lambda b: b.get("event_count") or 0)
    median_gap = out.get("median_gap_days")
    burst_days = out.get("burst_days")
    text = (f"{sub} 的 {out.get('event_count')} 起事件聚成 "
            f"{out.get('burst_count')} 个聚集簇（簇内跨度 ≤{burst_days} 天），"
            f"相邻事件中位间隔 {median_gap} 天；"
            f"最大簇 {biggest.get('event_count')} 起（{biggest.get('start')}"
            f"~{biggest.get('end')}）")
    text += _degraded_note(out)
    claims = [
        f"聚集簇 {out.get('burst_count')} 个",
        f"中位间隔 {median_gap} 天",
        f"最大簇 {biggest.get('event_count')} 起 @ {biggest.get('start')}",
    ]
    return text, claims


def _basis_collision(out: dict) -> tuple[str, list[str]]:
    """timeline_cross_collision：窗口内异质事件汇聚（本函数数据最完整）。"""
    rows = out.get("rows") or []
    if not rows:
        return ("在指定窗口内未检出跨类型事件汇聚"
                f"（锚点 {out.get('anchor_date')}，±{out.get('window_days')} 天）。"), []
    proj = _name(out.get("project"))
    anchor = out.get("anchor_date")
    wd = out.get("window_days")
    parts, claims = [], []
    for r in rows:
        types = r.get("event_types") or []
        parts.append(
            f"{r.get('subject_raw')} 涉 {r.get('type_count')} 类"
            f"（{'、'.join(types)}）共 {r.get('event_count')} 起，"
            f"偏移 {r.get('first_offset')}~{r.get('last_offset')} 天")
        claims.append(
            f"{r.get('subject_raw')}：{r.get('event_count')} 起/"
            f"{r.get('type_count')} 类，偏移 {r.get('first_offset')}~{r.get('last_offset')} 天")
    text = (f"围绕 {proj} 公示日 {anchor} ±{wd} 天窗口内，"
            f"{len(rows)} 个主体命中跨类型汇聚：{'；'.join(parts)}")
    text += _degraded_note(out)
    return text, claims


def _basis_neighborhood(out: dict) -> tuple[str, list[str]]:
    """relation_neighborhood：邻域规模与跳数分布。"""
    nodes = out.get("nodes") or []
    edges = out.get("edges") or []
    if not nodes:
        return "未检出该主体的关系邻域。", []
    sub = _name(out.get("subject"))
    by_hop: dict[int, int] = {}
    for n in nodes:
        h = n.get("hops")
        if isinstance(h, int):
            by_hop[h] = by_hop.get(h, 0) + 1
    hop_desc = "、".join(f"{h} 跳 {c} 个" for h, c in sorted(by_hop.items()))
    others = [n for n in nodes if n.get("hops")]
    type_desc = ""
    if others:
        tc: dict[str, int] = {}
        for n in others:
            tc[n.get("type") or "?"] = tc.get(n.get("type") or "?", 0) + 1
        type_desc = f"，其中 {_types(tc)}"
    text = (f"以 {sub} 为中心共 {len(nodes)} 个主体、{len(edges)} 条边"
            f"（{hop_desc}）{type_desc}")
    text += _degraded_note(out)
    claims = [f"邻域 {len(nodes)} 主体 / {len(edges)} 边", hop_desc]
    return text, claims


def _basis_paths(out: dict) -> tuple[str, list[str]]:
    """relation_paths：路径条数与最短跳数。截断必须明说。"""
    paths = out.get("paths") or []
    if not paths:
        return f"{_name(out.get('subject_a'))} 与 {_name(out.get('subject_b'))} 在限定跳数内无连通路径。", []
    lens = [p.get("length") for p in paths if isinstance(p.get("length"), int)]
    shortest = min(lens) if lens else None
    a, b = _name(out.get("subject_a")), _name(out.get("subject_b"))
    text = (f"{a} 与 {b} 之间检出 {len(paths)} 条路径"
            f"{f'，最短 {shortest} 跳' if shortest is not None else ''}")
    # 截断是最容易误导用户的地方：不说就等于默认查全了
    if out.get("degraded"):
        text += f"；{out.get('degraded_reason')}，样本不完整，不可据此认定路径穷尽"
    claims = [f"路径 {len(paths)} 条", f"最短 {shortest} 跳"]
    return text, claims


def _basis_common_neighbors(out: dict) -> tuple[str, list[str]]:
    """relation_common_neighbors：共同关联主体。无就是无，不凑。"""
    common = out.get("common") or []
    a, b = _name(out.get("subject_a")), _name(out.get("subject_b"))
    if not common:
        return f"{a} 与 {b} 在限定范围内无共同关联主体（检出 0 个）。", []
    text = f"{a} 与 {b} 共同关联 {len(common)} 个中间主体"
    text += _degraded_note(out)
    return text, [f"共同关联 {len(common)} 个"]


_BASIS = {
    "timeline_sequence": _basis_sequence,
    "timeline_rhythm": _basis_rhythm,
    "timeline_cross_collision": _basis_collision,
    "relation_neighborhood": _basis_neighborhood,
    "relation_paths": _basis_paths,
    "relation_common_neighbors": _basis_common_neighbors,
}

# 证伪条件：回答「什么情况下这个判据不成立」。
# 现从本体 verify_playbooks 借口径；镜头级声明应落到 pack.json，
# 这里给兜底值（缺失时不阻塞，但前端应提示未声明）。
_FALSIFICATION = {
    "timeline_sequence": "该主体职务性往来本就密集；若按类型剔除职务性事件后序列不再集中，则不构成异常",
    "timeline_rhythm": "若该主体在项目期本就高频往来，短间隔系工作常态，则节奏异常不成立",
    "timeline_cross_collision": "窗口期资金均为对公工程尾款、通话对象均为项目参建方时，属业务正常耦合",
    "relation_neighborhood": "若该主体为项目负责人，单点汇聚系职务性连接，非利益输送结构",
    "relation_paths": "若中间节点为同一单位代持主体，路径长度不构成亲密关系证据",
    "relation_common_neighbors": "若中间主体为共同参建单位且往来为工程款，则闭环属业务链路",
}


def basis_for(skill_id: str, out: dict) -> dict:
    """生成镜头判据。未知镜头 → 空 basis（不编），调用方应显式提示。"""
    fn = _BASIS.get(skill_id)
    if fn is None:
        return {"basis": "", "falsification": "", "claims": []}
    text, claims = fn(out)
    return {
        "basis": text,
        "falsification": _FALSIFICATION.get(skill_id, ""),
        "claims": claims,
    }


# ----------------------------------------------------------------------
# 从**线索 detail** 重建函数输出形状
# ----------------------------------------------------------------------
# 为什么需要：包 handler 只把函数输出的**子集**写进 detail（如 sequence 没写
# event_count/span_days；cross_collision 每条线索只带单个主体的 events、
# 不带 rows 列表）。而 basis_for 吃的是完整函数输出。
# 存量产物已落盘、不可能回改，读侧必须能自建形状（补算，不编造）。

def _normalize_detail(skill_id: str, d: dict) -> dict:
    """把单条线索的 detail 补成 basis_for 期望的函数输出形状。

    只做**可推导的补算**（从已有字段算得出），缺字段一律留空由判据
    明说受限，不猜、不编。
    """
    out = dict(d or {})

    if skill_id == "timeline_sequence":
        tl = out.get("timeline") or []
        if tl and out.get("event_count") is None:
            out["event_count"] = len(tl)
        if out.get("span_days") is None and len(tl) >= 2:
            try:
                from datetime import date as _d
                f = _d.fromisoformat(str(tl[0].get("date")))
                l = _d.fromisoformat(str(tl[-1].get("date")))
                out["span_days"] = (l - f).days
            except Exception:
                out["span_days"] = None

    elif skill_id == "timeline_rhythm":
        bursts = out.get("bursts") or []
        if bursts:
            if out.get("burst_count") is None:
                out["burst_count"] = len(bursts)
            if out.get("event_count") is None:
                out["event_count"] = sum(
                    len(b.get("events") or []) for b in bursts)

    elif skill_id == "timeline_cross_collision":
        # 单条线索 = 单个主体 → 重建 rows（列表只含自己）
        if not out.get("rows") and out.get("events"):
            out["rows"] = [{
                "subject_raw": out.get("主体") or out.get("subject_raw") or "",
                "event_types": sorted({
                    str(e.get("type")) for e in (out.get("events") or [])
                    if e.get("type")}),
                "type_count": len({
                    str(e.get("type")) for e in (out.get("events") or [])
                    if e.get("type")}),
                "event_count": len(out.get("events") or []),
                "first_offset": min(
                    (e.get("offset_days") for e in (out.get("events") or [])
                     if isinstance(e.get("offset_days"), int)), default=None),
                "last_offset": max(
                    (e.get("offset_days") for e in (out.get("events") or [])
                     if isinstance(e.get("offset_days"), int)), default=None),
            }]

    elif skill_id == "relation_paths":
        # 单条线索只带一条路径（length/nodes），重建 paths 供判据读数
        if not out.get("paths") and out.get("length") is not None:
            out["paths"] = [{"length": out.get("length"),
                             "nodes": out.get("nodes") or [],
                             "edges": out.get("edges") or []}]

    return out


def basis_from_detail(skill_id: str, detail: dict) -> dict:
    """从线索 detail 生成判据（读侧兜底 / 存量产物可用）。

    优先用生产时已写入的 detail["basis"]（完整函数输出算的，最准）；
    没有才从 detail 重建。两者都没有 → 空，调用方显式提示，不硬凑。
    """
    d = detail or {}
    cached = str(d.get("basis") or "").strip()
    if cached:
        return {"basis": cached,
                "falsification": str(d.get("falsification") or "")
                                 or _FALSIFICATION.get(skill_id, ""),
                "claims": list(d.get("claims") or [])}
    if skill_id not in _BASIS:
        return {"basis": "", "falsification": "", "claims": []}
    return basis_for(skill_id, _normalize_detail(skill_id, d))
