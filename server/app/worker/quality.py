"""
server/app/worker/quality.py
W-P-008 数据质量检查快速通道：只读跑合规/新鲜度/敏感列/单位四扫描，
汇总落 per-case state.sqlite（quality_check 表），**不产 DuckDB 版本文件**。

严重度红线：
  - compliance（确定性数据元合规）：有违规 → block，通过 → ok；
  - freshness（确定性时间超期）：超期 → warn，否则 ok；
  - sensitive/unit（统计推断）：severity 封顶 suggest，**永不 block**
    （红线六：误报不阻断流程）。
health=None：扫描不写 run_diagnostic（结果只落 state 报告）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from core import compliance, data_freshness, sensitive_scan, unit_scan
from core.gateway import OntologyReadGateway

from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _compliance_checks(comp: dict) -> list[dict]:
    out = []
    for prop_key, info in (comp.get("by_property") or {}).items():
        obj, _, prop = prop_key.partition(".")
        v = int(info.get("violations") or 0)
        codes = info.get("codes") or {}
        if v:
            message = (f"数据元 {info.get('element')} 合规违规 {v}/"
                       f"{info.get('checked')} 行（违规率 {info.get('rate')}，"
                       f"违规码 {codes}）")
        else:
            message = f"{prop_key} 合规检查通过（{info.get('checked')} 行）"
        out.append({"category": "compliance", "mode": "deterministic",
                    "rule_id": f"compliance:{info.get('element')}",
                    "obj": obj, "prop": prop,
                    "severity": "block" if v else "ok",
                    "count": v, "message": message, "samples_masked": []})
    return out


def _freshness_checks(fresh: dict) -> list[dict]:
    out = []
    for e in fresh.get("objects") or []:
        stale = int(e.get("age_days") or 0) > int(fresh.get("stale_days") or 0)
        message = (f"最新数据 {e.get('latest')}，距今 {e.get('age_days')} 天"
                   + (f" > 阈值 {fresh.get('stale_days')} 天，数据时间旧"
                      if stale else "，数据新鲜"))
        out.append({"category": "freshness", "mode": "deterministic",
                    "rule_id": "data_freshness.stale",
                    "obj": e.get("object"), "prop": e.get("property"),
                    "severity": "warn" if stale else "ok",
                    "count": 1 if stale else 0,
                    "message": message, "samples_masked": []})
    return out


def _sensitive_checks(sens: dict) -> list[dict]:
    out = []
    for d in sens.get("details") or []:
        samples = [ev.get("sample_masked") for ev in (d.get("evidence") or [])
                   if ev.get("sample_masked")]
        out.append({"category": "sensitive", "mode": "heuristic",
                    "rule_id": "sensitive.column_suspect",
                    "obj": d.get("object"), "prop": d.get("property"),
                    "severity": "suggest",  # 红线六：封顶 suggest
                    "count": 1,
                    "message": f"疑似敏感列未声明遮蔽（{d.get('suggestion')}）",
                    "samples_masked": samples})
    return out


def _unit_checks(unit: dict) -> list[dict]:
    out = []
    for m in unit.get("mismatches") or []:
        out.append({"category": "unit", "mode": "heuristic",
                    "rule_id": "unit.magnitude_mismatch",
                    "obj": None, "prop": None,
                    "severity": "suggest",  # 红线六：封顶 suggest
                    "count": 1,
                    "message": (f"数据元 {m.get('element')} 跨表金额量级相差 "
                                f"{m.get('ratio')} 倍：{m.get('low')} vs "
                                f"{m.get('high')}（声明单位 "
                                f"{m.get('units') or '未全声明'}），疑似元/万元"
                                f"混用或金额突增，需人工核对"),
                    "samples_masked": []})
    for mu in unit.get("missing_unit") or []:
        out.append({"category": "unit", "mode": "heuristic",
                    "rule_id": "unit.unit_missing",
                    "obj": mu.get("object"), "prop": mu.get("property"),
                    "severity": "suggest",
                    "count": 1,
                    "message": (f"金额类属性未声明 unit（数据元 "
                                f"{mu.get('element')}），建议在 data_elements "
                                f"补充 元/万元/% 单位声明"),
                    "samples_masked": []})
    return out


def handle_quality(task, *, repo, factory, snapshot_base_for,
                   **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无数据可质检")

    base = snapshot_base_for(case.id)
    store = factory.for_case(case.id, mode="read")
    try:
        # 质检对象是当前 BUILD 产物：STALE（源端有更新未重建）不阻断扫描，
        # 新鲜度本身就是 freshness 扫描的一项检查内容。
        gw = OntologyReadGateway(store.read_conn, case.pack_id,
                                 base_dir=base, allow_stale=True)
        comp = compliance.scan(gw, health=None, base_dir=base)
        fresh = data_freshness.scan(gw, health=None, base_dir=base)
        sens = sensitive_scan.scan(gw, health=None, base_dir=base)
        unit = unit_scan.scan(gw, health=None, base_dir=base)
    finally:
        store.close()

    checks = (_compliance_checks(comp) + _freshness_checks(fresh)
              + _sensitive_checks(sens) + _unit_checks(unit))
    summary = {
        "total": len(checks),
        "passed": sum(1 for c in checks if c["severity"] == "ok"),
        "warnings": sum(1 for c in checks
                        if c["severity"] in ("warn", "suggest")),
        "violations": sum(1 for c in checks if c["severity"] == "block"),
    }

    state = StateStore(case.id, factory.case_dir(case.id) / "state.sqlite")
    try:
        check_id = f"qc_{uuid.uuid4().hex[:12]}"
        state.save_quality_check(
            check_id=check_id, created_at=_now(),
            created_by=str((task.params or {}).get("operator") or ""),
            data_version=ver, summary=summary, checks=checks)
    finally:
        state.close()
    return {"check_id": check_id, "summary": summary, "version": ver}
