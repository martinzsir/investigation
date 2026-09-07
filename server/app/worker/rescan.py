"""
server/app/worker/rescan.py
W-014 AC-6：规则工坊调参/启停后重跑（TASK_RESCAN）。

MVP 语义（M3 计划 D-M3 偏差栏明示，防"增量"名不副实）：
  - 复用 BUILD 编排（handle_build）：从案件快照 pack（已含工坊改后的
    rules.json）重编译语义层并产新版本文件，成功才切版本指针；
  - "增量"在 MVP 仅体现为任务级幂等（idem_key 去重）与版本文件基线派生，
    规则引擎本身全量重跑——与 build_ontology 既有幂等语义一致，不引入
    新的 core 增量机制；
  - 任务 params 可携带触发来源 {rule_id, changed: [...]}，落 ops 审计。
"""
from __future__ import annotations

from typing import Any

from server.app.worker.tasks import handle_build


def handle_rescan(task, *, repo, factory, snapshot_base_for, **kw: Any) -> dict:
    """RESCAN = 以案件快照（含工坊规则变更）重跑 BUILD 编排。"""
    result = handle_build(
        task, repo=repo, factory=factory,
        snapshot_base_for=snapshot_base_for, **kw)
    rule_id = (task.params or {}).get("rule_id", "")
    changed = (task.params or {}).get("changed") or []
    repo.record_ops(
        "rule_rescan", task.case_id,
        {"version": result.get("version"), "rule_id": rule_id,
         "changed": changed, "triggered_by": task.created_by})
    return {"rescan": True, **result}
