"""镜头顾问：跑之前先回答两个问题。

  ① **这个镜头适合验证当前假设吗**（相关性推荐）
  ② **现在跑它会不会因为数据没接入而降级**（完整性前置检测）

为什么需要
----------
镜头用不用、用哪个，此前全靠用户自己判断；数据没接入也只有跑完之后
看到 degraded_reason 才知情。两个都发生在「决策之后」，用户要么盲选，
要么白跑一趟。

本模块把判断**前移**到选择时——且只依据本体既有声明，不新增配置字段。

相关性依据（全部是本体现有信息，无需新声明）
--------------------------------------------
  镜头  produces_dims      ←→  假设  dimension
  镜头  consumes_objects   ←→  假设  object_types

即「这个镜头产出的维度，是不是这条假设关心的维度」+「它读的数据，
是不是这条假设涉及的数据」。两个交集越大越贴切。

完整性依据
----------
  镜头 consumes_objects  ∩  已物化对象（gateway.materialized_objects）
  → 未物化的即「数据源未接入」，跑之前就能告知会降级。

设计约束
--------
- **只建议，不拦截**：相关性低不禁止运行（正兵有权用任何手段验证任何假设）；
  数据缺失也不禁止（降级产出仍有价值）。拦截会剥夺人的判断权。
- **不编造**：算不出就说算不出（返回空推荐），不硬凑一个。
- **本体无关**：维度/对象类型全部从声明读，换本体即换口径，无中文硬编码。
"""

from __future__ import annotations

from typing import Any


# ----------------------------------------------------------------------
# ① 镜头 ↔ 假设 相关性
# ----------------------------------------------------------------------

def relevance(dims: list[str], objs: list[str],
              hyp_dims: list[str], hyp_objs: list[str],
              ) -> tuple[int, list[str]]:
    """镜头与假设的贴合度 → (得分, 理由列表)。

    维度交集权重高于对象交集：维度回答「看什么角度」，
    对象只回答「看什么数据」——角度对不上，数据再多也不贴切。
    """
    d = set(dims or []) & set(hyp_dims or [])
    o = set(objs or []) & set(hyp_objs or [])
    score = 2 * len(d) + len(o)
    reasons: list[str] = []
    if d:
        reasons.append(f"产出维度覆盖假设关切维度：{'、'.join(sorted(d))}")
    if o:
        reasons.append(f"读取对象命中假设涉及对象：{'、'.join(sorted(o))}")
    return score, reasons


def rank_for_hypothesis(specs: list[Any], hyp: dict | None) -> list[dict]:
    """按与给定假设的贴合度给镜头排序。

    specs: SkillSpec 列表（取 skill_id/name/produces_dims/consumes_objects/
           description）
    hyp  : 假设 dict（取 dimension / object_types）；None → 无假设，返回
           空推荐（不硬凑）。

    返回 [{skill_id, name, score, reasons, recommendation}]，score 降序。
    score == 0 的镜头仍返回，但 recommendation 为 " unrelated "，
    由 UI 决定是否折叠——**不代替用户做排除决定**。
    """
    if not hyp:
        return []
    hyp_dims = list(hyp.get("dimension") or [])
    hyp_objs = list(hyp.get("object_types") or [])
    if not hyp_dims and not hyp_objs:
        return []

    out: list[dict] = []
    for s in specs:
        sid = getattr(s, "skill_id", "")
        if not sid:
            continue
        score, reasons = relevance(
            list(getattr(s, "produces_dims", None) or []),
            list(getattr(s, "consumes_objects", None) or []),
            hyp_dims, hyp_objs)
        if not reasons:
            reasons = ["产出维度与假设关切维度无交集"]
        out.append({
            "skill_id": sid,
            "name": getattr(s, "name", sid),
            "description": getattr(s, "description", "") or "",
            "score": score,
            "reasons": reasons,
            "recommendation": ("recommended" if score >= 3
                               else "possible" if score >= 1 else "unrelated"),
        })
    out.sort(key=lambda x: (-x["score"], x["skill_id"]))
    return out


# ----------------------------------------------------------------------
# ② 数据完整性前置检测
# ----------------------------------------------------------------------

def readiness(spec: Any, materialized: list[str] | None,
              materialized_links: list[str] | None = None,
              ) -> dict[str, Any]:
    """跑这个镜头前，看它依赖的数据接没接好。

    spec               : SkillSpec（取 consumes_objects）
    materialized       : 已物化**对象**名（obj_* 实表去掉前缀）
    materialized_links : 已物化**链接**名（lnk_* 实表去掉前缀）

    为什么必须分开传两个集合
    ------------------------
    consumes_objects 声明里**混着对象与链接**（如 timeline_* 依赖
    transaction/call（对象）与 time_window（链接）；relation_* 依赖
    transfers/calls_to（链接））。只按 obj_* 比对会把链接全部误判为
    「未接入」——实测六个镜头全被误报降级，而它们实际数据都在。

    返回 {ready, missing, note}：**不禁止运行**，只提前告知会降级。
    """
    deps = list(getattr(spec, "consumes_objects", None) or [])
    if not deps:
        # 无依赖声明 → 无从判断，如实说不知道，不假装 ready
        return {"ready": True, "missing": [], "deps": [],
                "note": "该镜头未声明数据依赖，无法预判完整性"}
    have = set(materialized or []) | set(materialized_links or [])
    missing = sorted(d for d in deps if d not in have)
    if not missing:
        return {"ready": True, "missing": [], "deps": deps, "note": ""}
    return {
        "ready": False,
        "missing": missing,
        "deps": deps,
        "note": (f"依赖 {'、'.join(missing)} 尚未接入，"
                 f"本次产出将降级（不完整），判据会一并说明受限范围"),
    }


def advisories(specs: list[Any], materialized: list[str] | None,
               materialized_links: list[str] | None = None,
               ) -> list[dict]:
    """批量：每个镜头的就绪情况（供运行弹窗在列出镜头时一并展示）。"""
    out = []
    for s in specs:
        sid = getattr(s, "skill_id", "")
        if not sid:
            continue
        r = readiness(s, materialized, materialized_links)
        r["skill_id"] = sid
        r["name"] = getattr(s, "name", sid)
        out.append(r)
    return out


# ----------------------------------------------------------------------
# ③ 时间精度前置探测（时刻窗能不能用）
# ----------------------------------------------------------------------

def trackpoint_time_precision(store) -> dict[str, Any] | None:
    """探测 obj_trackpoint 的实际时间精度 → 回答「时刻窗现在能不能用」。

    为什么需要
    ----------
    window_minutes（时刻窗）只在参与配对的两条轨迹**都精确到分钟档**时才
    按真实秒间隔过滤。数据是纯日期时，正兵填了也不会有任何变化——这是最
    糟的失败模式：**无声无效**（填了、跑了、结果一样，没有任何提示）。

    所以把它前移到选择镜头时告知，与 readiness 同理念（跑之前说，不跑完才说）。

    判定口径与 core.time_semantics 一致：时刻全零（整点）派生为 hour 档，
    不是"有 timestamp 就等于精确"——否则同落 00:00:00 会被判成同时。

    返回 {time_precision, total, with_ts, minute_level, note}；
    表/列不存在或探测失败 → None（如实说不知道，不谎报 ready）。
    """
    try:
        cols = {str(r[0] if not isinstance(r, dict) else r.get("column_name"))
                for r in store.query(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'obj_trackpoint'")}
    except Exception:
        return None
    if "timestamp" not in cols:
        return None
    try:
        rows = store.query(
            "SELECT count(*) AS total, "
            "  sum(CASE WHEN timestamp IS NULL THEN 0 ELSE 1 END) AS with_ts, "
            "  sum(CASE WHEN timestamp IS NOT NULL AND ("
            "      EXTRACT(minute FROM timestamp) > 0 "
            "      OR EXTRACT(second FROM timestamp) > 0) "
            "      THEN 1 ELSE 0 END) AS minute_level "
            "FROM obj_trackpoint")
        r = rows[0] if rows else None
        if not r:
            return None
        total = int(r[0] if not isinstance(r, dict) else r.get("total") or 0)
        with_ts = int(r[1] if not isinstance(r, dict) else r.get("with_ts") or 0)
        minute_level = int(r[2] if not isinstance(r, dict)
                           else r.get("minute_level") or 0)
    except Exception:
        return None
    if total <= 0:
        return None
    if minute_level > 0:
        precision = "minute"
        note = (f"{minute_level}/{total} 条轨迹含分钟级时刻，"
                f"时刻窗（window_minutes）可用")
    elif with_ts > 0:
        precision = "hour"
        note = (f"{with_ts}/{total} 条轨迹含时刻但均为整点（hour 档），"
                f"时刻窗不生效——整点派生为小时精度，不判「同时」")
    else:
        precision = "date"
        note = (f"{total} 条轨迹均无时刻（date 档），时刻窗不生效；"
                f"仍按 window_days 天级判定同地异时")
    return {"time_precision": precision, "total": total,
            "with_ts": with_ts, "minute_level": minute_level, "note": note}
