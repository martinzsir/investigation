"""观察档案读面（镜头产出的浏览/筛选/统计）。

观察 ≠ 线索：镜头只摆出数据的某种结构，不下"这是异常"的判断
（无常态基线——规则才有）。故观察有**独立浏览入口**，不混进线索清单：
否则对不上任何已有线索的观察就永远没人看见，自动研判发现未知的价值
会沉没；但也不能占用处置清单的位置（无假设可证伪，只能空转）。

生灭口径
--------
档案本体随版本（RESCAN 重算、可复现）；正兵的**处置动作**（认领/归档/
提升为线索）落 state.sqlite 跨版本持久 —— observation_id 由「镜头+靶心+
参数」派生、不含版本号，故跨版本稳定，操作能挂住。

版本前进后旧观察不在新档案里 → 不消失，标「已不在当前版本」：
正兵认领过的东西不能因为重扫就凭空蒸发，但也要让他知道它已不在当前
研判结果中（可能靶心变了、数据变了）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from server.app.clues_artifact import load_case_observations


def assemble_observations(*, case_dir: str | Path, version: int,
                          dispositions: dict[str, dict] | None = None,
                          skill: str | None = None,
                          subject: str | None = None,
                          disposition: str | None = None,
                          page: int = 1, page_size: int = 50,
                          ) -> dict[str, Any]:
    """观察档案列表：镜头/主体/处置态筛选 + 分页。

    筛选口径
    --------
      skill       : 按镜头（skill_id）
      subject     : 按观察对象（主体名/项目名，模糊匹配）
      disposition : 未认领 / 已认领 / 已归档 / 已提升
                    —— 来自 state.sqlite，跨版本持久

    统计（供 UI 顶部概览）
    ----------------------
      total / 未认领数 / 已提升数 / 按镜头分布
    """
    obs = load_case_observations(case_dir, version)
    disp = dispositions or {}

    rows: list[dict] = []
    for o in obs:
        d = disp.get(o.observation_id) or {}
        rows.append({
            "observation_id": o.observation_id,
            "skill_id": o.skill_id,
            "lens_name": o.lens_name or o.skill_id,
            "title": o.title,
            "subject": o.subject,
            "project": o.project,
            "basis": o.basis,
            "falsification": o.falsification,
            "facts_count": len(o.facts or []),
            "evidence_count": len(o.evidence_refs or []),
            "degraded": o.degraded,
            "degraded_reason": o.degraded_reason,
            "param_source": o.param_source,
            "created_at": o.created_at,
            # 正兵操作（跨版本持久）
            "disposition": d.get("disposition") or "未认领",
            "note": d.get("note") or "",
            "operator": d.get("operator") or "",
            "promoted_clue_id": d.get("promoted_clue_id") or "",
            "promoted_hypothesis": d.get("promoted_hypothesis") or "",
        })

    # ---- 跨版本遗留：认领过但不在当前版本档案里 ----
    # 不隐藏、不删除：标为「已不在当前版本」，让正兵知道它去哪了。
    current_ids = {o.observation_id for o in obs}
    stale: list[dict] = []
    for oid, d in disp.items():
        if oid in current_ids:
            continue
        if not d.get("disposition") or d.get("disposition") == "未认领":
            continue  # 从未认领过的旧观察不必提醒
        stale.append({
            "observation_id": oid,
            "skill_id": "",
            "lens_name": "—",
            "title": "该观察已不在当前版本（重扫后靶心或数据已变）",
            "subject": "", "project": "", "basis": "",
            "falsification": "", "facts_count": 0, "evidence_count": 0,
            "degraded": False, "degraded_reason": "",
            "param_source": "", "created_at": d.get("updated_at") or "",
            "disposition": d.get("disposition") or "",
            "note": d.get("note") or "",
            "operator": d.get("operator") or "",
            "promoted_clue_id": d.get("promoted_clue_id") or "",
            "promoted_hypothesis": d.get("promoted_hypothesis") or "",
            "stale": True,
        })
    for r in rows:
        r["stale"] = False

    # ---- 筛选 ----
    if skill:
        rows = [r for r in rows if r["skill_id"] == skill]
    if subject:
        key = subject.strip()
        rows = [r for r in rows
                if key in (r["subject"] or "") or key in (r["project"] or "")
                or key in (r["title"] or "")]
    if disposition:
        rows = [r for r in rows if r["disposition"] == disposition]

    # ---- 统计（筛选前口径，反映全貌）----
    all_rows = [{
        **r, "disposition": (disp.get(r["observation_id"]) or {}).get(
            "disposition") or "未认领"}
        for r in [{
            "observation_id": o.observation_id, "skill_id": o.skill_id,
            "lens_name": o.lens_name or o.skill_id, "title": o.title,
            "subject": o.subject, "project": o.project,
        } for o in obs]
    ]
    by_lens: dict[str, int] = {}
    for r in all_rows:
        by_lens[r["lens_name"] or r["skill_id"]] = \
            by_lens.get(r["lens_name"] or r["skill_id"], 0) + 1
    stats = {
        "total": len(all_rows),
        "unclaimed": sum(1 for r in all_rows
                         if r["disposition"] == "未认领"),
        "claimed": sum(1 for r in all_rows if r["disposition"] == "已认领"),
        "archived": sum(1 for r in all_rows if r["disposition"] == "已归档"),
        "promoted": sum(1 for r in all_rows if r["disposition"] == "已提升"),
        "by_lens": by_lens,
        "stale": len(stale),
    }

    # ---- 排序：未认领优先（待办在前），其次镜头、对象 ----
    _order = {"未认领": 0, "已认领": 1, "已提升": 2, "已归档": 3}
    rows.sort(key=lambda r: (_order.get(r["disposition"], 9),
                             r["skill_id"], r["subject"] or ""))

    total_filtered = len(rows)
    start = (max(1, page) - 1) * max(1, page_size)
    page_rows = rows[start:start + max(1, page_size)]

    return {
        "version": version,
        "observations": page_rows,
        "stale": stale,
        "stats": stats,
        "page": max(1, page),
        "page_size": max(1, page_size),
        "total": total_filtered,
    }


def assemble_observation_detail(*, case_dir: str | Path, version: int,
                                observation_id: str,
                                dispositions: dict[str, dict] | None = None,
                                ) -> dict | None:
    """单条观察详情：判据 + 证伪条件 + 事实明细 + 证据引用 + 完整 detail。

    事实明细默认只返回前 50 条（与档案内 facts 上限一致）；完整事件序列
    由画布下钻承载——详情页不铺开上百行。
    """
    obs = load_case_observations(case_dir, version)
    o = next((x for x in obs if x.observation_id == observation_id), None)
    if o is None:
        return None
    d = (dispositions or {}).get(o.observation_id) or {}
    return {
        "observation_id": o.observation_id,
        "skill_id": o.skill_id,
        "lens_name": o.lens_name or o.skill_id,
        "title": o.title,
        "subject": o.subject,
        "project": o.project,
        "basis": o.basis,
        "falsification": o.falsification,
        "claims": o.claims or [],
        "facts": o.facts or [],
        "evidence_refs": o.evidence_refs or [],
        "detail": o.detail or {},
        "param_source": o.param_source,
        "degraded": o.degraded,
        "degraded_reason": o.degraded_reason,
        "created_at": o.created_at,
        "disposition": d.get("disposition") or "未认领",
        "note": d.get("note") or "",
        "operator": d.get("operator") or "",
        "promoted_clue_id": d.get("promoted_clue_id") or "",
        "promoted_hypothesis": d.get("promoted_hypothesis") or "",
    }
