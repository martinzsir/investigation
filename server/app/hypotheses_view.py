"""
server/app/hypotheses_view.py
W-P-005 庙算页派生视图（纯只读）。

MiaoSuan 沙盘为 run_all 内存态、server 无持久化；本端点为**派生口径**
（响应 derived:true 标注）：
  - coverage：run_diagnostic 双路覆盖缺口（miaosuan:dimension 声明 /
    miaosuan:dimension:empirical 实证），维度名透传；
  - heatmap：产物线索按 jian_types（兵法五间）× 交叉等级聚合——
    R5 起间类全名/等级名均读 jians.json/cross_levels 声明，不硬编码；
  - candidates：未处置（待查）线索按 priority 取 top 20 —— **纯展示候补，
    不改变交叉等级、不含升格字段**（红线二）；
  - restricted：间类密级过滤掉的线索（只给 id + 灰显原因，不泄露内容）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.ontology_loader import load_dimensions
from core.access import can_see_jian_types, jian_clearance_for_role
from core.registry import ClueStatus
from core.run_health import RunHealth

from server.app import ontology_meta
from server.app.clues_view import (
    _load_raw,
    _status_of,
)

_CANDIDATE_LIMIT = 20
# 旧产物级别别名前缀（全名匹配失败时的兼容窗口，D6）：观察/线索/确认*
_LEGACY_LEVEL_PREFIX = ("观察", "线索", "确认")


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


def _level_idx(level: Any, level_names: list[str]) -> int | None:
    """等级名 → 列序。声明全名精确匹配；旧产物前缀走兼容窗口。"""
    s = str(level or "")
    if not s:
        return None
    if s in level_names:
        return level_names.index(s)
    for name in level_names:                  # 声明名前缀（如旧值"线索(双源)"）
        if s.startswith(name):
            return level_names.index(name)
    for i, pfx in enumerate(_LEGACY_LEVEL_PREFIX):  # 历史别名 确认*
        if s.startswith(pfx) and i < len(level_names):
            return i
    return None


def _heatmap(raws: list[dict], jian_names: list[str],
             level_names: list[str]) -> dict:
    counts = [[0] * len(jian_names) for _ in level_names]
    for raw in raws:
        det = raw.get("detail") or {}
        li = _level_idx(det.get("cross_level") or det.get("级别") or det.get("level"), level_names)
        if li is None:
            continue
        for j in (raw.get("jian_types") or []):
            j = str(j)
            if j in jian_names:               # D6：间类全名精确匹配
                counts[li][jian_names.index(j)] += 1
    return {"jians": jian_names, "levels": level_names, "counts": counts}


def _candidates(raws: list[dict], state_map: dict[str, dict],
                level_ranks: dict[str, int] | None = None) -> list[dict]:
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
            "level": det.get("cross_level") or det.get("级别") or det.get("level"),
            "priority_score": det.get("priority_score"),
            "score_basis": det.get("score_basis"),
            "score_formula": det.get("score_formula"),
            "score_source": det.get("score_source"),
            "reason": str(reason)[:80],
        })
    # P0-1：等级主序 → 分数降序 → clue_id 稳定（与列表/看板三页同序）
    out.sort(key=lambda c: (
        ontology_meta.level_sort_rank(c["level"], level_ranks),
        -(c["priority_score"] if isinstance(c["priority_score"],
                                            (int, float)) else -1),
        c["clue_id"] or ""))
    return out[:_CANDIDATE_LIMIT]


def assemble_hypotheses(*, case_dir: str | Path, state_map: dict[str, dict],
                        role: str = "正兵", conn, pack: str,
                        base_dir: str | Path) -> dict[str, Any]:
    """庙算派生视图。无产物线索 → available:false（空结构，不报错）。"""
    raws, art_ver = _load_raw(Path(case_dir), None)
    # R5：间类/等级名全部声明化；密级按角色对照通用判定（不再硬编码内间）
    jian_names = ontology_meta.jian_names(pack, base_dir)
    level_names = ontology_meta.cross_level_names(pack, base_dir)
    clearances = ontology_meta.jian_clearances(pack, base_dir)
    total_dims = len(load_dimensions(
        pack, ontology_meta.resolve_base(pack, base_dir)))

    ceiling = jian_clearance_for_role(role)
    visible: list[dict] = []
    restricted: list[dict] = []
    for raw in raws:
        jts = raw.get("jian_types") or []
        if not can_see_jian_types(jts, role=role, jian_clearances=clearances):
            blocked = [j for j in jts if clearances.get(j, 99) > ceiling]
            restricted.append({
                "clue_id": raw.get("clue_id"),
                "reason": f"{'/'.join(blocked) or '受护'}线索·权限不足",
            })
        else:
            visible.append(raw)

    return {
        "available": art_ver is not None,
        "derived": True,
        "coverage": _coverage(_diag_rows(conn), total_dims),
        "heatmap": _heatmap(visible, jian_names, level_names),
        "candidates": _candidates(
            visible, state_map,
            ontology_meta.cross_level_rank(pack, base_dir)),
        "restricted": restricted,
    }
