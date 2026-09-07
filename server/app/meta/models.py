"""
server/app/meta/models.py
元数据层行模型（W-004）。纯 dataclass，不含 SQL；与 repo_sqlite 的表结构一一对应。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 案件状态机（backend_api.md 2.1）----
CASE_DRAFT = "待建案"
CASE_ACTIVE = "侦查中"
CASE_CLOSED = "已结案"
CASE_ARCHIVED = "已封存"

CASE_STATUSES = (CASE_DRAFT, CASE_ACTIVE, CASE_CLOSED, CASE_ARCHIVED)

# 合法迁移：非法迁移由仓储层硬拒（W-005）
CASE_TRANSITIONS: dict[str, frozenset[str]] = {
    CASE_DRAFT: frozenset({CASE_ACTIVE}),
    CASE_ACTIVE: frozenset({CASE_CLOSED, CASE_ARCHIVED}),
    CASE_CLOSED: frozenset({CASE_ARCHIVED}),
    CASE_ARCHIVED: frozenset(),
}

# ---- 任务状态机（backend_api.md 2.2）----
TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_SUCCEEDED = "SUCCEEDED"
TASK_FAILED = "FAILED"

TASK_STATUSES = (TASK_PENDING, TASK_RUNNING, TASK_SUCCEEDED, TASK_FAILED)

USER_ACTIVE = "active"
USER_DISABLED = "disabled"


class IllegalTransition(ValueError):
    """案件/任务状态机非法迁移。"""


@dataclass
class User:
    operator: str
    password_hash: str
    salt: str
    role: str
    clearance: int
    tenant_id: str
    status: str = USER_ACTIVE
    created_at: str = ""


@dataclass
class Session:
    token: str
    operator: str
    created_at: str
    expires_at: str
    revoked: int = 0  # SQLite 无 bool：0/1


@dataclass
class CaseRecord:
    id: str
    tenant_id: str
    name: str
    status: str = CASE_DRAFT
    pack_id: str = "default"
    pack_snapshot_at: str = ""
    current_version: int = 0
    created_at: str = ""
    created_by: str = ""


@dataclass
class PackSnapshot:
    case_id: str
    pack_id: str
    version: str
    snapshot_path: str
    locked_at: str = ""


@dataclass
class TaskRow:
    id: str
    case_id: str
    task_type: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = TASK_PENDING
    progress_pct: float = 0.0
    progress_stage: str = ""
    progress_label: str = ""
    progress_detail: str = ""
    retry_count: int = 0
    max_retries: int = 3
    idem_key: str = ""
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_code: str = ""
    error_message: str = ""
    created_by: str = ""


@dataclass
class CaseVersion:
    case_id: str
    version: int
    updated_at: str = ""
    updated_by: str = ""


def assert_case_transition(current: str, target: str) -> None:
    """案件状态机硬校验：非法迁移抛 IllegalTransition。"""
    if target == current:
        return
    allowed = CASE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalTransition(
            f"案件状态非法迁移：{current} → {target}（合法去向：{sorted(allowed) or '无（吸收态）'}）")
