"""
packs/geo/impl.py
PLAN-GEO-001 P3 空间研判镜头包实现（经 pack_loader 的 "impl:func" 路径加载）。

镜头只做编排与线索化：
  - 计算全部委托给 ontology Function（geo_subject_sites / geo_profile_cgt），
    不自己写 SQL、不直接读 Parquet；
  - 把 Function 结果转为 LineageClue，证据引用全部指向真实 obj_*/lnk_* 行
    （skill_invoke 后处理按 P1/P3 契约校验，悬空引用硬失败）；
  - 零命中/事件数不足不造线索，返回 []；坐标缺口在 Function 层落降级诊断，
    线索 detail 带 degraded/degraded_reason；
  - 措辞只给观测结构与「优先排查区域」候选，不下定址/定性结论（红线 3）。
"""
from __future__ import annotations

from core.functions import FunctionExecutor
from core.graph import NODE_PK_COLUMN
from core.lens_basis import basis_for
from core.registry import LineageClue

# 事件点引用上限（计划 §5.4：按 (src_object, event_pk) 去重、截断 150）
_EVENT_REF_LIMIT = 150
_SITE_REF_LIMIT = 60

_SOURCE_TYPE = "空间研判"


def _invoke(store, fn_name: str, fn_params: dict, health) -> dict:
    return FunctionExecutor(store, health=health).invoke(fn_name, fn_params)


def _note_degraded(ctx, skill_id: str, out: dict, r: dict) -> None:
    """零线索分支的函数层降级原因经 ctx 备注通道透出（不造线索，只留痕）。

    落 run 产物/ops 事件，避免"任务 100% 完成、零观察、无原因"的无声失败。
    """
    if not isinstance(ctx, dict) or not out.get("degraded"):
        return
    prof0 = {}
    profiles = r.get("profiles")
    if isinstance(profiles, list) and profiles and isinstance(profiles[0], dict):
        prof0 = profiles[0]
    ctx.setdefault("lens_notes", []).append({
        "skill_id": skill_id,
        "function": out.get("function"),
        "degraded": True,
        "degraded_reason": (prof0.get("degraded_reason")
                            or out.get("degraded_reason")),
        "events_total": r.get("events_total"),
        "events_used": r.get("events_used"),
        "events_dropped_no_coord": r.get("events_dropped_no_coord"),
        "min_events": r.get("min_events"),
    })


# 时空伴随镜头服务的假设（与 packs/geo/pack.json 的 assumption 一致）
_ACCOMPANY_ASSUMPTION = "H6"


def _clean(params: dict, keys: list[str]) -> dict:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _person_ref(subject: dict) -> dict:
    t = subject["type"]
    return {"kind": "node", "ref": f"obj_{t}#{subject['pk']}",
            "key_column": NODE_PK_COLUMN[t]}


def _location_ref(location_id: str) -> dict:
    return {"kind": "node", "ref": f"obj_location#{location_id}",
            "key_column": "location_id"}


def _trackpoint_ref(event_pk: str) -> dict:
    return {"kind": "node", "ref": f"obj_trackpoint#{event_pk}",
            "key_column": "track_id"}


def site_profile_lens(miao=None, store=None, ctx=None, params=None,
                      health=None) -> list:
    """目标主体落脚点归并 → 1 条聚合线索（到访频次/首末日期/坐标覆盖）。"""
    params = params or {}
    fn_params = _clean(params, ["target_type"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "geo_subject_sites", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "geo_site_profile", out, r)
        return []

    subject, sites = r["subject"], r["sites"]
    refs = [_person_ref(subject)]
    # 落脚点实体（location_id 缺失的 raw 回落点不挂节点引用，不悬空）
    for s in sites[:_SITE_REF_LIMIT]:
        if s.get("location_id"):
            refs.append(_location_ref(s["location_id"]))
    # 事件点跨落脚点去重，截断 150
    seen: set = set()
    for s in sites:
        for pk in s.get("event_pks") or []:
            if pk in seen:
                continue
            if len(seen) >= _EVENT_REF_LIMIT:
                break
            refs.append(_trackpoint_ref(pk))
            seen.add(pk)
        if len(seen) >= _EVENT_REF_LIMIT:
            break
    cov = r.get("coord_coverage") or {}
    refs.append({"kind": "aggregate", "metric": "site_count",
                 "value": r["site_count"]})
    refs.append({"kind": "aggregate", "metric": "total_visits",
                 "value": r["total_visits"]})
    if cov:
        refs.append({"kind": "aggregate", "metric": "coord_coverage",
                     "value": f"{cov.get('with_coords')}/{cov.get('total')}"})

    _b = basis_for("geo_site_profile", r)
    top = sites[0] if sites else {}
    clue = LineageClue(
        skill_id="geo_site_profile",
        title=f"{subject['name']} 的落脚点画像：{r['site_count']} 个落脚点、"
              f"{r['total_visits']} 次到访"
              + (f"（坐标覆盖 {cov.get('with_coords')}/{cov.get('total')}）"
                 if cov else ""),
        evidence_refs=refs,
        detail={
            "function": "geo_subject_sites",
            "basis": _b["basis"],
            "falsification": _b["falsification"],
            "claims": _b["claims"],
            "source_type": _SOURCE_TYPE,
            "hypothesis": f"{subject['name']} 的轨迹在地点维度上汇聚为 "
                          f"{r['site_count']} 个落脚点，高频点「{top.get('std_address')}」"
                          f"到访 {top.get('visits')} 次；是否为私人落脚关联待正兵"
                          f"核查（只出归并结构，不作定性）",
            "evidence_level": "观察",
            "subject": subject,
            "sites": sites,
            "coord_coverage": cov,
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def accompany_lens(miao=None, store=None, ctx=None, params=None,
                   health=None) -> list:
    """时空伴随 → 每**主体对**一条观察（反复同框才是伴随，单次只是同框）。

    为什么按主体对出条
    ------------------
    事件对粒度无法承载"反复"这个语义：3 次同框与 3 对各同框一次，在事件
    对列表里长得一样。按主体对聚合后，同框次数/跨度/地点集/精度分布都
    进 detail，正兵一眼能看出这是"关系"还是"巧合"。

    未达 min_meets 的主体对**仍出观察**，但 repeated=False 且标题不称
    "伴随"——单次同框是事实，只是不足以支撑关系判断。留着比丢掉好：
    它可能是未来补足数据的入口。
    """
    params = params or {}
    fn_params = _clean(params, ["radius_m", "window_days", "min_meets",
                                "max_pairs", "window_minutes"])
    out = _invoke(store, "geo_spatiotemporal_accompany", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "geo_accompany", out, r)
        return []

    clues: list = []
    for c in r.get("companions") or []:
        refs: list[dict] = []
        for pk in (c.get("event_pks") or [])[:_EVENT_REF_LIMIT]:
            refs.append(_trackpoint_ref(pk))
        refs.append({"kind": "aggregate", "metric": "meet_count",
                     "value": c["meet_count"]})
        refs.append({"kind": "aggregate", "metric": "span_days",
                     "value": c.get("span_days")})
        refs.append({"kind": "aggregate", "metric": "location_count",
                     "value": c.get("location_count")})

        # 主体引用归一：两端各自挂引用字典（不合并成一个字段——
        # 一端歧义另一端不歧义是常态，合并就会丢失哪端待裁决）。
        from core.entity_ref import person_ref_dict as _prd
        from core.entity_ref import resolve_person as _rp
        _pr = _rp([c.get("person_a"), c.get("person_b")],
                  conn=getattr(store, "conn", None))
        c["person_a_ref"] = _prd(c.get("person_a"), persons=_pr)
        c["person_b_ref"] = _prd(c.get("person_b"), persons=_pr)
        _b = basis_for("geo_accompany", {**r, **c})
        repeated = bool(c.get("repeated"))
        head = ("反复同框" if repeated else "单次同框")
        clue = LineageClue(
            skill_id="geo_accompany",
            title=f"{c['person_a']} × {c['person_b']} 时空{head}"
                  f"{c['meet_count']} 次（{c.get('first_date')}~"
                  f"{c.get('last_date')}，判据：{c.get('spatial_note')}）",
            evidence_refs=refs,
            detail={
                "function": "geo_spatiotemporal_accompany",
                "basis": _b["basis"],
                "falsification": _b["falsification"],
                "claims": _b["claims"],
                "source_type": _SOURCE_TYPE,
                # 观察本身不带假设链（不是命题），但声明它服务于哪个假设：
                # 正兵提升为线索时以此作为默认候选，避免"提升了却不知道
                # 在验证什么"。与 packs/geo/pack.json 的 assumption 同步。
                "assumed_hypothesis": _ACCOMPANY_ASSUMPTION,
                "hypothesis": (
                    f"{c['person_a']} 与 {c['person_b']} 在 ±"
                    f"{r.get('window_days')} 天内先后出现于 "
                    f"{c.get('location_count')} 个地点共 {c['meet_count']} 次"
                    + ("，达反复伴随下限" if repeated else "，未达反复下限")
                    + "；是否构成私下接触待正兵核查（只出时空结构，不作定性）"),
                "evidence_level": "观察",
                "companion": c,
                "radius_m": r.get("radius_m"),
                "window_days": r.get("window_days"),
                "min_meets": r.get("min_meets"),
                "time_granularity": r.get("time_granularity"),
                "time_note": r.get("time_note"),
                # 时刻窗自陈：让正兵在观察详情里就能看到这次是按分钟判的还是
                # 按天判的——"填了 window_minutes 却没变化"必须有据可查，
                # 否则只能靠猜。
                "window_minutes": r.get("window_minutes"),
                "time_window_mode": r.get("time_window_mode"),
                "minute_windowed_pairs": r.get("minute_windowed_pairs"),
                "pair_count": r.get("pair_count"),
                "repeated": repeated,
                "degraded": bool(c.get("degraded")),
                "degraded_reason": r.get("degraded_reason"),
            },
        )
        clues.append(clue)
    return clues



def buffer_scan_lens(miao=None, store=None, ctx=None, params=None,
                     health=None) -> list:
    """反向追踪：以锚点为中心的双环带扫描 → 每**主体**一条观察。

    为什么按主体出条
    ----------------
    反向追踪要回答「谁在那儿」。事件列表粒度回答不了——正兵拿到 11 条
    事件还得自己数人头。按主体归并后每个主体一条，可直接对该主体深挖
    或提升为线索。

    三处刻意与旧实现不同
    --------------------
    1) 锚点主体自身事件默认排除：以某人重心为锚却扫出他自己，是循环论证；
    2) 距离必带 coord_precision：质心坐标下的米数是区级推算，不是实距；
    3) 支持时间窗：反向追踪的核心是「案发时」谁在现场附近，无窗则跨全时段
       且必须自陈，否则正兵会当成同期。
    """
    params = params or {}
    fn_params = _clean(params, ["inner_radius_m", "outer_radius_m",
                                "date_from", "date_to", "center_lat",
                                "center_lng", "target_type",
                                "exclude_anchor_subject", "max_subjects"])
    tgt = (params.get("target_subject") or params.get("target")
           or "").strip()
    if tgt:
        fn_params["target"] = tgt
    out = _invoke(store, "geo_buffer_scan", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "geo_buffer_scan", out, r)
        return []

    max_subjects = int(params.get("max_subjects", 20) or 20)
    subs = (r.get("subjects") or [])[:max_subjects]
    inner, outer = r.get("inner_radius_m"), r.get("outer_radius_m")
    anchor = r.get("anchor") or {}
    win = ""
    if r.get("date_from") or r.get("date_to"):
        win = (f"{r.get('date_from') or '不限'}~{r.get('date_to') or '不限'}")
    else:
        win = "全时段"
    prec = r.get("coord_precision")

    clues: list = []
    for s in subs:
        refs: list[dict] = []
        for ev in (r.get("ring_events") or []) + (r.get("inner_events") or []):
            if (ev.get("subject_raw") or "").strip() == s.get("name"):
                refs.append(_trackpoint_ref(ev.get("event_pk")))
            if len(refs) >= _EVENT_REF_LIMIT:
                break
        refs.append({"kind": "aggregate", "metric": "visit_count",
                     "value": s.get("count")})
        refs.append({"kind": "aggregate", "metric": "distance_m",
                     "value": s.get("min_distance_m")})

        _b = basis_for("geo_buffer_scan", {**r, "subjects": [s]})
        clue = LineageClue(
            skill_id="geo_buffer_scan",
            # 标题说"范围内"不说"环带"：事件可能全落内环，说环带会误导
            title=(f"{s.get('name')} 在锚点 {outer} 米范围内出现 "
                   f"{s.get('count')} 次"
                   f"（{s.get('first_date')}~{s.get('last_date')}，{win}）"),
            evidence_refs=refs,
            detail={
                "function": "geo_buffer_scan",
                "basis": _b["basis"],
                "falsification": _b["falsification"],
                "claims": _b["claims"],
                "source_type": _SOURCE_TYPE,
                "hypothesis": (
                    f"锚点 {anchor.get('source')} 周边检出 {s.get('name')} "
                    f"{s.get('count')} 次到访；仅陈述该主体到过锚点附近，"
                    f"不构成接触/同行/利益关联结论（定性权属正兵）"),
                "evidence_level": "观察",
                "traced_subject": s,
                "anchor": anchor,
                "inner_radius_m": inner,
                "outer_radius_m": outer,
                "date_from": r.get("date_from"),
                "date_to": r.get("date_to"),
                "subject_count": r.get("subject_count"),
                "self_count": r.get("self_count"),
                "out_of_window": r.get("out_of_window"),
                "coord_precision": prec,
                "distance_note": r.get("distance_note"),
                "degraded": bool(r.get("degraded")),
                "degraded_reason": r.get("degraded_reason"),
            },
        )
        clues.append(clue)
    return clues

def serial_profile_lens(miao=None, store=None, ctx=None, params=None,
                        health=None) -> list:
    """目标主体系列事件 CGT 概率面 → 1 条聚合线索（优先排查区域，非定址）。"""
    params = params or {}
    fn_params = _clean(params, ["target_type", "min_events", "grid_meters",
                                "buffer_m", "top_n"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "geo_profile_cgt", fn_params, health)
    r = out.get("result") or {}
    # hit=False 覆盖：主体不存在 / 有效事件 < min_events / 语义表缺口
    # —— 缺口不充数，零线索不造数；降级原因经 ctx 备注透出到 run 产物。
    if not r.get("hit"):
        _note_degraded(ctx, "geo_serial_profile", out, r)
        return []

    subject = r["subject"]
    grid = r.get("grid") or {}
    zones = r.get("priority_zones") or []
    refs = [_person_ref(subject)]
    for pk in (r.get("event_pks") or [])[:_EVENT_REF_LIMIT]:
        refs.append(_trackpoint_ref(pk))
    refs.append({"kind": "aggregate", "metric": "events_used",
                 "value": r.get("events_used")})
    if r.get("events_dropped_no_coord"):
        refs.append({"kind": "aggregate", "metric": "events_dropped_no_coord",
                     "value": r["events_dropped_no_coord"]})
    refs.append({"kind": "aggregate", "metric": "grid_rows",
                 "value": grid.get("rows")})
    refs.append({"kind": "aggregate", "metric": "grid_cols",
                 "value": grid.get("cols")})
    top = r.get("top_zone") or {}

    _b = basis_for("geo_serial_profile", r)
    clue = LineageClue(
        skill_id="geo_serial_profile",
        title=f"{subject['name']} 的系列案件地理画像：{r.get('events_used')} 起"
              f"坐标事件 → CGT 优先排查区域（网格 {grid.get('rows')}×"
              f"{grid.get('cols')}，顶格区非定址）",
        evidence_refs=refs,
        detail={
            "function": "geo_profile_cgt",
            "basis": _b["basis"],
            "falsification": _b["falsification"],
            "claims": _b["claims"],
            "source_type": _SOURCE_TYPE,
            "hypothesis": f"{subject['name']} 的 {r.get('events_used')} 起坐标事件"
                          f"按 Rossmo CGT 形成概率面，顶格排查网格位于 "
                          f"({top.get('lat')},{top.get('lng')}) 一带；"
                          f"产出为排查优先级区域，落脚点定址与定性权属正兵",
            "evidence_level": "观察",
            "subject": subject,
            "min_events": r.get("min_events"),
            "buffer_m": r.get("buffer_m"),
            "grid_meters": r.get("grid_meters"),
            "grid": grid,
            "events_total": r.get("events_total"),
            "events_used": r.get("events_used"),
            "events_dropped_no_coord": r.get("events_dropped_no_coord"),
            "coord_coverage": r.get("coord_coverage"),
            "top_zone": top,
            "priority_zones": zones,
            # P4 地图展示直接消费（GCJ-02 FeatureCollection，授权后叠高德无偏移）
            "geojson": r.get("geojson"),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def segment_lens(miao=None, store=None, ctx=None, params=None,
                 health=None) -> list:
    """轨迹分段 → 1 条主体级观察（停留段 / 移动段）。

    为什么是观察而不是线索
    ----------------------
    分段只回答"这个人怎么移动"，是结构描述，不是可证伪的命题——它不
    指向任何假设。硬挂假设会重蹈"凭间类硬认亲"的覆辙（分段产出与任何
    现有假设的证据类型都对不上）。等有了明确的业务假设（如"异常停留"
    该挂哪条）再声明，此处不猜。
    """
    params = params or {}
    fn_params = _clean(params, ["stay_radius_m", "stay_min_minutes",
                                "target_type"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "geo_trajectory_segment", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "geo_segment", out, r)
        return []

    subject = r["subject"]
    # ---- 主体引用归一 ----
    # 空间侧写姓名、关系侧写代理键，不固化对齐就联动不了（只能手工 join）。
    # 这里把三件套挂上：pk 供引用、name 供展示、ambiguous 供前端区分
    # 「待裁决」与「查无此人」——后者会让正兵以为数据缺失。
    from core.entity_ref import (attach_person_ref, person_ref_dict,
                                 resolve_person)
    _names = [subject.get("name")]
    for _a in r.get("anomalies") or []:
        _names.extend(_a.get("co_present") or [])
    _persons = resolve_person([_n for _n in _names if _n],
                              conn=getattr(store, "conn", None))
    attach_person_ref(subject, subject.get("name"), persons=_persons)
    for _a in r.get("anomalies") or []:
        _a["co_present_refs"] = [
            person_ref_dict(_n, persons=_persons)
            for _n in (_a.get("co_present") or [])]
    refs = [_person_ref(subject)]
    seen: set = set()
    for s in r.get("stays") or []:
        if s.get("location_id"):
            refs.append(_location_ref(s["location_id"]))
        for pk in s.get("event_pks") or []:
            if pk not in seen and len(seen) < _EVENT_REF_LIMIT:
                refs.append(_trackpoint_ref(pk))
                seen.add(pk)
    refs.append({"kind": "aggregate", "metric": "stay_count",
                 "value": r["stay_count"]})
    refs.append({"kind": "aggregate", "metric": "move_count",
                 "value": r["move_count"]})

    _b = basis_for("geo_trajectory_segment", r)
    top = (r.get("stays") or [{}])[0]
    clue = LineageClue(
        skill_id="geo_segment",
        title=f"{subject['name']} 轨迹分段：{r['stay_count']} 个停留段、"
              f"{r['move_count']} 个移动段（累计停留 "
              f"{r.get('total_stay_minutes')} 分钟）",
        evidence_refs=refs,
        detail={
            "function": "geo_trajectory_segment",
            "basis": _b["basis"],
            "falsification": _b["falsification"],
            "claims": _b["claims"],
            "source_type": _SOURCE_TYPE,
            "hypothesis": (f"{subject['name']} 的轨迹切分为 "
                           f"{r['stay_count']} 段停留与 {r['move_count']} 段移动"
                           f"，最长停留「{top.get('std_address')}」"
                           f"{top.get('duration_minutes')} 分钟；仅陈述移动结构，"
                           f"不作行为定性"),
            "evidence_level": "观察",
            "subject": subject,
            "stays": r.get("stays"), "moves": r.get("moves"),
            "coord_precision": r.get("coord_precision"),
            "sampling_note": r.get("sampling_note"),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def anomaly_lens(miao=None, store=None, ctx=None, params=None,
                 health=None) -> list:
    """常驻基线之上的偏离 → 1 条主体级观察（不作"可疑"定性）。

    为什么一条而不是每处一条
    ------------------------
    偏离是**同一个主体**相对自身基线的多种表现，按处拆条会在画布上堆出
    十几条同主体观察，把"这个人有几类偏离"这个整体判断打碎。共现信息不
    埋在 detail 里就算了——标题明示"其中 N 处另有他人同在"，那是研判抓手。
    """
    params = params or {}
    fn_params = _clean(params, ["stay_radius_m", "stay_min_minutes",
                                "min_baseline_days", "rare_ratio",
                                "max_anomalies", "target_type"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "geo_anomaly_trajectory", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "geo_anomaly", out, r)
        return []

    subject = r["subject"]
    # ---- 主体引用归一 ----
    # 空间侧写姓名、关系侧写代理键，不固化对齐就联动不了（只能手工 join）。
    # 这里把三件套挂上：pk 供引用、name 供展示、ambiguous 供前端区分
    # 「待裁决」与「查无此人」——后者会让正兵以为数据缺失。
    from core.entity_ref import (attach_person_ref, person_ref_dict,
                                 resolve_person)
    _names = [subject.get("name")]
    for _a in r.get("anomalies") or []:
        _names.extend(_a.get("co_present") or [])
    _persons = resolve_person([_n for _n in _names if _n],
                              conn=getattr(store, "conn", None))
    attach_person_ref(subject, subject.get("name"), persons=_persons)
    for _a in r.get("anomalies") or []:
        _a["co_present_refs"] = [
            person_ref_dict(_n, persons=_persons)
            for _n in (_a.get("co_present") or [])]
    refs = [_person_ref(subject)]
    seen: set = set()
    for a in r.get("anomalies") or []:
        if a.get("location_id"):
            refs.append(_location_ref(a["location_id"]))
        for pk in a.get("event_pks") or []:
            if pk not in seen and len(seen) < _EVENT_REF_LIMIT:
                refs.append(_trackpoint_ref(pk))
                seen.add(pk)
    for k, v in sorted((r.get("by_kind") or {}).items()):
        refs.append({"kind": "aggregate", "metric": f"deviation_{k}", "value": v})

    _b = basis_for("geo_anomaly_trajectory", r)
    by = r.get("by_kind") or {}
    co = [a for a in r["anomalies"] if a.get("co_present")]
    head = "、".join(
        p for p in (f"非常驻地点 {by['off_route']} 次" if by.get("off_route") else "",
                    f"非常态时段 {by['off_hours']} 次" if by.get("off_hours") else "",
                    f"非常态通勤 {by['off_path']} 次" if by.get("off_path") else "")
        if p)
    tail = ""
    if co:
        names = sorted({n for a in co for n in a["co_present"]})
        tail = f"，其中 {len(co)} 处另有{'、'.join(names[:3])}同在"
    clue = LineageClue(
        skill_id="geo_anomaly",
        title=f"{subject['name']} 偏离常驻模式 {r['anomaly_count']} 处"
              f"（{head}）{tail}",
        evidence_refs=refs,
        detail={
            "function": "geo_anomaly_trajectory",
            "basis": _b["basis"],
            "falsification": _b["falsification"],
            "claims": _b["claims"],
            "source_type": _SOURCE_TYPE,
            "hypothesis": (f"{subject['name']} 在其 "
                           f"{r['baseline'].get('days')} 天常驻基线之上出现 "
                           f"{r['anomaly_count']} 处偏离；偏离是结构事实，"
                           f"是否可疑须正兵核查（出差、外勤、采样缺失均会致偏离）"),
            "evidence_level": "观察",
            "subject": subject,
            "baseline": r.get("baseline"),
            "anomalies": r.get("anomalies"),
            "by_kind": by,
            "coord_precision": r.get("coord_precision"),
            "anomaly_note": r.get("anomaly_note"),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def activity_range_lens(miao=None, store=None, ctx=None, params=None,
                        health=None) -> list:
    """活动范围画像 → 1 条主体级观察（不推断落脚点、不作行为定性）。

    为什么不挂假设
    --------------
    "活动范围多大、朝哪个方向延展、密度集中在哪"是**描述统计**：它不指向任何
    现有假设的证据类型，也不是可证伪的命题——"某人活动范围 12 平方公里"没有
    对应的证伪条件。硬挂假设会重蹈"凭间类硬认亲"的覆辙。它回答的是研判中的
    背景问题（这个人日常在哪一片活动），为异常检测与时空同框提供参照面，
    本身不构成疑点。

    为什么不按热点拆条
    ------------------
    拆条会在画布上堆出多条同主体观察，把"整体活动范围"这个判断打碎。热点
    作为 detail 里的排名给出，标题只给几何摘要。
    """
    params = params or {}
    fn_params = _clean(params, ["date_from", "date_to", "sigma_multiplier",
                                "bandwidth_m", "grid_size", "min_points",
                                "top_hotspots", "weight_by", "target_type"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "geo_activity_range", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "geo_activity_range", out, r)
        return []

    subject = r["subject"]
    # ---- 主体引用归一 ----
    # 空间侧写姓名、关系侧写代理键，不固化对齐就联动不了（只能手工 join）。
    # 这里把三件套挂上：pk 供引用、name 供展示、ambiguous 供前端区分
    # 「待裁决」与「查无此人」——后者会让正兵以为数据缺失。
    from core.entity_ref import (attach_person_ref, person_ref_dict,
                                 resolve_person)
    _names = [subject.get("name")]
    for _a in r.get("anomalies") or []:
        _names.extend(_a.get("co_present") or [])
    _persons = resolve_person([_n for _n in _names if _n],
                              conn=getattr(store, "conn", None))
    attach_person_ref(subject, subject.get("name"), persons=_persons)
    for _a in r.get("anomalies") or []:
        _a["co_present_refs"] = [
            person_ref_dict(_n, persons=_persons)
            for _n in (_a.get("co_present") or [])]
    refs = [_person_ref(subject)]
    for h in r.get("hotspots") or []:
        if h.get("location_id"):
            refs.append(_location_ref(h["location_id"]))
    e = r.get("std_ellipse") or {}
    refs.append({"kind": "aggregate", "metric": "standard_distance_m",
                 "value": r.get("standard_distance_m")})
    refs.append({"kind": "aggregate", "metric": "ellipse_area_km2",
                 "value": e.get("area_km2")})

    _b = basis_for("geo_activity_range", r)
    mc = r.get("mean_center") or {}
    clue = LineageClue(
        skill_id="geo_activity_range",
        title=(f"{subject['name']} 活动范围：{r['point_count']} 个落脚点、"
               f"标准距离 {r.get('standard_distance_m')} 米、"
               f"{e.get('sigma_multiplier')}σ 椭圆约 {e.get('area_km2')} 平方公里"),
        evidence_refs=refs,
        detail={
            "function": "geo_activity_range",
            "basis": _b["basis"],
            "falsification": _b["falsification"],
            "claims": _b["claims"],
            "source_type": _SOURCE_TYPE,
            "hypothesis": (f"{subject['name']} 的 {r['point_count']} 个落脚点"
                           f"呈平均中心 ({mc.get('lat')}, {mc.get('lng')})、"
                           f"跨度 {r.get('span_km')} 公里的分布；仅描述活动范围"
                           f"几何，不推断落脚点、不作行为定性"),
            "evidence_level": "观察",
            "subject": subject,
            "mean_center": mc,
            "std_ellipse": e,
            "kde": r.get("kde"),
            "hotspots": r.get("hotspots"),
            "bbox": r.get("bbox"),
            "span_km": r.get("span_km"),
            "weight_by": r.get("weight_by"),
            "coord_precision": r.get("coord_precision"),
            "coord_note": r.get("coord_note"),
            "small_sample_note": r.get("small_sample_note"),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


# ----------------------------------------------------------------------
# 旧口径兼容：同地点对（规则 R-GEO-3 → H6）
# ----------------------------------------------------------------------
# 为什么保留：该 Function 在画布 RC-204 白名单里可直连调用（co_located_pairs），
# 包装后与 geo_accompany 同构、可批量调度并出 basis。
# 为什么标注重叠：geo_accompany 有四级空间判据与精度加权，本镜头读的是
# lnk_co_located 语义链接的既有结果，无空间判据。**优先使用 geo_accompany**，
# 本镜头存在只为兼容既有白名单口径，不代表推荐路径。

def _raw_person_ref(store, value):
    """person 引用：**pk 与 raw_name 两种入参都认**。查不到返回 None，不猜不造。

    为什么必须两种都认：lnk_co_located 的 person_1/person_2 存的是**主键**
    （如 person_ecb52c3719fc），而其它语义链接里可能存 raw_name。早前只按
    raw_name 匹配，遇主键即查不到 → 引用静默缺失，线索看着正常却挂不上人。
    """
    v = str(value or "").strip()
    if not v or store is None:
        return None
    for col in ("person_id", "raw_name"):
        try:
            rows = store.query(
                f"SELECT person_id FROM obj_person WHERE {col} = ? LIMIT 1", (v,))
        except Exception:
            continue
        if rows:
            return {"kind": "node",
                    "ref": f"obj_person#{rows[0]['person_id']}",
                    "key_column": "person_id"}
    return None


def _person_display(store, value):
    """显示名：主键反查姓名，查不到原样返回（不编造姓名）。"""
    v = str(value or "").strip()
    if not v or store is None:
        return v
    try:
        rows = store.query(
            "SELECT raw_name FROM obj_person WHERE person_id = ? LIMIT 1", (v,))
    except Exception:
        return v
    return rows[0]["raw_name"] if rows else v


def co_located_pairs_lens(miao=None, store=None, ctx=None, params=None,
                          health=None) -> list:
    """同地点对 → 每个主体对一个地点一条观察（旧口径，无空间判据）。

    按「主体对 × 地点」聚合出条，而不是逐行出条：同一对人在同一地点
    多次同框才构成"反复"，逐行摊开等于没做研判。
    """
    params = params or {}
    fn = "co_located_pairs"
    out = _invoke(store, fn, _clean(params, []), health)
    rows = out.get("rows") or []
    if not rows:
        _note_degraded(ctx, "geo_co_located_pairs", out, out)
        return []

    from core.lens_assumption import resolve_function_assumption as _rfa
    hyp, _why = _rfa(fn)

    # 按 (person_1, person_2, location) 聚合
    buckets: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("person_1"), r.get("person_2"), r.get("location"))
        b = buckets.setdefault(key, {"count": 0, "dates": []})
        b["count"] += 1
        d = r.get("date")
        if d is not None:
            b["dates"].append(str(d))

    clues: list = []
    for (p1, p2, loc), b in buckets.items():
        refs: list[dict] = []
        for name in (p1, p2):
            ref = _raw_person_ref(store, name)
            if ref:
                refs.append(ref)
        refs.append({"kind": "aggregate", "metric": "co_located_count",
                     "value": b["count"]})
        dates = sorted(b["dates"])
        _b = basis_for("geo_co_located_pairs",
                       {"person_1": p1, "person_2": p2, "location": loc,
                        "count": b["count"], "dates": dates})
        clues.append(LineageClue(
            skill_id="geo_co_located_pairs",
            title=f"{_person_display(store, p1)} × {_person_display(store, p2)}"
                  f" 于 {loc} 同地点 {b['count']} 次"
                  f"（{'~'.join([dates[0], dates[-1]]) if dates else '日期未标注'}）",
            evidence_refs=refs,
            detail={
                "function": fn,
                "basis": _b["basis"],
                "falsification": _b["falsification"],
                "claims": _b["claims"],
                "source_type": _SOURCE_TYPE,
                "assumed_hypothesis": hyp,
                "person_1": p1, "person_2": p2, "location": loc,
                "count": b["count"],
                "dates": dates,
                # 精度纪律：本口径无时刻，只知同地异时，不得声称"同框/同时"
                "precision": "date",
                "not_claiming": "同时出现",
                "superseded_by": "geo_accompany",
            },
        ))
    return clues
