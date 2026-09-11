"""
server/app/clues_view.py
线索读面组装（W-019；纯只读，不触发任何任务/扫描，AC-6）。

三源拼接（D-M3-2 / M3 研究结论 4/5）：
  - artifacts/clues_v{N}.json：线索属性/溯源/合并/抑制（随版本不可变）；
  - state.sqlite clue_disposal_status：处置状态真值，覆盖产物内旧状态；
  - 秩级过滤：正兵及以下（rank < 偏将）不见内间线索（REQ-011 AC1 延续，
    与 MCP clue_list 同一判定）。

本模块不含 SQL、不开 DuckDB：产物 JSON + state 只读面即可满足列表/详情，
避免读面给版本文件增加读者租约压力。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.access import can_see_jian_types
from core.registry import ClueStatus

from server.app import ontology_meta
from server.app.clues_artifact import (
    artifact_path,
    latest_artifact_version,
)


def _load_raw(case_dir: Path, version: int | None) -> tuple[list[dict], int | None]:
    """返回 (线索 dict 列表, 产物版本号)；无产物 → ([], None)。"""
    if version is not None:
        p = artifact_path(case_dir, version)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("clues", []), version
    latest = latest_artifact_version(case_dir)
    if latest is None:
        return [], None
    data = json.loads(artifact_path(case_dir, latest).read_text(encoding="utf-8"))
    return data.get("clues", []), latest


def _status_of(raw: dict, state_map: dict[str, dict]) -> dict:
    """状态真值：state 覆盖产物；无 state 记录回落产物 status（缺省待查）。"""
    cid = raw.get("clue_id", "")
    st = state_map.get(cid)
    if st is not None:
        return {"status": st["status"], "note": st.get("note", ""),
                "operator": st.get("operator", ""),
                "updated_at": st.get("updated_at", ""),
                "status_source": "state"}
    return {"status": raw.get("status") or ClueStatus.PENDING,
            "note": raw.get("note", ""), "operator": "",
            "updated_at": "", "status_source": "artifact"}


def _base_item(raw: dict, state_map: dict[str, dict]) -> dict[str, Any]:
    det = raw.get("detail") or {}
    return {
        "clue_id": raw.get("clue_id"),
        "title": raw.get("title", ""),
        "basis": det.get("依据") or raw.get("依据") or "",
        "skill_id": raw.get("skill_id", ""),
        "jian_types": raw.get("jian_types") or [],
        "assumption_chain": raw.get("assumption_chain") or [],
        "level": det.get("cross_level") or det.get("级别") or det.get("level"),
        "dimension": det.get("维度") or det.get("dimension"),
        "priority_rank": det.get("priority_rank"),
        "priority_score": det.get("priority_score"),
        "score_basis": det.get("score_basis"),
        "score_formula": det.get("score_formula"),
        "score_source": det.get("score_source"),
        "source_row_count": len(raw.get("source_rows") or []),
        "merged_from": det.get("merged_from") or [],
        **_status_of(raw, state_map),
    }


def _matches(item: dict, raw: dict, *, level: str | None,
             dimension: str | None, jian: str | None,
             subject: str | None, status: str | None) -> bool:
    if level is not None and str(item.get("level") or "") != level:
        return False
    if dimension is not None and str(item.get("dimension") or "") != dimension:
        return False
    if jian is not None and jian not in (item.get("jian_types") or []):
        return False
    if status is not None and item.get("status") != status:
        return False
    if subject is not None:
        hay = f"{item.get('title', '')} {json.dumps(raw.get('detail') or {}, ensure_ascii=False)}"
        if subject not in hay:
            return False
    return True


def assemble_list(*, case_dir: str | Path, version: int | None,
                  state_map: dict[str, dict], role: str = "正兵",
                  level: str | None = None, dimension: str | None = None,
                  jian: str | None = None, subject: str | None = None,
                  status: str | None = None,
                  page: int = 1, page_size: int = 50,
                  pack_id: str = "default",
                  ontology_base: str | Path | None = None) -> dict:
    """线索列表（筛选/排序/分页）。返回信封 data 结构。"""
    raws, art_ver = _load_raw(Path(case_dir), version)
    # R5：间类可见性按 jians.json default_clearance × 角色密级对照判定，
    # 不再硬编码内间（受护间随包声明变化，未知间类 fail-closed）。
    jian_clearances = ontology_meta.jian_clearances(pack_id, ontology_base)
    filtered_hidden = 0
    items: list[dict] = []
    for raw in raws:
        jts = raw.get("jian_types") or []
        if not can_see_jian_types(jts, role=role,
                                 jian_clearances=jian_clearances):
            filtered_hidden += 1
            continue
        item = _base_item(raw, state_map)
        if not _matches(item, raw, level=level, dimension=dimension,
                        jian=jian, subject=subject, status=status):
            continue
        items.append(item)
    # 排序：priority_score 降序（None 垫底），其次 priority_rank、clue_id 稳定
    items.sort(key=lambda x: (
        -(x.get("priority_score") if isinstance(x.get("priority_score"),
                                                 (int, float)) else -1),
        x.get("priority_rank") or 999,
        x.get("clue_id") or ""))
    total = len(items)
    start = (max(1, page) - 1) * page_size
    page_items = items[start:start + page_size]
    out: dict[str, Any] = {
        "items": page_items, "total": total,
        "page": page, "page_size": page_size,
        "artifact_version": art_ver,
        "available": art_ver is not None,
    }
    if art_ver is None:
        out["note"] = "案件尚未产出线索（先运行 BUILD/RESCAN）"
    if filtered_hidden:
        out["access_note"] = (
            f"role={role}：按间类密级策略过滤线索 {filtered_hidden} 条（REQ-011）")
    return out


def assemble_detail(*, case_dir: str | Path, version: int | None,
                    clue_id: str, state_map: dict[str, dict],
                    decisions: list[dict] | None = None,
                    access=None, pack_id: str = "default",
                    base_dir=None) -> dict | None:
    """线索详情：五间/溯源 source_rows/合并来源/状态/决策/evidence/source_row_details。

    access（AccessContext）非空时产出 evidence 三栏 + source_row_details 字段表。
    无此线索返回 None。
    """
    raws, art_ver = _load_raw(Path(case_dir), version)
    raw = next((r for r in raws if r.get("clue_id") == clue_id), None)
    if raw is None:
        return None
    item = _base_item(raw, state_map)
    source_rows = raw.get("source_rows") or []
    item["source_rows"] = source_rows
    item["audit_log"] = raw.get("audit_log") or []
    det = raw.get("detail") or {}
    item["detail"] = det
    item["suppressed_log"] = det.get("suppressed_log") or []
    item["artifact_version"] = art_ver
    if decisions is not None:
        item["decisions"] = [d for d in decisions
                             if d.get("target_id") == clue_id]

    # ---- B3：三栏证据 + 溯源面板字段表（遮蔽在服务端做，FE-T-021）----
    if access is not None:
        from server.app.evidence_builder import build_evidence
        from server.app.source_row_dto import resolve_source_rows
        item["evidence"] = build_evidence(
            raw_clue=raw, conn=None, pack_id=pack_id,
            base_dir=base_dir, access=access)
        item["source_row_details"] = resolve_source_rows(
            source_rows=source_rows, conn=None, pack_id=pack_id,
            base_dir=base_dir, access=access)

    return item


def assemble_suppressed(*, case_dir: str | Path,
                        version: int | None) -> dict:
    """被抑制记录（不删除仅移出主列表，AC-4）：聚合各线索 detail.suppressed_log。"""
    raws, art_ver = _load_raw(Path(case_dir), version)
    entries: list[dict] = []
    for raw in raws:
        for e in (raw.get("detail") or {}).get("suppressed_log") or []:
            entry = dict(e)
            # 富集：承载该抑制记录的主线索（core 条目本身只有 rule_id/原因）
            entry["host_clue_id"] = raw.get("clue_id")
            entries.append(entry)
    return {"items": entries, "total": len(entries),
            "artifact_version": art_ver, "available": art_ver is not None}
