"""
server/app/meta/repo.py
元数据仓储接口（W-004）。

接口先行：测试与服务层依赖本 ABC，SQLite 实现可在不改调用方的前提下
替换为 Postgres（ADR-004：SQLite 是 DuckDB 事务性写入的兜底选择，
非持久立场）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from server.app.meta.models import (
    CaseRecord,
    CaseVersion,
    PackSnapshot,
    Session,
    TaskRow,
    User,
)


class MetaRepo(ABC):
    # ---- 用户 ----
    @abstractmethod
    def create_user(self, user: User) -> None: ...

    @abstractmethod
    def get_user(self, operator: str) -> User | None: ...

    @abstractmethod
    def set_user_status(self, operator: str, status: str) -> None: ...

    # ---- 会话 ----
    @abstractmethod
    def create_session(self, session: Session) -> None: ...

    @abstractmethod
    def get_session(self, token: str) -> Session | None: ...

    @abstractmethod
    def revoke_session(self, token: str) -> None: ...

    # ---- 案件 ----
    @abstractmethod
    def create_case(self, case: CaseRecord) -> None: ...

    @abstractmethod
    def get_case(self, case_id: str) -> CaseRecord | None: ...

    @abstractmethod
    def list_cases(self, tenant_id: str | None = None) -> list[CaseRecord]: ...

    @abstractmethod
    def transition_case(self, case_id: str, target_status: str,
                        by: str) -> CaseRecord: ...

    @abstractmethod
    def set_case_pack(self, case_id: str, pack_id: str,
                      snapshot_at: str) -> None: ...

    # ---- pack 快照 ----
    @abstractmethod
    def create_pack_snapshot(self, snap: PackSnapshot) -> None: ...

    @abstractmethod
    def get_pack_snapshot(self, case_id: str) -> PackSnapshot | None: ...

    # ---- 任务 ----
    @abstractmethod
    def create_task(self, task: TaskRow) -> TaskRow:
        """幂等创建：(case_id, task_type, idem_key) 冲突时返回既有行。"""

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRow | None: ...

    @abstractmethod
    def list_tasks(self, *, case_id: str | None = None,
                   status: str | None = None, limit: int = 100) -> list[TaskRow]: ...

    @abstractmethod
    def list_leaseable(self) -> list[TaskRow]:
        """可认领任务：所属案件当前无 RUNNING 任务的 PENDING，按创建时间 FIFO。"""

    @abstractmethod
    def claim_task(self, task_id: str) -> bool:
        """原子认领：PENDING→RUNNING（条件 UPDATE，抢不到返回 False）。"""

    @abstractmethod
    def update_progress(self, task_id: str, *, pct: float, stage: str,
                        stage_label: str, detail: str) -> None: ...

    @abstractmethod
    def complete_task(self, task_id: str) -> None: ...

    @abstractmethod
    def fail_task(self, task_id: str, *, error_code: str,
                  error_message: str) -> None: ...

    @abstractmethod
    def requeue_task(self, task_id: str, retry_count: int) -> None:
        """失败重试：RUNNING/FAILED → PENDING，递增 retry_count。"""

    # ---- 版本指针（W-007，阶段 D 原子切换）----
    @abstractmethod
    def current_version(self, case_id: str) -> int:
        """案件当前生效版本；无记录返回 0。"""

    @abstractmethod
    def set_version(self, case_id: str, version: int, by: str) -> None:
        """Upsert 版本指针（调用方须在同一事务语义下于构建成功后调用）。"""
