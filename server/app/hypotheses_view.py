"""
server/app/hypotheses_view.py
W-P-005 庙算页派生视图（纯只读）。

MiaoSuan 沙盘为 run_all 内存态、server 无持久化；本端点为**派生口径**
（响应 derived:true 标注）：
  - coverage：run_diagnostic 双路覆盖缺口（miaosuan:dimension 声明 /
    miaosuan:dimension:empirical 实证），维度名透传；
  - heatmap：产物线索按 jian_types（五间）× 级别（观察/线索/确认）聚合；
  - candidates：未处置（待查）线索按 priority 取 top 20 —— **纯展示候补，
    不改变交叉等级、不含升格字段**（红线二）；
  - restricted：秩级过滤掉的内间线索（只给 id + 灰显原因，不泄露内容）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.ontology_loader import load_dimensions
from core.registry import ClueStatus
from core.run_health import RunHealth

from server.app.clues_view import (
    _can_see_neijian,
    _load_raw,
    _status_of,
)

_NEIJIAN = "内间"
_JIANS = ["因", "内", "反", "死", "生"]
_LEVELS = ["观察", "线索", "确认"]
_CANDIDATE_LIMIT = 20


def _diag_rows(conn) -> list[dict]:
    try:
        return RunHealth.readonly(conn).rows()
    except Exception:
        return []


def _coverage(rows: list[dict], total_dims: int) -> dict:
    def card(src: str) -> list[dict]:
        out = []
        for r in rows:
            if r.get("kind") != "coverage_gap" or r.get("source") != src:
                continue
            missing = [str(x) for x in
                       ((r.get("detail") or {}).get("missing") or [])]
            out.append({
                "dimension": "、".join(missing) if missing else None,
                "covered": max(0, total_dims - len(missing)),
                "total": total_dims,
                "missing": missing,
                "reason": r.get("reason"),
                "severity": r.get("severity"),
                "created_at": r.get("created_at"),
            })
        return out
    return {"declared": card("miaosuan:dimension"),
            "empirical": card("miaosuan:dimension:empirical")}


def _level_idx(level: Any) -> int | None:
    s = str(level or "")
    for i, lv in enumerate(_LEVELS):
        if s.startswith(lv):
            return i
    return None


def _heatmap(raws: list[dict]) -> dict:
    counts = [[0] * len(_JIANS) for _ in _LEVELS]
    for raw in raws:
        li = _level_idx((raw.get("detail") or {}).get("级别")
                        or (raw.get("detail") or {}).get("level"))
        if li is None:
            continue
        for j in (raw.get("jian_types") or []):
            j = str(j)
            if j and j[0] in _JIANS:
                counts[li][_JIANS.index(j[0])] += 1
    return {"jians": _JIANS, "levels": _LEVELS, "counts": counts}


def _candidates(raws: list[dict], state_map: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    for raw in raws:
        st = _status_of(raw, state_map)
        if st["status"] != ClueStatus.PENDING:
            continue
        det = raw.get("detail") or {}
        reason = (det.get("依据") or det.get("rule_text")
                  or f"产物候选（{raw.get('skill_id') or '检测'}）")
        out.append({
            "clue_id": raw.get("clue_id"),
            "title": raw.get("title", ""),
            "jian_types": raw.get("jian_types") or [],
            "level": det.get("级别") or det.get("level"),
            "priority_score": det.get("priority_score"),
            "reason": str(reason)[:80],
        })
    out.sort(key=lambda c: (
        -(c["priority_score"] if isinstance(c["priority_score"],
                                            (int, float)) else -1),
        c["clue_id"] or ""))
    return out[:_CANDIDATE_LIMIT]


def assemble_hypotheses(*, case_dir: str | Path, state_map: dict[str, dict],
                        role: str = "正兵", conn, pack: str,
                        base_dir: str | Path) -> dict[str, Any]:
    """庙算派生视图。无产物线索 → available:false（空结构，不报错）。"""
    raws, art_ver = _load_raw(Path(case_dir), None)
    see_neijian = _can_see_neijian(role)
    total_dims = len(load_dimensions(pack, Path(base_dir)))

    visible: list[dict] = []
    restricted: list[dict] = []
    for raw in raws:
        if not see_neijian and _NEIJIAN in (raw.get("jian_types") or []):
            restricted.append({"clue_id": raw.get("clue_id"),
                               "reason": "内间线索·权限不足"})
        else:
            visible.append(raw)

    return {
        "available": art_ver is not None,
        "derived": True,
        "coverage": _coverage(_diag_rows(conn), total_dims),
        "heatmap": _heatmap(visible),
        "candidates": _candidates(visible, state_map),
        "restricted": restricted,
    }
