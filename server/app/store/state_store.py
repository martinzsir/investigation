"""
server/app/store/state_store.py
D1 骨架（M2，决策 D-M2-2）：per-case state.sqlite 业务写承载层。

定位（M2_plan 附录 A）：
  - 业务面短频写（处置/人审裁决/审计链追加）由 per-case state.sqlite（WAL）
    承载，不随分析版本复制 GB 级 DuckDB（ADR 2.7 缺口修正）；
  - M3 接线方式：AuditChain 构造参数扩可选 backend（duckdb 缺省/sqlite），
    ActionExecutor 由 server 注入；本地 CLI/MCP 不传 backend 行为零变化；
  - **M2 不接任何写路径**：本模块只被 store/ 与 tests/ 引用（grep 门禁
    tests/test_state_store.py 硬断言），接线随 M3 处置端点同批。

schema 纪律：
  - audit_chain 与 DuckDB 版**逐列同构**（seq/event_id/.../signature），
    签名算法复用 core.audit._compute_signature——迁移后 chain_verify 逐条可验；
  - action_request / clue_disposal_status 与 core 同构；review_decision 为
    M3 人审裁决预留（接线时若需调整以 actions.json/处置端点为准）；
  - BUILD 任务不触碰 state.sqlite（决策 D1 原文：决策不随分析版本重建丢失）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from core.audit import _compute_signature

_GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_chain (
    seq               INTEGER NOT NULL,
    event_id          TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL,
    ontology_version  TEXT NOT NULL,
    rule_version      TEXT,
    function_version  TEXT,
    params_hash       TEXT,
    source_row_ids    TEXT,
    operator          TEXT NOT NULL,
    before_state      TEXT,
    after_state       TEXT,
    prev_hash         TEXT NOT NULL,
    signature         TEXT NOT NULL,
    occurred_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ac_seq ON audit_chain(seq);
CREATE TABLE IF NOT EXISTS action_request (
    action_id       TEXT PRIMARY KEY,
    idempotency_key TEXT,
    action_name     TEXT NOT NULL,
    clue_id         TEXT,
    target_status   TEXT,
    params_json     TEXT,
    status          TEXT NOT NULL,
    submitted_by    TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    approved_by     TEXT,
    approved_at     TEXT,
    dispatched_at   TEXT,
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    external_id     TEXT,
    writeback_status TEXT
);
CREATE TABLE IF NOT EXISTS clue_disposal_status (
    clue_id    TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    note       TEXT DEFAULT '',
    operator   TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS review_decision (
    decision_id TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT '',
    target_id   TEXT,
    verdict     TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}'
);
"""

_AC_COLUMNS = (
    "seq, event_id, case_id, ontology_version, rule_version, function_version, "
    "params_hash, source_row_ids, operator, before_state, after_state, "
    "prev_hash, signature, occurred_at")


def _as_duckdb_conn(source):
    """接受 CaseStore（.read_conn）/ 裸 duckdb 连接。"""
    conn = getattr(source, "read_conn", None)
    if conn is not None:
        return conn
    return getattr(source, "conn", source)


class StateStore:
    """per-case 业务状态库（WAL；M2 骨架：迁移演练 + 只读校验面）。"""

    def __init__(self, case_id: str, path: str | Path):
        self.case_id = case_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=5,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    @property
    def conn(self):
        """sqlite3 写连接（StateSink 暴露给 core ActionExecutor/AuditChain）。"""
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ---- 写面（M3 D1 接线；core 经 StateSink 窄协议调用）----
    def insert_decision(self, *, kind: str, target_id: str | None,
                        verdict: str, decided_by: str,
                        payload: dict) -> dict:
        """file 动作副作用：决策落 state.review_decision（不写版本文件语义表）。"""
        import json as _json
        import time as _time
        import uuid as _uuid
        decision_id = f"decision_{_uuid.uuid4().hex[:12]}"
        decided_at = _time.strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO review_decision "
            "(decision_id, kind, target_id, verdict, decided_by, decided_at, "
            " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [decision_id, kind, target_id, verdict, decided_by, decided_at,
             _json.dumps(payload, ensure_ascii=False, default=str)])
        self._conn.commit()
        return {"decision_id": decision_id, "persisted": True,
                "created_at": decided_at}

    def status_map(self) -> dict[str, dict]:
        """处置状态真值只读面（clue_id → {status,note,operator,updated_at}）。"""
        rows = self._conn.execute(
            "SELECT clue_id, status, note, operator, updated_at "
            "FROM clue_disposal_status").fetchall()
        return {r["clue_id"]: {"status": r["status"], "note": r["note"],
                               "operator": r["operator"],
                               "updated_at": r["updated_at"]}
                for r in rows}

    def list_decisions(self, target_id: str | None = None) -> list[dict]:
        """决策只读列表（审计/详情面用）。"""
        import json as _json
        if target_id is not None:
            rows = self._conn.execute(
                "SELECT decision_id, kind, target_id, verdict, decided_by, "
                "decided_at, payload_json FROM review_decision "
                "WHERE target_id=? ORDER BY decided_at",
                [target_id]).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT decision_id, kind, target_id, verdict, decided_by, "
                "decided_at, payload_json FROM review_decision "
                "ORDER BY decided_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = _json.loads(d.pop("payload_json") or "{}")
            except _json.JSONDecodeError:
                d["payload"] = {}
            out.append(d)
        return out

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 只读校验面（迁移断言用）----
    def event_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM audit_chain").fetchone()
        return int(row[0]) if row else 0

    def root_hash(self) -> str:
        """末条 signature 作为根哈希（与 DuckDB AuditChain.root_hash 同语义）。"""
        row = self._conn.execute(
            "SELECT signature FROM audit_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else _GENESIS_HASH

    def chain_verify(self) -> bool:
        """整链校验：prev_hash 衔接 + signature 重算（复用 core 签名算法）。"""
        rows = self._conn.execute(
            f"SELECT {_AC_COLUMNS} FROM audit_chain ORDER BY seq").fetchall()
        import json as _json
        prev = _GENESIS_HASH
        for r in rows:
            if r["prev_hash"] != prev:
                return False
            try:
                source_row_ids = (_json.loads(r["source_row_ids"])
                                  if r["source_row_ids"] else [])
            except _json.JSONDecodeError:
                source_row_ids = []
            before = _json.loads(r["before_state"]) if r["before_state"] else None
            after = _json.loads(r["after_state"]) if r["after_state"] else None
            recomputed = _compute_signature(
                r["event_id"], r["case_id"], r["ontology_version"],
                r["rule_version"], r["function_version"], r["params_hash"],
                source_row_ids, r["operator"], before, after, r["prev_hash"])
            if recomputed != r["signature"]:
                return False
            prev = r["signature"]
        return True

    # ---- 迁移演练（幂等；M3 上线后按案件一次性执行，附录 A）----
    def import_from_duckdb(self, source) -> dict:
        """读案件 DuckDB audit_chain 全量行 → 写 state.sqlite（幂等）。

        source：CaseStore（读模式）或裸 duckdb 连接。event_id 冲突
        INSERT OR REPLACE（重跑行数不变）；返回行数/根哈希比对结果。
        """
        conn = _as_duckdb_conn(source)
        try:
            rows = conn.execute(
                f"SELECT {_AC_COLUMNS} FROM audit_chain ORDER BY seq"
            ).fetchall()
        except Exception as e:
            raise RuntimeError(f"读取 DuckDB audit_chain 失败：{e}") from e
        # duckdb fetchall 返回原生元组：index 12 = signature
        duckdb_root = rows[-1][12] if rows else _GENESIS_HASH
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO audit_chain ({_AC_COLUMNS}) "
                    f"VALUES ({','.join('?' * 14)})", tuple(r))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {
            "duckdb_count": len(rows),
            "state_count": self.event_count(),
            "duckdb_root_hash": duckdb_root,
            "state_root_hash": self.root_hash(),
            "counts_match": len(rows) == self.event_count(),
            "root_match": duckdb_root == self.root_hash(),
        }
