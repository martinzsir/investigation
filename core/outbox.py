"""
core/outbox.py
回写发件箱（REQ-013）—— 本地事务与外部回写之间的可靠边界。

dispatch 阶段只把待回写 payload 落 outbox（与本地提交同库同事务边界），
WritebackDispatcher 异步取出经 adapter 发送：
  queued → sent（外部已受理，待回执）→ confirmed（拿到业务号）
                                ↘ failed（可重试，REQ-014 退避/死信）
幂等键稳定为 wb:{action_id}：同一动作重复发送，外部只产生一条记录。
"""
from __future__ import annotations

import json
import time
import uuid

_DDL = """
CREATE TABLE IF NOT EXISTS writeback_outbox (
    outbox_id VARCHAR PRIMARY KEY,
    action_id VARCHAR NOT NULL,
    action_name VARCHAR NOT NULL,
    clue_id VARCHAR,
    payload_json VARCHAR NOT NULL,
    idempotency_key VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    external_id VARCHAR,
    last_error VARCHAR,
    created_by VARCHAR,
    created_at VARCHAR NOT NULL,
    sent_at VARCHAR,
    confirmed_at VARCHAR,
    last_attempt_at VARCHAR
)
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Outbox:
    """回写发件箱（DuckDB 表 writeback_outbox）。"""

    def __init__(self, conn):
        self._conn = conn
        conn.execute(_DDL)
        # 旧库迁移：补 last_attempt_at（退避计时）
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('writeback_outbox')").fetchall()}
        if "last_attempt_at" not in cols:
            conn.execute(
                "ALTER TABLE writeback_outbox ADD COLUMN last_attempt_at VARCHAR")

    def enqueue(self, *, action_id: str, action_name: str, clue_id: str | None,
                payload: dict, created_by: str = "system") -> str:
        """登记一条待回写；同 action_id 重复入队返回既有 outbox_id（幂等）。"""
        key = f"wb:{action_id}"
        row = self._conn.execute(
            "SELECT outbox_id FROM writeback_outbox WHERE idempotency_key=?",
            [key]).fetchone()
        if row:
            return row[0]
        outbox_id = f"ob_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            """INSERT INTO writeback_outbox
               (outbox_id, action_id, action_name, clue_id, payload_json,
                idempotency_key, status, attempts, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)""",
            [outbox_id, action_id, action_name, clue_id,
             json.dumps(payload, ensure_ascii=False, default=str),
             key, created_by, _now()])
        return outbox_id

    def list_by_status(self, status: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT outbox_id, action_id, action_name, clue_id, payload_json, "
            "idempotency_key, status, attempts, external_id, last_error, "
            "created_by, created_at, sent_at, confirmed_at, last_attempt_at "
            "FROM writeback_outbox WHERE status=? ORDER BY created_at",
            [status]).fetchall()
        cols = ["outbox_id", "action_id", "action_name", "clue_id", "payload_json",
                "idempotency_key", "status", "attempts", "external_id", "last_error",
                "created_by", "created_at", "sent_at", "confirmed_at", "last_attempt_at"]
        out = [dict(zip(cols, r)) for r in rows]
        for r in out:
            r["payload"] = json.loads(r.pop("payload_json") or "{}")
        return out

    def list_pending(self) -> list[dict]:
        """待发送/待重试：queued + failed（未死信）。"""
        return self.list_by_status("queued") + self.list_by_status("failed")

    def mark_sent(self, outbox_id: str) -> None:
        self._conn.execute(
            "UPDATE writeback_outbox SET status='sent', attempts=attempts+1, "
            "sent_at=?, last_attempt_at=? WHERE outbox_id=?",
            [_now(), _now(), outbox_id])

    def mark_confirmed(self, outbox_id: str, external_id: str) -> None:
        self._conn.execute(
            "UPDATE writeback_outbox SET status='confirmed', external_id=?, "
            "confirmed_at=? WHERE outbox_id=?", [external_id, _now(), outbox_id])

    def mark_failed(self, outbox_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE writeback_outbox SET status='failed', attempts=attempts+1, "
            "last_error=?, last_attempt_at=? WHERE outbox_id=?",
            [error, _now(), outbox_id])

    def mark_dead_letter(self, outbox_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE writeback_outbox SET status='dead_letter', last_error=?, "
            "last_attempt_at=? WHERE outbox_id=?", [error, _now(), outbox_id])

    def get(self, outbox_id: str) -> dict:
        rows = self._conn.execute(
            "SELECT outbox_id, action_id, action_name, clue_id, payload_json, "
            "idempotency_key, status, attempts, external_id, last_error, "
            "created_by, created_at, sent_at, confirmed_at, last_attempt_at "
            "FROM writeback_outbox WHERE outbox_id=?", [outbox_id]).fetchall()
        if not rows:
            raise KeyError(f"outbox 记录不存在：{outbox_id}")
        cols = ["outbox_id", "action_id", "action_name", "clue_id", "payload_json",
                "idempotency_key", "status", "attempts", "external_id", "last_error",
                "created_by", "created_at", "sent_at", "confirmed_at", "last_attempt_at"]
        rec = dict(zip(cols, rows[0]))
        rec["payload"] = json.loads(rec.pop("payload_json") or "{}")
        return rec
