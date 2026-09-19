"""
packs/timeline/impl.py
P5 时间研判镜头包实现（经 pack_loader 的 "impl:func" 路径加载）。

镜头只做编排与线索化：
  - 计算全部委托给 ontology Function（timeline_*，资金/通话/轨迹统一时间轴），
    不自己写 SQL、不直接读 Parquet；
  - 把 Function 结果转为 LineageClue，证据引用全部指向真实 obj_*/lnk_* 行
    （skill_invoke 后处理按 P1/P3 契约校验，悬空引用硬失败）；
  - 零命中不造线索，返回 []；数据源缺口在 Function 层落降级诊断，
    有结果但不完整时线索 detail 带 gaps。
"""
from __future__ import annotations

from datetime import date as _date, timedelta as _td

from core.functions import FunctionExecutor
from core.graph import NODE_PK_COLUMN
from core.registry import LineageClue

_EVENT_REF_LIMIT = 15
_COLLISION_LIMIT = 10

_SOURCE_TYPE = "时间研判"

# 事件源对象 → 行键列（obj_transaction/obj_call/obj_trackpoint）
_EVENT_KEY_COLUMN = {
    "transaction": "txn_id",
    "call": "call_id",
    "trackpoint": "track_id",
}


def _invoke(store, fn_name: str, fn_params: dict, health) -> dict:
    return FunctionExecutor(store, health=health).invoke(fn_name, fn_params)


def _clean(params: dict, keys: list[str]) -> dict:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _entity_ref(subject: dict) -> dict:
    # 实体（person/account/org/bid_project）引用带声明的行键列
    t = subject["type"]
    return {"kind": "node", "ref": f"obj_{t}#{subject['pk']}",
            "key_column": NODE_PK_COLUMN[t]}


def _event_ref(event: dict) -> dict:
    src = event["src_object"]
    return {"kind": "node",
            "ref": f"obj_{src}#{event['event_pk']}",
            "key_column": _EVENT_KEY_COLUMN[src]}


def _time_window_ref(store, project_pk: str, window_days: int,
                     anchor_iso: str, has_fund: bool) -> dict | None:
    """构造 time_window 类证据引用。

    仅当：碰撞含资金事件、窗口 ≤20（lnk_time_window 物化口径 ±20）、
    语义表存在且该项目有邻接行时返回；否则 None（不悬空、不夸大）。
    """
    if not has_fund or window_days > 20:
        return None
    rows = store.query(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_name = 'lnk_time_window'")
    if not rows or rows[0]["c"] == 0:
        return None
    rows = store.query(
        "SELECT project_id FROM lnk_time_window "
        "WHERE project_id = ? LIMIT 1", (project_pk,))
    if not rows:
        return None
    anchor = _date.fromisoformat(anchor_iso)
    return {
        "kind": "time_window",
        "ref": f"lnk_time_window#{project_pk}",
        "key_column": "project_id",
        "time_from": (anchor - _td(days=window_days)).isoformat(),
        "time_to": (anchor + _td(days=window_days)).isoformat(),
    }


def sequence_lens(miao=None, store=None, ctx=None, params=None,
                  health=None) -> list:
    """目标主体跨类型事件序列 → 1 条聚合线索（统一时间轴）。"""
    params = params or {}
    fn_params = _clean(params, ["target_type"])
    fn_params["target"] = params.get("target_subject", "")
    out = _invoke(store, "timeline_event_sequence", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        return []

    subject, timeline = r["subject"], r["timeline"]
    refs = [_entity_ref(subject)]
    for e in timeline[:_EVENT_REF_LIMIT]:
        refs.append(_event_ref(e))
    refs.append({"kind": "aggregate", "metric": "event_count",
                 "value": r["event_count"]})
    refs.append({"kind": "aggregate", "metric": "span_days",
                 "value": r["span_days"]})

    clue = LineageClue(
        skill_id="timeline_sequence",
        title=f"{subject['name']} 的跨类型事件序列：{r['event_count']} 起事件、"
              f"跨度 {r['span_days']} 天",
        evidence_refs=refs,
        detail={
            "function": "timeline_event_sequence",
            "source_type": _SOURCE_TYPE,
            "hypothesis": f"{subject['name']} 在统一时间轴上留下资金/通话/轨迹事件，"
                          f"相邻节奏待正兵核查（只出序列，不作定性）",
            "evidence_level": "观察",
            "subject": subject,
            "timeline": timeline,
            "type_counts": r.get("type_counts", {}),
            "diagnostics": r.get("diagnostics", {}),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def rhythm_lens(miao=None, store=None, ctx=None, params=None,
                health=None) -> list:
    """目标主体周期节奏（聚集簇）→ 1 条聚合线索。"""
    params = params or {}
    fn_params = _clean(params, ["burst_days"])
    fn_params["target"] = params.get("target_subject", "")
    if params.get("target_type"):
        fn_params["target_type"] = params["target_type"]
    out = _invoke(store, "timeline_rhythm", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        return []

    subject, bursts = r["subject"], r["bursts"]
    refs = [_entity_ref(subject)]
    seen_events: set = set()
    for b in bursts:
        for e in b["events"]:
            sig = (e["src_object"], e["event_pk"])
            if sig in seen_events:
                continue
            if len([x for x in refs if x["kind"] == "node"]) - 1 \
                    >= _EVENT_REF_LIMIT:
                break
            refs.append(_event_ref(e))
            seen_events.add(sig)
    refs.append({"kind": "aggregate", "metric": "burst_count",
                 "value": r["burst_count"]})
    refs.append({"kind": "aggregate", "metric": "median_gap_days",
                 "value": r["median_gap_days"]})

    clue = LineageClue(
        skill_id="timeline_rhythm",
        title=f"{subject['name']} 的事件节奏：{r['burst_count']} 个聚集簇、"
              f"常态间隔中位数 {r['median_gap_days']} 天",
        evidence_refs=refs,
        detail={
            "function": "timeline_rhythm",
            "source_type": _SOURCE_TYPE,
            "hypothesis": f"{subject['name']} 的事件在 {r.get('burst_days', 3)} 天窗内"
                          f"成簇出现，节奏聚集待正兵核查（只出节奏，不作定性）",
            "evidence_level": "观察",
            "subject": subject,
            "bursts": bursts,
            "median_gap_days": r["median_gap_days"],
            "diagnostics": r.get("diagnostics", {}),
            "degraded": bool(r.get("degraded")),
            "degraded_reason": r.get("degraded_reason"),
        },
    )
    return [clue]


def cross_collision_lens(miao=None, store=None, ctx=None, params=None,
                         health=None) -> list:
    """锚点项目窗口内同主体跨类型碰撞 → 每个主体 1 条线索。"""
    params = params or {}
    fn_params = _clean(params, ["window_days", "min_event_types"])
    fn_params["project"] = params.get("project", "")
    out = _invoke(store, "timeline_cross_collision", fn_params, health)
    r = out.get("result") or {}
    if not r.get("hit"):
        return []

    window_days = r["window_days"]
    clues = []
    for i, row in enumerate(r["rows"][:_COLLISION_LIMIT], start=1):
        events = row["events"]
        # 单项目调用时项目在顶层；规则引擎全项目扫描时每行自带 project/anchor
        project = row.get("project") or r["project"]
        anchor_iso = row.get("anchor_date") or r["anchor_date"]
        refs = [{"kind": "node",
                 "ref": f"obj_bid_project#{project['pk']}",
                 "key_column": "project_id"}]
        for e in events[:_EVENT_REF_LIMIT]:
            refs.append(_event_ref(e))
        tw = _time_window_ref(
            store, project["pk"], window_days, anchor_iso,
            has_fund="资金" in row["事件类型"])
        if tw is not None:
            refs.append(tw)
        refs.append({"kind": "aggregate", "metric": "碰撞事件数",
                     "value": row["事件数"]})
        refs.append({"kind": "aggregate", "metric": "碰撞类型数",
                     "value": row["类型数"]})
        clues.append(LineageClue(
            skill_id="timeline_cross_collision",
            title=f"{row['主体']} 在{project['name']}公示日前后 {window_days} 天"
                  f"跨类型碰撞：{row['事件类型']}",
            evidence_refs=refs,
            detail={
                "function": "timeline_cross_collision",
                "source_type": _SOURCE_TYPE,
                "hypothesis": f"{row['主体']} 在项目公示日前后 {window_days} 天内"
                              f"出现 {row['事件类型']} 跨类型事件（偏移 "
                              f"{row['最早偏移']:+d}~{row['最晚偏移']:+d} 天），"
                              f"时间协同待正兵核查；只报候选事实，定性权属正兵",
                "evidence_level": "观察",
                "collision_index": i,
                "project": project,
                "anchor_date": anchor_iso,
                "window_days": window_days,
                "主体": row["主体"],
                "events": events,
                "diagnostics": r.get("diagnostics", {}),
                "degraded": bool(r.get("degraded")),
                "degraded_reason": r.get("degraded_reason"),
            },
        ))
    return clues
