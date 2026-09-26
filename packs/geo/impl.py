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
