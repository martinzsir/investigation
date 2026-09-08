"""
server/app/worker/review.py
W-021 人审队列与实体裁决（M4 阶段 C）。

裁决写：入队 REVIEW 任务 → Worker 构建 OrganizationResolver（从 obj_org）
→ core.entity.merge_entities / reject_review（纯函数，不改语义表）
→ 裁决落 state.review_decision + 审计链（StateSink，不产 DuckDB 版本）。

红线 AC-5：系统任何模式下不自动合并 needs_review 候选——只有本任务（人审触发）
才会调用 merge_entities。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from core.entity import (
    OrganizationResolver,
    build_org_table_from_duckdb,
    merge_entities,
    reject_review,
)

from server.app.store.state_sink import StateSink
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError

_ORG_COLS = {"name": "raw_name", "legal_rep": "legal_rep"}


def _build_resolver(conn) -> OrganizationResolver:
    return build_org_table_from_duckdb(conn, table="obj_org", cols=_ORG_COLS)


def handle_review(task, *, repo, factory, **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    p = task.params or {}
    action = str(p.get("action") or "").strip()
    candidate_id = str(p.get("candidate_id") or "").strip()
    reason = str(p.get("reason") or "").strip()
    operator = str(p.get("operator") or "").strip()

    if action not in ("review_merge", "review_reject"):
        raise TaskExecError("UNKNOWN_ACTION", f"未知裁决动作：{action!r}")
    if not candidate_id:
        raise TaskExecError("CANDIDATE_REQUIRED", "缺少 candidate_id")
    if action == "review_reject" and not reason:
        raise TaskExecError("REASON_REQUIRED", "驳回必须给出理由")

    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无实体可裁决")

    store = None
    state = None
    try:
        store = factory.for_case(case.id, mode="read")
        resolver = _build_resolver(store.read_conn)
        if action == "review_merge":
            decision = merge_entities(resolver, candidate_id)
        else:
            decision = reject_review(resolver, candidate_id, reason)
    except FileNotFoundError:
        raise TaskExecError("NO_VERSION", "案件版本库不存在")
    except ValueError as e:
        raise TaskExecError("CANDIDATE_NOT_FOUND", str(e))
    finally:
        if store is not None:
            store.close()

    # 裁决落 state.review_decision + 审计链（不产版本）
    state_path = factory.case_dir(case.id) / "state.sqlite"
    state = StateStore(case.id, state_path)
    try:
        sink = StateSink(state, ontology_version=f"v{ver}")
        decision_id = f"rev_{uuid.uuid4().hex[:12]}"
        sink.conn.execute(
            "INSERT INTO review_decision "
            "(decision_id, kind, target_id, verdict, decided_by, decided_at, "
            "payload_json) VALUES (?,?,?,?,?,?,?)",
            [decision_id, "entity_review", candidate_id,
             decision["action"], operator,
             datetime.now().isoformat(timespec="seconds"),
             json.dumps(decision, ensure_ascii=False, default=str)])
        sink.conn.commit()
        # 审计链追加
        sink.audit_append({
            "event": "entity_review_decision",
            "candidate_id": candidate_id,
            "action": decision["action"],
            "operator": operator,
            "ontology_version": f"v{ver}",
        })
        return {"decision_id": decision_id, **decision}
    finally:
        if state is not None:
            state.close()
