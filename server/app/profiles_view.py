"""
server/app/profiles_view.py
W-P-006 数据画像页组装（只读语义层；health=None 不写库，不产诊断）。

  - 快照感知：OntologyReadGateway + OntologyProfiler 均以 base_dir 指向
    案件快照（规则工坊/数据元/阈值编辑对画像生效）；
  - clean_stats：从 run_diagnostic 的 clean_drop_rate 诊断重建（BUILD 期
    落账口径），供画像 L1/L2 展示每属性清洗前后行数；
  - focus 缺省 = obj_person 首批主体名（query focus 可覆盖，逗号分隔）；
  - 空态（无物化对象）→ available:false + 空结构，不报错。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.gateway import OntologyReadGateway
from core.ontology_profile import OntologyProfiler
from core.run_health import RunHealth

_FOCUS_LIMIT = 10


def _diag_rows(conn) -> list[dict]:
    try:
        return RunHealth.readonly(conn).rows()
    except Exception:
        return []


def rebuild_clean_stats(rows: list[dict]) -> list[dict]:
    """从 clean_drop_rate 诊断重建 profiler 所需 clean_stats 条目。"""
    out: list[dict] = []
    for r in rows:
        if r.get("kind") != "clean_drop_rate":
            continue
        d = r.get("detail") or {}
        rules = d.get("rules") or []
        out.append({
            "object": d.get("object"),
            "property": d.get("prop"),
            "dropped_rows": d.get("dropped_rows"),
            "rows_before": d.get("rows_before"),
            "rule": rules[0] if rules else None,
            "sample_masked": d.get("sample_masked") or [],
        })
    return out


def _default_focus(conn) -> list[str]:
    try:
        rows = conn.execute(
            'SELECT raw_name FROM obj_person WHERE raw_name IS NOT NULL '
            'LIMIT ?', [_FOCUS_LIMIT]).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def assemble_profiles(*, conn, pack: str, base_dir: str | Path,
                      focus: list[str] | None = None,
                      anchor_date: str | None = None) -> dict[str, Any]:
    """画像视图。conn=CaseStore.read_conn。"""
    base = Path(base_dir)
    # Web 展示读面容忍 STALE（源端有新数据时不阻断画像查看；新鲜度提示
    # 由 hypotheses/quality 另路展示），allow_stale 按 gateway 契约留痕。
    gw = OntologyReadGateway(conn, pack, base_dir=base, allow_stale=True)
    try:
        materialized = gw.materialized_objects()
    except Exception:
        materialized = []
    if not materialized:
        return {"available": False,
                "note": "尚未接入数据源（语义层未构建，先导入数据并 BUILD）"}

    focus_entities = focus if focus is not None else _default_focus(conn)
    clean_stats = rebuild_clean_stats(_diag_rows(conn))

    prof = OntologyProfiler(
        gw, pack=pack, focus_entities=focus_entities,
        anchor_date=anchor_date, health=None,
        clean_stats=clean_stats, base_dir=base)
    report = prof.profile_all()

    return {"available": True, "derived": True,
            "focus": focus_entities, "anchor_date": anchor_date,
            **report}
