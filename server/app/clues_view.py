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
import logging
from pathlib import Path
from typing import Any

from core.access import can_see_jian_types
from core.registry import ClueStatus

from server.app import ontology_meta
from server.app.clues_artifact import (
    artifact_path,
    latest_artifact_version,
)

logger = logging.getLogger(__name__)


def _with_promoted(case_dir: Path, raws: list[dict]) -> list[dict]:
    """并入**由观察提升**的线索（artifacts/promoted_clues/，案件级）。

    与 lens_runs 的关键差异：提升线索**不挂版本、跨版本持久**。
    提升是正兵的人工认领——他断言"这批观察构成疑点"，这是证据链的一环，
    不能因为重扫（RESCAN）就凭空消失，否则处置记录会变孤儿。

    观察本体仍随版本（可复现），线索只持有引用与摘要，两处不冲突。
    """
    try:
        from server.app.observation_promote import load_promoted_clues
        extra = load_promoted_clues(case_dir)
    except Exception:
        return raws
    return [*raws, *extra] if extra else raws


def _load_raw(case_dir: Path, version: int | None) -> tuple[list[dict], int | None]:
    """返回 (线索 dict 列表, 产物版本号)；无产物 → ([], None)。

    不再并线定向镜头产出：定向与批量统一产**观察**，线索只能由人认领
    （提升）产生。lens_runs/v{N}/*.json 退化为纯运行审计留痕。
    """
    if version is not None:
        p = artifact_path(case_dir, version)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return (_with_promoted(case_dir, data.get("clues", [])),
                    version)
    latest = latest_artifact_version(case_dir)
    if latest is None:
        return [], None
    data = json.loads(artifact_path(case_dir, latest).read_text(encoding="utf-8"))
    return (_with_promoted(case_dir, data.get("clues", [])), latest)


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
        # 无命题可证伪 = 进处置流程只能空转。本体未为该间类声明假设时
        # （如实測死间）假设链为空，此处显式标出，让正兵知道要先指定
        # 验证目标，而不是看到一个"查证中"却不知查证什么。
        "needs_hypothesis": not (raw.get("assumption_chain") or []),
        "hypothesis_gap_reason": det.get("假设缺失原因") or "",
        # 五间本间独立源数（交叉等级判定的真正粒度；非用间线索为 None）
        "jian_sources": det.get("本间独立源数"),
        # 定向镜头运行留痕（非定向线索为 None/空，前端按 lens_run_id 出徽标）
        "lens_run_id": raw.get("lens_run_id") or None,
        "lens_run_at": raw.get("lens_run_at") or None,
        "lens_operator": raw.get("lens_operator") or None,
        **_status_of(raw, state_map),
    }


def _matches(item: dict, raw: dict, *, level: str | None,
             dimension: str | None, jian: str | None,
             subject: str | None, status: str | None,
             skill: str | None = None, lens_run: bool = False) -> bool:
    if level is not None and str(item.get("level") or "") != level:
        return False
    if dimension is not None and str(item.get("dimension") or "") != dimension:
        return False
    if jian is not None and jian not in (item.get("jian_types") or []):
        return False
    if status is not None and item.get("status") != status:
        return False
    if skill is not None and str(item.get("skill_id") or "") != skill:
        return False
    if lens_run and not item.get("lens_run_id"):
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
                  skill: str | None = None, lens_run: bool = False,
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
                        jian=jian, subject=subject, status=status,
                        skill=skill, lens_run=lens_run):
            continue
        items.append(item)
    # 排序（P0-1）：等级主序（声明推导，候选级恒在前；未知/待核实垫底），
    # 同级内 priority_score 降序（None 垫底），其次 priority_rank、clue_id 稳定。
    # 红线：等级只由独立源数决定，分数仅在同级组内排序，不参与定性。
    level_ranks = ontology_meta.cross_level_rank(pack_id, ontology_base)
    items.sort(key=lambda x: (
        ontology_meta.level_sort_rank(x.get("level"), level_ranks),
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


def load_clue_raw(case_dir: str | Path, version: int | None,
                  clue_id: str) -> dict | None:
    """读取线索原始 artifact 行（M4 RC-105 画布建议渲染复用同一取数口径）。

    无版本产物/无此线索返回 None。
    """
    raws, _ = _load_raw(Path(case_dir), version)
    return next((r for r in raws if r.get("clue_id") == clue_id), None)


def build_view_raw(raw: dict, *, cross_rows: list[dict] | None = None,
                   pack_id: str = "default", base_dir=None) -> dict:
    """线索视图行装配（只读、不改 artifact）：合并线索规则回填 +
    方案 B 聚合行回填 + 数据源图章回填。assemble_detail 与 M4 画布
    建议生成共用同一口径。"""
    from server.app.verify_provision import (
        backfill_aggregate_rows,
        backfill_rule_fields,
        stamp_row_datasets,
    )
    raw_for_view = backfill_rule_fields(
        raw, pack_id=pack_id, base_dir=base_dir)
    raw_for_view = backfill_aggregate_rows(raw_for_view, cross_rows)
    return stamp_row_datasets(raw_for_view, pack_id=pack_id, base_dir=base_dir)


def assemble_detail(*, case_dir: str | Path, version: int | None,
                    clue_id: str, state_map: dict[str, dict],
                    decisions: list[dict] | None = None,
                    access=None, pack_id: str = "default",
                    base_dir=None, state_store=None,
                    cross_rows: list[dict] | None = None,
                    provision_suggested: bool = True) -> dict | None:
    """线索详情：五间/溯源 source_rows/合并来源/状态/决策/evidence/source_row_details。

    access（AccessContext）非空时产出 evidence 三栏 + source_row_details 字段表。
    state_store（StateStore）非空时执行 REQ-V-002 惰性供给：evidence 三栏映射为
    auto 核查项 upsert（INSERT OR IGNORE，只补缺），响应附加 verify={items,progress}；
    state.sqlite 不存在（state_store=None）时供给跳过、响应无 verify 键。
    provision_suggested=False（M4 画布路径）：只供给 auto 项，手册建议项
    （verify_playbooks.json）不落 state——未采纳建议仅画布存在（RC-105 AC2）。
    cross_rows（方案 B）：jian_cross_level Function rows（路由持只读连接执行后
    注入），旧版聚合线索（用间交叉）产物无行集时回填表级汇总行；本模块不开库。
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
    # 定向镜头线索的主体/项目/事件对象引用走 evidence_refs（与 detail 的
    # 时间研判结构配套）；画布 lens 成图层依赖它挂主体/项目节点，不透传
    # 会只剩事件与区间、规则没有「涉及」主体的边。
    item["evidence_refs"] = raw.get("evidence_refs") or []
    item["suppressed_log"] = det.get("suppressed_log") or []
    item["artifact_version"] = art_ver
    if decisions is not None:
        item["decisions"] = [d for d in decisions
                             if d.get("target_id") == clue_id]

    # ---- B3：三栏证据 + 溯源面板字段表（遮蔽在服务端做，FE-T-021）----
    if access is not None:
        from server.app.evidence_builder import build_evidence
        from server.app.source_row_dto import resolve_source_rows
        from server.app.verify_provision import (
            provision_from_evidence,
            render_suggested,
        )
        # REQ-V-002 方案 b：合并线索回填规则字段（只渲染、不回写 artifact）；
        # 方案 B：聚合线索回填表级汇总行（cross_rows 由路由注入）；
        # 三栏证据与核查项供给同源一次构建（文本逐字一致）
        raw_for_view = build_view_raw(
            raw, cross_rows=cross_rows, pack_id=pack_id, base_dir=base_dir)
        view_rows = raw_for_view.get("source_rows") or []
        if view_rows and view_rows != source_rows:
            # 回填行只在响应视图层生效（artifact 不可变），溯源面板/三栏同源
            item["source_rows"] = view_rows
        # 合并线索规则回填同步进视图层 detail（REQ-V-002 口径：只渲染、
        # 不回写 artifact）——画布规则节点/详情规则标签与三栏规则卡同源，
        # 否则 seed 出「未关联规则（历史产物）」而 evidence 已是回填后规则
        filled_det = raw_for_view.get("detail")
        if isinstance(filled_det, dict) and filled_det is not det:
            item["detail"] = filled_det
        evidence = build_evidence(
            raw_clue=raw_for_view, conn=None, pack_id=pack_id,
            base_dir=base_dir, access=access)
        item["evidence"] = evidence
        item["source_row_details"] = resolve_source_rows(
            source_rows=view_rows, conn=None, pack_id=pack_id,
            base_dir=base_dir, access=access)
        # REQ-V-002：state.sqlite 存在才供给落库（读面顺带、只补缺）
        if state_store is not None:
            # auto 项始终供给（INSERT OR IGNORE 只补缺）；
            # REQ-V-018 手册建议项仅在 provision_suggested=True 时 upsert
            # （M4 画布路径关闭：未采纳建议仅画布存在，不进核查工作台 AC-105-2）
            provision = provision_from_evidence(evidence)
            if provision_suggested:
                suggested, skipped = render_suggested(
                    raw_for_view, evidence, pack_id=pack_id,
                    base_dir=base_dir)
                provision = provision + suggested
                # D3：读面无 run handle，skipped 以 logging 留痕（不写 run_diagnostic）
                for sk in skipped:
                    logger.warning(
                        "verify_suggest skipped case=%s clue=%s playbook=%s reason=%s",
                        state_store.case_id, clue_id,
                        sk.get("playbook_id"), sk.get("reason"))
            if provision:
                state_store.upsert_verify_items(
                    state_store.case_id, clue_id, provision)
            item["verify"] = {
                "items": state_store.list_verify_items(clue_id),
                "progress": state_store.verify_progress(clue_id),
            }

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
