"""
server/app/portal_view.py
W-P-015 门户页汇总（纯读聚合；待办计数前端不再自行拼 dashboard）。

state.sqlite 的开启由 routers 层负责（state_store 引用门禁：仅
store/routers/worker 可 import），本模块只消费传入的 state_map/chain_ok。

  - todos.clues_pending：产物线索中状态为"待查"的条数（state 真值回灌）；
  - todos.review_pending：PENDING/RUNNING 的 REVIEW 任务数；
  - todos.anomalies_pending：最近一次运行 run_diagnostic 中
    warning/critical 条数；
  - health.chain_ok：state.sqlite 审计链校验（无 state 视为 True）；
  - health.degraded：诊断出现 critical；diagnostics_warn=warning+critical。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.registry import ClueStatus
from core.run_health import RunHealth

from server.app.clues_view import _load_raw, _status_of

_REVIEW_TASK = "REVIEW"
_ACTIVE_TASK_STATES = ("PENDING", "RUNNING")


def assemble_summary(*, case_dir: str | Path, state_map: dict,
                     chain_ok: bool, conn, tasks: list[Any],
                     version: int) -> dict[str, Any]:
    case_dir = Path(case_dir)

    raws, _ = _load_raw(case_dir, version)
    clues_pending = sum(
        1 for raw in raws
        if _status_of(raw, state_map)["status"] == ClueStatus.PENDING)

    review_pending = sum(
        1 for t in tasks
        if t.task_type == _REVIEW_TASK
        and t.status in _ACTIVE_TASK_STATES)

    diagnostics_warn = 0
    degraded = False
    if conn is not None:
        try:
            sev: dict[str, int] = {}
            for r in RunHealth.readonly(conn).rows():
                sev[r.get("severity") or "info"] = (
                    sev.get(r.get("severity") or "info", 0) + 1)
            diagnostics_warn = sev.get("warning", 0) + sev.get("critical", 0)
            degraded = sev.get("critical", 0) > 0
        except Exception:
            pass

    return {
        "todos": {
            "clues_pending": clues_pending,
            "review_pending": review_pending,
            "anomalies_pending": diagnostics_warn,
        },
        "health": {
            "chain_ok": chain_ok,
            "degraded": degraded,
            "diagnostics_warn": diagnostics_warn,
        },
    }
