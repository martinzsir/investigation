"""
server/app/meta/repo_sqlite.py
MetaRepo 的 SQLite-WAL 实现（W-004）。

- 仅依赖标准库 sqlite3（M1 依赖最小化，与项目风格一致）；
- WAL：多读单写不互斥；每次操作开短连接，天然线程安全
  （Worker 线程池与 API 线程共享同一 meta 文件）；
- 状态机非法迁移、幂等 UNIQUE 冲突均在 SQL 层兜底。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from server.app.meta.models import (
    CASE_STATUSES,
    CaseRecord,
    CaseVersion,
    IllegalTransition,
    PackSnapshot,
    Session,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_STATUSES,
    TASK_SUCCEEDED,
    TASK_FAILED,
    TaskRow,
    User,
    assert_case_transition,
)
from server.app.meta.repo import MetaRepo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_users (
    operator      VARCHAR PRIMARY KEY,
    password_hash VARCHAR NOT NULL,
    salt          VARCHAR NOT NULL,
    role          VARCHAR NOT NULL,
    clearance     INTEGER NOT NULL DEFAULT 1,
    tenant_id     VARCHAR NOT NULL,
    status        VARCHAR NOT NULL DEFAULT 'active',
    created_at    VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_sessions (
    token      VARCHAR PRIMARY KEY,
    operator   VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    expires_at VARCHAR NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
    id                 VARCHAR PRIMARY KEY,
    tenant_id          VARCHAR NOT NULL,
    name               VARCHAR NOT NULL,
    status             VARCHAR NOT NULL DEFAULT '待建案',
    pack_id            VARCHAR NOT NULL DEFAULT 'default',
    pack_snapshot_at   VARCHAR NOT NULL DEFAULT '',
    current_version    INTEGER NOT NULL DEFAULT 0,
    created_at         VARCHAR NOT NULL,
    created_by         VARCHAR NOT NULL DEFAULT '',
    UNIQUE(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS case_pack_snapshots (
    case_id       VARCHAR PRIMARY KEY,
    pack_id       VARCHAR NOT NULL,
    version       VARCHAR NOT NULL,
    snapshot_path VARCHAR NOT NULL,
    locked_at     VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS case_version (
    case_id    VARCHAR PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at VARCHAR NOT NULL,
    updated_by VARCHAR NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    id              VARCHAR PRIMARY KEY,
    case_id         VARCHAR NOT NULL,
    task_type       VARCHAR NOT NULL,
    params_json     TEXT NOT NULL DEFAULT '{}',
    status          VARCHAR NOT NULL DEFAULT 'PENDING',
    progress_pct    REAL NOT NULL DEFAULT 0,
    progress_stage  VARCHAR NOT NULL DEFAULT '',
    progress_label  VARCHAR NOT NULL DEFAULT '',
    progress_detail TEXT NOT NULL DEFAULT '',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    idem_key        VARCHAR,
    created_at      VARCHAR NOT NULL,
    updated_at      VARCHAR NOT NULL,
    started_at      VARCHAR NOT NULL DEFAULT '',
    finished_at     VARCHAR NOT NULL DEFAULT '',
    error_code      VARCHAR NOT NULL DEFAULT '',
    error_message   TEXT NOT NULL DEFAULT '',
    created_by      VARCHAR NOT NULL DEFAULT '',
    UNIQUE(case_id, task_type, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_case ON tasks(case_id, created_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SqliteMetaRepo(MetaRepo):
    """SQLite-WAL 元数据仓储。db_path=':memory:' 时每连接独立内存库
    （仅单连接自测用；多线程/多连接测试须用临时文件）。"""

    def __init__(self, db_path: str | Path = "meta/meta.db"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---- 连接 ----
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            # executescript 隐式提交并自行管理 DDL 事务，不再外包 BEGIN/COMMIT
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ---- 行映射 ----
    @staticmethod
    def _user(r: sqlite3.Row) -> User:
        return User(operator=r["operator"], password_hash=r["password_hash"],
                    salt=r["salt"], role=r["role"], clearance=r["clearance"],
                    tenant_id=r["tenant_id"], status=r["status"],
                    created_at=r["created_at"])

    @staticmethod
    def _session(r: sqlite3.Row) -> Session:
        return Session(token=r["token"], operator=r["operator"],
                       created_at=r["created_at"], expires_at=r["expires_at"],
                       revoked=r["revoked"])

    @staticmethod
    def _case(r: sqlite3.Row) -> CaseRecord:
        return CaseRecord(id=r["id"], tenant_id=r["tenant_id"], name=r["name"],
                          status=r["status"], pack_id=r["pack_id"],
                          pack_snapshot_at=r["pack_snapshot_at"],
                          current_version=r["current_version"],
                          created_at=r["created_at"], created_by=r["created_by"])

    @staticmethod
    def _snap(r: sqlite3.Row) -> PackSnapshot:
        return PackSnapshot(case_id=r["case_id"], pack_id=r["pack_id"],
                            version=r["version"], snapshot_path=r["snapshot_path"],
                            locked_at=r["locked_at"])

    @staticmethod
    def _task(r: sqlite3.Row) -> TaskRow:
        return TaskRow(
            id=r["id"], case_id=r["case_id"], task_type=r["task_type"],
            params=json.loads(r["params_json"] or "{}"),
            status=r["status"], progress_pct=r["progress_pct"],
            progress_stage=r["progress_stage"], progress_label=r["progress_label"],
            progress_detail=r["progress_detail"], retry_count=r["retry_count"],
            max_retries=r["max_retries"], idem_key=r["idem_key"] or "",
            created_at=r["created_at"], updated_at=r["updated_at"],
            started_at=r["started_at"], finished_at=r["finished_at"],
            error_code=r["error_code"], error_message=r["error_message"],
            created_by=r["created_by"])

    # ---- 用户 ----
    def create_user(self, user: User) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO meta_users (operator,password_hash,salt,role,"
                "clearance,tenant_id,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user.operator, user.password_hash, user.salt, user.role,
                 user.clearance, user.tenant_id, user.status,
                 user.created_at or _now()))
        finally:
            conn.close()

    def get_user(self, operator: str) -> User | None:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM meta_users WHERE operator=?", (operator,)).fetchone()
            return self._user(r) if r else None
        finally:
            conn.close()

    def set_user_status(self, operator: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE meta_users SET status=? WHERE operator=?",
                         (status, operator))
        finally:
            conn.close()

    # ---- 会话 ----
    def create_session(self, session: Session) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO meta_sessions (token,operator,created_at,expires_at,"
                "revoked) VALUES (?,?,?,?,?)",
                (session.token, session.operator, session.created_at or _now(),
                 session.expires_at, session.revoked))
        finally:
            conn.close()

    def get_session(self, token: str) -> Session | None:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM meta_sessions WHERE token=?", (token,)).fetchone()
            return self._session(r) if r else None
        finally:
            conn.close()

    def revoke_session(self, token: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE meta_sessions SET revoked=1 WHERE token=?",
                         (token,))
        finally:
            conn.close()

    # ---- 案件 ----
    def create_case(self, case: CaseRecord) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cases (id,tenant_id,name,status,pack_id,"
                "pack_snapshot_at,current_version,created_at,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (case.id, case.tenant_id, case.name, case.status, case.pack_id,
                 case.pack_snapshot_at, case.current_version,
                 case.created_at or _now(), case.created_by))
        finally:
            conn.close()

    def get_case(self, case_id: str) -> CaseRecord | None:
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM cases WHERE id=?",
                             (case_id,)).fetchone()
            return self._case(r) if r else None
        finally:
            conn.close()

    def list_cases(self, tenant_id: str | None = None) -> list[CaseRecord]:
        conn = self._connect()
        try:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT * FROM cases ORDER BY created_at DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cases WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,)).fetchall()
            return [self._case(r) for r in rows]
        finally:
            conn.close()

    def transition_case(self, case_id: str, target_status: str,
                        by: str) -> CaseRecord:
        if target_status not in CASE_STATUSES:
            raise IllegalTransition(f"未知案件状态：{target_status!r}")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT * FROM cases WHERE id=?",
                             (case_id,)).fetchone()
            if r is None:
                raise FileNotFoundError(f"案件不存在：{case_id}")
            current = self._case(r)
            assert_case_transition(current.status, target_status)
            conn.execute("UPDATE cases SET status=? WHERE id=?",
                         (target_status, case_id))
            conn.execute("COMMIT")
            updated = self.get_case(case_id)
            assert updated is not None
            return updated
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def set_case_pack(self, case_id: str, pack_id: str, snapshot_at: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE cases SET pack_id=?, pack_snapshot_at=? WHERE id=?",
                (pack_id, snapshot_at, case_id))
        finally:
            conn.close()

    # ---- pack 快照 ----
    def create_pack_snapshot(self, snap: PackSnapshot) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO case_pack_snapshots (case_id,pack_id,version,"
                "snapshot_path,locked_at) VALUES (?,?,?,?,?)",
                (snap.case_id, snap.pack_id, snap.version, snap.snapshot_path,
                 snap.locked_at or _now()))
        finally:
            conn.close()

    def get_pack_snapshot(self, case_id: str) -> PackSnapshot | None:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM case_pack_snapshots WHERE case_id=?",
                (case_id,)).fetchone()
            return self._snap(r) if r else None
        finally:
            conn.close()

    # ---- 任务 ----
    def create_task(self, task: TaskRow) -> TaskRow:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 幂等：同 (case_id, task_type, idem_key) 已有任务则返回既有行
            if task.idem_key:
                r = conn.execute(
                    "SELECT * FROM tasks WHERE case_id=? AND task_type=? "
                    "AND idem_key=?",
                    (task.case_id, task.task_type, task.idem_key)).fetchone()
                if r is not None:
                    conn.execute("COMMIT")
                    return self._task(r)
            now = _now()
            conn.execute(
                "INSERT INTO tasks (id,case_id,task_type,params_json,status,"
                "progress_pct,progress_stage,progress_label,progress_detail,"
                "retry_count,max_retries,idem_key,created_at,updated_at,"
                "started_at,finished_at,error_code,error_message,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.id, task.case_id, task.task_type,
                 json.dumps(task.params, ensure_ascii=False),
                 task.status, task.progress_pct, task.progress_stage,
                 task.progress_label, task.progress_detail, task.retry_count,
                 task.max_retries, task.idem_key or None,
                 task.created_at or now, task.updated_at or now,
                 task.started_at, task.finished_at, task.error_code,
                 task.error_message, task.created_by))
            conn.execute("COMMIT")
            got = self.get_task(task.id)
            assert got is not None
            return got
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_task(self, task_id: str) -> TaskRow | None:
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM tasks WHERE id=?",
                             (task_id,)).fetchone()
            return self._task(r) if r else None
        finally:
            conn.close()

    def list_tasks(self, *, case_id: str | None = None,
                   status: str | None = None, limit: int = 100) -> list[TaskRow]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list = []
        if case_id is not None:
            sql += " AND case_id=?"
            args.append(case_id)
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        conn = self._connect()
        try:
            return [self._task(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def list_leaseable(self) -> list[TaskRow]:
        """有 PENDING 且当前无 RUNNING 的案件优先，同案按创建时间 FIFO。"""
        sql = (
            "SELECT * FROM tasks t WHERE status=? AND NOT EXISTS ("
            "  SELECT 1 FROM tasks r WHERE r.case_id=t.case_id AND r.status=?) "
            "ORDER BY t.created_at ASC")
        conn = self._connect()
        try:
            return [self._task(r) for r in conn.execute(
                sql, (TASK_PENDING, TASK_RUNNING)).fetchall()]
        finally:
            conn.close()

    def claim_task(self, task_id: str) -> bool:
        """原子认领：PENDING→RUNNING，且同案件无其他 RUNNING（案件级串行
        在此强制——list_leaseable 与认领是 check-then-act，
        多 Worker 并发时必须以这条 SQL 为准，否则同案两任务会被同时认领）。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE tasks SET status=?, started_at=?, updated_at=? "
                "WHERE id=? AND status=? AND NOT EXISTS ("
                "  SELECT 1 FROM tasks r WHERE r.case_id=tasks.case_id "
                "    AND r.status=? AND r.id<>tasks.id)",
                (TASK_RUNNING, _now(), _now(), task_id, TASK_PENDING,
                 TASK_RUNNING))
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def update_progress(self, task_id: str, *, pct: float, stage: str,
                        stage_label: str, detail: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET progress_pct=?, progress_stage=?, "
                "progress_label=?, progress_detail=?, updated_at=? WHERE id=?",
                (pct, stage, stage_label, detail, _now(), task_id))
        finally:
            conn.close()

    def complete_task(self, task_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status=?, progress_pct=100, finished_at=?, "
                "updated_at=? WHERE id=?",
                (TASK_SUCCEEDED, _now(), _now(), task_id))
        finally:
            conn.close()

    def fail_task(self, task_id: str, *, error_code: str,
                  error_message: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status=?, error_code=?, error_message=?, "
                "finished_at=?, updated_at=? WHERE id=?",
                (TASK_FAILED, error_code, error_message, _now(), _now(),
                 task_id))
        finally:
            conn.close()

    def requeue_task(self, task_id: str, retry_count: int) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status=?, retry_count=?, started_at='', "
                "error_code='', error_message='', updated_at=? WHERE id=?",
                (TASK_PENDING, retry_count, _now(), task_id))
        finally:
            conn.close()

    # ---- 版本指针 ----
    def current_version(self, case_id: str) -> int:
        conn = self._connect()
        try:
            r = conn.execute("SELECT version FROM case_version WHERE case_id=?",
                             (case_id,)).fetchone()
            return int(r["version"]) if r else 0
        finally:
            conn.close()

    def set_version(self, case_id: str, version: int, by: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO case_version (case_id,version,updated_at,updated_by)"
                " VALUES (?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
                "version=excluded.version, updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by",
                (case_id, version, _now(), by))
            # cases.current_version 冗余同步（列表展示用）
            conn.execute("UPDATE cases SET current_version=? WHERE id=?",
                         (version, case_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- 运维辅助 ----
    def purge_expired_sessions(self, now_iso: str | None = None) -> int:
        """清理已过期/已撤销会话（返回清理条数）。"""
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM meta_sessions WHERE revoked=1 OR expires_at<=?",
                (now_iso or _now(),))
            return cur.rowcount
        finally:
            conn.close()
