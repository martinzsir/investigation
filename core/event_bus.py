"""
core/event_bus.py
事件总线与持久化事件日志（REQ-006）。

轻量事件总线（DuckDB 表 event_log），支持发布/订阅/重放/幂等消费。
不引入 Kafka；进程重启后仍可读取。

事件类型清单（本批实现的最小集标 *）：
  source.partition.arrived      partition.quarantined*
  ontology.stale                ontology.materialized*
  finding.created               clue.status_changed*
  review.decided                review.deferred
  action.submitted              action.approved          action.dispatched
  writeback.sent                writeback.failed         writeback.confirmed
  external.status_changed

幂等：同一 idempotency_key 重复投递，handler 只执行一次。
异常：handler 抛异常 → 写 dead_letter，不丢事件，不影响其他订阅者。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Any


# 完整事件类型清单（publish 不限定于此，但供文档与校验）
EVENT_TYPES = {
    "source.partition.arrived", "partition.quarantined",
    "ontology.stale", "ontology.materialized",
    "finding.created", "clue.status_changed",
    "review.decided", "review.deferred",
    "action.submitted", "action.approved", "action.dispatched",
    "writeback.sent", "writeback.failed", "writeback.confirmed",
    "external.status_changed",
}


@dataclass(frozen=True)
class Event:
    """不可变事件。"""
    event_id: str
    type: str
    occurred_at: str
    causation_id: str | None
    actor: str
    payload: dict
    payload_hash: str
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id, "type": self.type,
            "occurred_at": self.occurred_at, "causation_id": self.causation_id,
            "actor": self.actor, "payload": self.payload,
            "payload_hash": self.payload_hash, "schema_version": self.schema_version,
        }


_DDL = [
    """CREATE TABLE IF NOT EXISTS event_log (
        seq BIGINT NOT NULL,
        event_id VARCHAR PRIMARY KEY,
        type VARCHAR NOT NULL,
        occurred_at VARCHAR NOT NULL,
        causation_id VARCHAR,
        actor VARCHAR NOT NULL,
        payload VARCHAR,
        payload_hash VARCHAR NOT NULL,
        schema_version INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS event_dead_letter (
        dead_id VARCHAR PRIMARY KEY,
        event_id VARCHAR NOT NULL,
        handler_name VARCHAR NOT NULL,
        error TEXT,
        failed_at VARCHAR NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS event_idempotency (
        idempotency_key VARCHAR PRIMARY KEY,
        event_id VARCHAR NOT NULL,
        handler_name VARCHAR NOT NULL,
        processed_at VARCHAR NOT NULL
    )""",
]


class EventBus:
    """持久化事件总线。"""

    def __init__(self, conn):
        self._conn = conn
        self._subscribers: dict[str, list[tuple[Callable, Callable]]] = {}
        # type -> [(handler, idempotency_key_fn)]
        for ddl in _DDL:
            conn.execute(ddl)
        self._seq = self._next_seq()

    def _next_seq(self) -> int:
        """获取下一个序列号（基于当前 max(seq)+1）。"""
        try:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM event_log").fetchone()
            return row[0]
        except Exception:
            return 1

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, type: str, payload: dict, *, actor: str,
                causation_id: str | None = None) -> str:
        """落盘 + 通知订阅者；handler 异常进 dead_letter，不丢事件。"""
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        event = Event(
            event_id=uuid.uuid4().hex,
            type=type,
            occurred_at=datetime.now().isoformat(timespec="seconds"),
            causation_id=causation_id,
            actor=actor,
            payload=payload,
            payload_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        )
        # 落盘（event_id 唯一，重复 INSERT 被忽略）
        seq = self._seq
        self._seq += 1
        self._conn.execute(
            """INSERT OR IGNORE INTO event_log
               (seq, event_id, type, occurred_at, causation_id, actor, payload,
                payload_hash, schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [seq, event.event_id, event.type, event.occurred_at, event.causation_id,
             event.actor, payload_json, event.payload_hash, event.schema_version])
        # 通知订阅者
        for handler, key_fn in self._subscribers.get(type, []):
            self._dispatch(event, handler, key_fn)
        return event.event_id

    def _dispatch(self, event: Event, handler: Callable, key_fn: Callable) -> None:
        """派发事件给单个订阅者，带幂等与死信。"""
        key = key_fn(event)
        # 幂等检查
        row = self._conn.execute(
            "SELECT 1 FROM event_idempotency WHERE idempotency_key=? AND handler_name=?",
            [key, handler.__name__]
        ).fetchone()
        if row:
            return  # 已处理过
        try:
            handler(event)
            self._conn.execute(
                """INSERT OR IGNORE INTO event_idempotency
                   (idempotency_key, event_id, handler_name, processed_at)
                   VALUES (?, ?, ?, ?)""",
                [key, event.event_id, handler.__name__,
                 datetime.now().isoformat(timespec="seconds")])
        except Exception as e:
            self._conn.execute(
                """INSERT OR IGNORE INTO event_dead_letter
                   (dead_id, event_id, handler_name, error, failed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [uuid.uuid4().hex, event.event_id, handler.__name__,
                 str(e), datetime.now().isoformat(timespec="seconds")])

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(self, type: str, handler: Callable[[Event], None],
                  *, idempotency_key_fn: Callable[[Event], str]) -> None:
        """注册订阅；同 type 多次订阅追加。"""
        self._subscribers.setdefault(type, []).append((handler, idempotency_key_fn))

    # ------------------------------------------------------------------
    # 重放
    # ------------------------------------------------------------------
    def replay(self, from_event_id: str | None = None) -> int:
        """按 seq 顺序重放，返回已重放条数。

        重放时给每个事件新 idempotency_key（前缀 replay-），确保 handler 再次执行。
        """
        if from_event_id:
            rows = self._conn.execute(
                """SELECT event_id, type, occurred_at, causation_id, actor,
                          payload, payload_hash, schema_version
                   FROM event_log
                   WHERE seq > (SELECT seq FROM event_log WHERE event_id=?)
                   ORDER BY seq""",
                [from_event_id]
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT event_id, type, occurred_at, causation_id, actor,
                          payload, payload_hash, schema_version
                   FROM event_log ORDER BY seq"""
            ).fetchall()
        count = 0
        for r in rows:
            event = Event(
                event_id=r[0], type=r[1], occurred_at=r[2], causation_id=r[3],
                actor=r[4], payload=json.loads(r[5]) if r[5] else {},
                payload_hash=r[6], schema_version=r[7])
            for handler, key_fn in self._subscribers.get(event.type, []):
                # 重放用 replay- 前缀避免与首次冲突
                replay_key = f"replay-{key_fn(event)}"
                # 检查是否已重放过
                seen = self._conn.execute(
                    "SELECT 1 FROM event_idempotency WHERE idempotency_key=?",
                    [replay_key]
                ).fetchone()
                if seen:
                    continue
                self._dispatch_with_key(event, handler, replay_key)
            count += 1
        return count

    def _dispatch_with_key(self, event: Event, handler: Callable, key: str) -> None:
        """带指定 key 派发（重放用）。"""
        try:
            handler(event)
            self._conn.execute(
                """INSERT OR IGNORE INTO event_idempotency
                   (idempotency_key, event_id, handler_name, processed_at)
                   VALUES (?, ?, ?, ?)""",
                [key, event.event_id, handler.__name__,
                 datetime.now().isoformat(timespec="seconds")])
        except Exception as e:
            self._conn.execute(
                """INSERT OR IGNORE INTO event_dead_letter
                   (dead_id, event_id, handler_name, error, failed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [uuid.uuid4().hex, event.event_id, handler.__name__,
                 str(e), datetime.now().isoformat(timespec="seconds")])

    # ------------------------------------------------------------------
    # 循环检测
    # ------------------------------------------------------------------
    def detect_cycle(self, event_id: str, max_depth: int = 10) -> bool:
        """沿 causation_id 回溯，深度 > max_depth 视为循环。"""
        depth = 0
        current = event_id
        while current and depth <= max_depth:
            row = self._conn.execute(
                "SELECT causation_id FROM event_log WHERE event_id=?",
                [current]
            ).fetchone()
            if not row or not row[0]:
                return False  # 到达链首，无循环
            current = row[0]
            depth += 1
        return depth > max_depth

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_events(self, type: str | None = None, limit: int = 100) -> list[dict]:
        """查询事件（调试用）。"""
        if type:
            rows = self._conn.execute(
                "SELECT * FROM event_log WHERE type=? ORDER BY occurred_at DESC LIMIT ?",
                [type, limit]
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM event_log ORDER BY occurred_at DESC LIMIT ?",
                [limit]
            ).fetchall()
        cols = [d[0] for d in self._conn.description]
        return [dict(zip(cols, r)) for r in rows]
