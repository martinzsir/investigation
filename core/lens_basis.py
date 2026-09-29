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


def _basis_site_profile(out: dict) -> tuple[str, list[str]]:
    """geo_site_profile：落脚点归并规模、到访集中度、坐标覆盖率。"""
    sites = out.get("sites") or []
    if not sites:
        return "未检出该主体的轨迹落脚点。", []
    sub = _name(out.get("subject"))
    top = sites[0]
    visits = out.get("total_visits") or sum(s.get("visits") or 0 for s in sites)
    cov = out.get("coord_coverage") or {}
    with_c, total = cov.get("with_coords"), cov.get("total")
    text = (f"{sub} 的轨迹归并为 {len(sites)} 个落脚点、共 {visits} 次到访；"
            f"到访最多为「{top.get('std_address')}」"
            f"（{top.get('visits')} 次，{top.get('first_date')}~"
            f"{top.get('last_date')}）")
    if with_c is not None and total:
        text += f"；坐标覆盖 {with_c}/{total}"
    text += _degraded_note(out)
    claims = [
        f"落脚点 {len(sites)} 个 / 到访 {visits} 次",
        f"最高频落脚点「{top.get('std_address')}」{top.get('visits')} 次",
    ]
    if with_c is not None and total:
        claims.append(f"坐标覆盖 {with_c}/{total}")
    return text, claims


def _basis_serial_profile(out: dict) -> tuple[str, list[str]]:
    """geo_serial_profile：CGT 概率面只陈述事件规模与顶格区域，不给定址结论。"""
    used = out.get("events_used")
    if not used:
        return "有效坐标事件不足，未生成地理画像概率面。", []
    sub = _name(out.get("subject"))
    grid = out.get("grid") or {}
    gm = out.get("grid_meters")
    if isinstance(gm, float):
        gm = int(gm)
    top = out.get("top_zone") or {}
    n_zones = len(out.get("priority_zones") or [])
    text = (f"{sub} 的 {used} 起带坐标事件按 Rossmo CGT 生成概率面"
            f"（网格 {grid.get('rows')}×{grid.get('cols')}，格宽约 {gm} 米），"
            f"顶格排查区位于 {round(top.get('lat'), 5) if top.get('lat') is not None else '?'},"
            f"{round(top.get('lng'), 5) if top.get('lng') is not None else '?'}，"
            f"共 {n_zones} 个优先排查网格；产出为排查优先级区域，非定址结论")
    dropped = out.get("events_dropped_no_coord")
    if dropped:
        text += f"；{dropped} 起事件无坐标未纳入"
    text += _degraded_note(out)
    claims = [
        f"纳入概率面事件 {used} 起",
        f"网格 {grid.get('rows')}×{grid.get('cols')}（约 {gm} 米格宽）",
        f"优先排查网格 {n_zones} 个，顶格区 "
        f"{round(top.get('lat'), 5) if top.get('lat') is not None else '?'},"
        f"{round(top.get('lng'), 5) if top.get('lng') is not None else '?'}",
    ]
    return text, claims


def _basis_accompany(out: dict) -> tuple[str, list[str]]:
    """geo_accompany：只陈述反复同框的时空结构，不给同行/同时结论。"""
    comps = out.get("companions") or []
    if not comps:
        return "未检出异主体时空同框事件对。", []
    top = comps[0]
    lv = top.get("spatial_level")
    note = top.get("spatial_note") or lv
    wd = out.get("window_days")
    text = (f"{top.get('person_a')} 与 {top.get('person_b')} 在 ±{wd} 天窗口内"
            f"先后出现于相近时空 {top.get('meet_count')} 次"
            f"（{top.get('first_date')}~{top.get('last_date')}，"
            f"跨度 {top.get('span_days')} 天，涉及 {top.get('location_count')} 个地点）；"
            f"空间判据：{note}")
    if top.get("repeated"):
        text += f"；达到反复伴随下限（≥{out.get('min_meets')} 次）"
    # ---- 精度构成自陈（不加这一句，次数就会骗人）----
    # 12 次里若全是日期级，只证明「前后一天先后出现在同一路段」，
    # 每一次都是独立巧合；时刻级才证明时间窗真重叠。正兵只看
    # 「同框 N 次」无法分辨，故必须把构成写出来。
    _mn = int(top.get("minute_level_meets") or 0)
    _dn = int(top.get("date_level_meets") or 0)
    if _mn or _dn:
        text += (f"；其中 {_mn} 次可判时刻重叠、{_dn} 次仅能判同地异时"
                 f"（加权有效同框 {top.get('effective_meet_score')}）")
    if len(comps) > 1:
        text += f"；另有 {len(comps) - 1} 组主体对"
    text += f"；{out.get('time_note') or '数据无时刻，仅判同地异时'}"
    text += _degraded_note(out)
    claims = [
        f"同框 {top.get('meet_count')} 次，跨度 {top.get('span_days')} 天",
        f"空间判据 {lv}（{note}）",
        f"涉及地点 {top.get('location_count')} 个",
        f"时刻级 {_mn} 次 / 日期级 {_dn} 次（加权有效 "
        f"{top.get('effective_meet_score')}）",
    ]
    return text, claims


def _basis_buffer_scan(out: dict) -> tuple[str, list[str]]:
    """geo_buffer_scan：只陈述「谁在锚点附近出现过」，不给接触/定性结论。

    必须自陈的两件事：坐标精度（质心则距离不代表实际间距）、时间窗
    （无窗则跨全时段，不等于同期）。不自陈就会出现"距离 2156.8 米"
    看起来是精确实距、实际是区级推算的伪精确。
    """
    subs = out.get("subjects") or []
    if not subs:
        return "缓冲区环带内未检出任何主体轨迹事件。", []
    top = subs[0]
    prec = out.get("coord_precision")
    prec_note = ("坐标为区县质心，距离不代表实际间距" if prec != "geocode"
                 else "门牌级坐标实距")
    win = ""
    if out.get("date_from") or out.get("date_to"):
        win = (f"时间窗 {out.get('date_from') or '（起点不限）'}~"
               f"{out.get('date_to') or '（终点不限）'}内")
    else:
        win = "未设时间窗（跨全时段）"
    # 事件数须含内环+环带：只报 ring_count 会出现"0 起事件却有 9 次到访"
    # 的自相矛盾（显式锚点 500m 内环时事件全落内环）。
    band = (out.get("ring_count") or 0) + (out.get("inner_count") or 0)
    text = (f"以锚点为圆心、半径 {out.get('outer_radius_m')} 米范围内，{win}"
            f"检出 {out.get('subject_count')} 个主体、{band} 起事件"
            f"（其中内环 {out.get('inner_count')} 起、环带 "
            f"{out.get('ring_count')} 起）")
    if out.get("self_count"):
        text += f"；锚点主体自身 {out['self_count']} 起已排除（不自我循环）"
    text += (f"；其中 {top.get('name')} 出现 {top.get('count')} 次"
             f"（{top.get('first_date')}~{top.get('last_date')}）")
    if len(subs) > 1:
        text += f"，另有 {len(subs) - 1} 个主体"
    text += f"；空间精度：{prec_note}"
    if out.get("out_of_window"):
        text += f"；{out['out_of_window']} 起落在时间窗外未计入"
    text += _degraded_note(out)
    claims = [
        f"{out.get('outer_radius_m')} 米内 {out.get('subject_count')} 个主体",
        f"{top.get('name')} 出现 {top.get('count')} 次",
        f"空间精度 {prec}",
    ]
    return text, claims


def _basis_trajectory_segment(out: dict):
    """geo_trajectory_segment：只陈述停留/移动的结构，不给行为定性。

    必须自陈的两件事：采样稀疏度（duration 是首尾间隔，不等于连续驻留）、
    坐标精度（质心则距离不可用）。不自陈就会出现"停留 10080 分钟"这类
    把 7 个工作日误并成一次驻留的伪结论。
    """
    stays = out.get("stays") or []
    if not stays:
        return "未识别出停留段。", []
    top = max(stays, key=lambda s: s.get("duration_minutes") or 0)
    text = (f"轨迹切分出 {out.get('stay_count')} 个停留段、"
            f"{out.get('move_count')} 个移动段，累计停留 "
            f"{out.get('total_stay_minutes')} 分钟")
    if out.get("total_move_minutes"):
        text += f"、移动 {out['total_move_minutes']} 分钟"
    text += (f"；最长停留「{top.get('std_address')}」"
             f"{top.get('duration_minutes')} 分钟，到访 "
             f"{out.get('stay_count')} 段")
    text += f"；空间精度：{out.get('coord_precision')}"
    if out.get("trackpoint_usable") is not None:
        text += (f"；可用轨迹 {out['trackpoint_usable']}/"
                 f"{out.get('trackpoint_total')} 条")
    text += _degraded_note(out)
    claims = [
        f"停留段 {out.get('stay_count')} 个",
        f"移动段 {out.get('move_count')} 个",
        f"累计停留 {out.get('total_stay_minutes')} 分钟",
    ]
    return text, claims


def _basis_anomaly_trajectory(out: dict):
    """geo_anomaly_trajectory：只陈述"偏离了什么"，不说"可疑"。

    判据必须写清基线规模（多少天的常驻模式）——没有基线规模的偏离
    声明等于无据。三类偏离分别计数，共现只在异常点上附加。
    """
    base = out.get("baseline") or {}
    anoms = out.get("anomalies") or []
    if not anoms:
        return (f"在 {base.get('days')} 天常驻基线上未检出偏离。", [])
    by = out.get("by_kind") or {}
    parts = []
    if by.get("off_route"):
        parts.append(f"非常驻地点 {by['off_route']} 次")
    if by.get("off_hours"):
        parts.append(f"非常态时段 {by['off_hours']} 次")
    if by.get("off_path"):
        parts.append(f"非常态通勤 {by['off_path']} 次")
    top = anoms[0]
    text = (f"以该主体 {base.get('days')} 天、{base.get('stay_count')} 段停留"
            f"建立的常驻基线（{base.get('site_count')} 个地点，常驻阈值 "
            f"{base.get('rare_ratio')}）之上，检出 {out.get('anomaly_count')} "
            f"处偏离：" + "、".join(parts))
    text += (f"；其中「{top.get('std_address') or top.get('to') or ''}」"
             f"{top.get('date', '')}")
    co = [a for a in anoms if a.get("co_present")]
    if co:
        text += (f"；{len(co)} 处偏离另有他人同日同地出现（"
                 f"{co[0].get('co_present')}）")
    text += f"；空间精度：{out.get('coord_precision')}"
    text += _degraded_note(out)
    claims = [f"基线 {base.get('days')} 天",
              f"偏离 {out.get('anomaly_count')} 处"]
    for k, v in sorted(by.items()):
        claims.append(f"{k} {v}")
    return text, claims


def _basis_activity_range(out: dict):
    """geo_activity_range：只描述活动范围几何，不推断落脚点、不作行为定性。

    必须自陈三件事：椭圆倍率（1σ 只含约 39% 的点，不是活动边界）、坐标精度
    （质心档是区级推算）、权重口径（默认按到访次数加权）。不自陈就会出现
    "把 1σ 椭圆当活动边界"或"把质心算出的密度当门牌级热度"这两类误读。
    """
    e = out.get("std_ellipse") or {}
    c = out.get("mean_center") or {}
    mc = f"({c.get('lat')}, {c.get('lng')})" if c.get("lat") is not None else "—"
    text = (f"{out.get('point_count')} 个落脚点的平均中心 {mc}，"
            f"标准距离 {out.get('standard_distance_m')} 米")
    if e.get("semi_major_m") is not None:
        text += (f"；{e.get('sigma_multiplier')}σ 椭圆长半轴 "
                 f"{e.get('semi_major_m')} 米、短半轴 {e.get('semi_minor_m')} 米、"
                 f"主轴方位 {e.get('azimuth_deg')}°、覆盖约 "
                 f"{e.get('area_km2')} 平方公里")
    text += f"；点位跨度 {out.get('span_km')} 公里"
    text += f"；权重口径：按{'到访次数' if out.get('weight_by') != 'uniform' else '等权'}"
    hs = out.get("hotspots") or []
    if hs:
        top = hs[0]
        text += (f"；密度最高「{top.get('std_address')}」到访 "
                 f"{top.get('visits')} 次、相对密度 {top.get('density')}")
    text += f"；坐标精度：{out.get('coord_precision')}"
    text += _degraded_note(out)
    claims = [f"落脚点 {out.get('point_count')} 个",
              f"标准距离 {out.get('standard_distance_m')} 米"]
    if e.get("semi_major_m") is not None:
        claims.append(f"{e.get('sigma_multiplier')}σ 长半轴 {e.get('semi_major_m')} 米")
        claims.append(f"主轴方位 {e.get('azimuth_deg')}°")
    for h in hs[:3]:
        claims.append(f"热点「{h.get('std_address')}」密度 {h.get('density')}")
    return text, claims


# ----------------------------------------------------------------------
# 裸 Function 包装镜头的判据（PLAN-FUND / PLAN-COMM / 关系包装）
#
# 共同纪律：
#   1. 只陈述函数实际返回的字段，不编造未计算的量；
#   2. 措辞不越界——聚合事实不称"逐笔"，高频不称"突增"，同地不称"同时"；
#   3. 降级/受限明说，不装作完整。
# ----------------------------------------------------------------------

def _basis_fund_integer_transfer(out: dict) -> tuple[str, list[str]]:
    total = out.get("total")
    if total is None:
        return "未检出整数转账聚合结果。", []
    text = (f"{out.get('from_raw')} → {out.get('to_raw')} 整万元转账合计 {total}"
            f"（{out.get('round_unit') or 10000} 元整数倍聚合）；"
            f"成对整万元流向是可识别的过桥结构候选，不声称资金性质")
    if out.get("unresolved_subjects"):
        text += f"；主体 {out['unresolved_subjects']} 未匹配到库内主键，未挂实体引用"
    return text, [
        f"转账合计 {total}",
        f"整数单位 {out.get('round_unit') or 10000} 元",
    ]


def _basis_fund_quarter_deposit(out: dict) -> tuple[str, list[str]]:
    cnt, amt = out.get("cnt"), out.get("amt")
    if cnt is None and amt is None:
        return "未检出季末整数现金存入。", []
    text = (f"{out.get('q')} 季度末窗口（{out.get('window_days')} 天）内"
            f"整万元现金存入 {cnt} 笔、合计 {amt}；"
            f"系季度级聚合，未挂逐笔引用")
    return text, [f"存入笔数 {cnt}", f"存入金额 {amt}",
                  f"季末窗口 {out.get('window_days')} 天"]


def _basis_fund_overpass_two_hop(out: dict) -> tuple[str, list[str]]:
    text = (f"{out.get('source')} → {out.get('bridge')} → {out.get('dest')} 两跳路径"
            f"（入 {out.get('amount_in')} / 出 {out.get('amount_out')}，"
            f"{out.get('engine')} 轨）")
    if out.get("gap_filtered"):
        if out.get("gap_unknown"):
            text += "；两跳间隔日期缺失不可判定，未做时间过滤"
        elif out.get("gap_days") is not None:
            text += f"；两跳间隔 {out['gap_days']} 天"
    else:
        text += "；未启用间隔过滤，不声称过桥成立"
    return text, [f"流入 {out.get('amount_in')}",
                  f"流出 {out.get('amount_out')}"]


def _basis_fund_time_window_collision(out: dict) -> tuple[str, list[str]]:
    text = (f"『{out.get('title')}』公示日 {out.get('pub_date')} 前后，"
            f"{out.get('owner_raw')} 整数资金 {out.get('amount')}"
            f"（偏移 {out.get('offset_days')} 天）"
            f"；碰撞是事实，不声称行贿")
    if out.get("unresolved_project"):
        text += "；项目未匹配到库内主键，未挂项目引用"
    return text, [f"偏移 {out.get('offset_days')} 天",
                  f"整数资金 {out.get('amount')}"]


def _basis_comm_call_frequency(out: dict) -> tuple[str, list[str]]:
    pairs = out.get("pairs") or []
    if not pairs:
        return "未检出高频通话对端。", []
    top = pairs[0]
    diag = out.get("diagnostics") or {}
    text = (f"{top.get('caller_raw')} → {top.get('callee_raw')} 通话 "
            f"{top.get('times')} 次")
    if diag.get("median_value"):
        text += f"，为常态中位数 {diag['median_value']} 的对照判据"
    if diag.get("threshold_used") and not diag.get("median_value"):
        text += (f"；无其他对端可比对，退化为绝对频次阈值 "
                 f"{diag['threshold_used']} 次判据")
    text += "；**不构成突增判定**（缺时间序列基线）"
    return text, [f"头部对端通话 {top.get('times')} 次"]


def _basis_geo_co_located_pairs(out: dict) -> tuple[str, list[str]]:
    text = (f"{out.get('person_1')} 与 {out.get('person_2')} 于 "
            f"{out.get('location')} 同地点 {out.get('count')} 次")
    dates = out.get("dates") or []
    if dates:
        text += f"（{dates[0]} ~ {dates[-1]}）"
    text += "；本口径无时刻且无空间判据，仅支持同地异时，不支持同时/同行结论"
    return text, [f"同地点 {out.get('count')} 次"]


def _basis_relation_org_interest(out: dict) -> tuple[str, list[str]]:
    r = out.get("row") or {}
    matched = r.get("matched_person") or []
    text = (f"组织『{r.get('raw_name') or r.get('org_name')}』的"
            f"法人/关联人字段命中案件知识包主体 {matched}"
            f"（知识包版本 {out.get('knowledge_version')}）；"
            f"人名只来自知识包，不做全库姓名匹配")
    return text, [f"命中主体 {matched}"]


def _basis_relation_jian_cross_level(out: dict) -> tuple[str, list[str]]:
    text = (f"五间交叉等级 {out.get('交叉等级')}："
            f"{out.get('独立源数')} 个独立数据源（{out.get('独立数据源')}），"
            f"命中间类 {out.get('命中间类')}；"
            f"规则为单源=观察、双源=线索、三源+=可立案依据候选。"
            f"这是材料充分度分级，不是证据假设")
    return text, [f"独立源数 {out.get('独立源数')}",
                  f"交叉等级 {out.get('交叉等级')}"]



_BASIS = {
    "timeline_sequence": _basis_sequence,
    "timeline_rhythm": _basis_rhythm,
    "timeline_cross_collision": _basis_collision,
    "relation_neighborhood": _basis_neighborhood,
    "relation_paths": _basis_paths,
    "relation_common_neighbors": _basis_common_neighbors,
    "geo_site_profile": _basis_site_profile,
    "geo_serial_profile": _basis_serial_profile,
    "geo_accompany": _basis_accompany,
    "geo_buffer_scan": _basis_buffer_scan,
    "geo_trajectory_segment": _basis_trajectory_segment,
    "geo_anomaly_trajectory": _basis_anomaly_trajectory,
    "geo_activity_range": _basis_activity_range,
    "fund_integer_transfer": _basis_fund_integer_transfer,
    "fund_quarter_deposit": _basis_fund_quarter_deposit,
    "fund_overpass_two_hop": _basis_fund_overpass_two_hop,
    "fund_time_window_collision": _basis_fund_time_window_collision,
    "comm_call_frequency": _basis_comm_call_frequency,
    "geo_co_located_pairs": _basis_geo_co_located_pairs,
    "relation_org_interest": _basis_relation_org_interest,
    "relation_jian_cross_level": _basis_relation_jian_cross_level,
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
    "geo_site_profile": "若高频地点系职务出行必经点（司机/外勤/巡线等岗位职责），"
                        "则到访频次不构成私人落脚关联；坐标为区划质心时空间精度仅到县级",
    "geo_accompany": "同框若均发生在项目现场、会议场所等职务性地点，或两主体本就属同一单位/同一项目组，则时空接近系工作常态；坐标为区划质心时距离不代表实际间距；数据无时刻，不支持同时/同行结论",
    "geo_buffer_scan": "环带内出现仅说明该主体到过锚点附近，不构成接触/同行/利益关联结论；若锚点为项目现场、办公场所等职务性地点，或主体本就属同一单位/同一项目组，则空间接近系工作常态；坐标为区划质心时距离不代表实际间距；未设时间窗时跨全时段，不等于同期",
    "geo_serial_profile": "CGT 适用于系列侵财/人身案件；若事件点系职务必经点、"
                          "坐标为区划质心（精度仅到县级）或有效事件不足声明下限，"
                          "概率面不成立；产出是排查优先级区域，不是落脚点定址",
    "geo_trajectory_segment": "停留时长为稀疏采样下的当日首尾点间隔，不等于连续驻留；"
                              "跨日不串段、跨日相邻停留之间不构成移动；坐标为区县质心时"
                              "不给出距离，停留判定改按地点实体",
    "geo_anomaly_trajectory": "偏离常驻模式不等于可疑：若偏离系临时出差、职务性外勤、"
                              "数据补录或采样缺失所致，或该地点本就属其工作范围，则偏离"
                              "不成立；基线不足声明天数时不产出异常",
    "geo_activity_range": "活动范围椭圆与核密度是描述统计，不推断落脚点、不作行为定性："
                          "若坐标为区划质心（精度仅到区级）或去重后位置不足 3 个，"
                          "椭圆与密度不成立；椭圆倍率决定覆盖比例（1σ≈39%、2σ≈86%、"
                          "3σ≈99%），椭圆外仍可能有活动；热点不等于落脚点，高频地点"
                          "若系职务必经点（司机/外勤/巡线）则不构成私人落脚关联",
    "fund_integer_transfer": "若转出/转入方为同一单位账户间的正常结算（如材料款、工资代发），则整万元系业务惯例而非过桥；成对流向不等于资金性质",
    "fund_quarter_deposit": "若该账户本就按季收现（个体工商户营业款、工程款回笼），季末整额存入系经营常态；季度聚合不支持逐笔定性",
    "fund_overpass_two_hop": "若中间方与上下游均有真实商业背景（供货、分包、代付），两跳系正常业务链路；未启用间隔过滤时，相隔数年的两笔不构成一条过桥路径",
    "fund_time_window_collision": "若资金为工程尾款/保证金退还且公示前后本就密集，则时间邻近系业务节奏；含对公后缀主体已排除，自然人同名未消歧时存在错配风险",
    "comm_call_frequency": "若主体为项目负责人/对接人，高频通话系职务性沟通；单一对端无对照组时不构成突增，仅为频次事实",
    "geo_co_located_pairs": "同地点若系项目现场、办公场所等职务性地点，或两主体本就属同一单位/项目组，则同地异时系工作常态；本口径无时刻，不支持同时/同行结论",
    "relation_org_interest": "若法人/关联人重名（自然人同名未消歧）或知识包版本过期，命中可能为错配；登记关联不等于实质利益输送",
    "relation_jian_cross_level": "交叉等级只反映已接入数据源的独立数量；未建模数据源不计入，等级偏低可能源于数据未接入而非材料不足",
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
