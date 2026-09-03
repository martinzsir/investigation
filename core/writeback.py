"""
core/writeback.py
Writeback Adapter：本地本体 → 外部业务系统的回写抽象（REQ-013）。

设计要点：
  - adapter 只接收 Action payload（dict），**拿不到 conn/store/board**——
    接口层面保证无法反向读取未授权本体属性（AC5）；
  - 外部"成功"的定义是**业务系统返回唯一业务号**，不是 HTTP 200（AC4）：
    200 无业务号 → pending_receipt，绝不置 confirmed；
  - 幂等键 f"wb:{action_id}" 稳定不变：重复 send() 外部台账只产生一条记录（AC2）；
  - dry_run() 与 send() 产出的 payload 必须逐字节一致（AC1）。

P0 用 StubLedgerAdapter（本地 JSON 台账）；真实系统接 WritebackAdapter 协议即可。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DryRunResult:
    """dry-run 结果：将要发送的 payload（与真实 send 一致）。"""
    payload: dict


@dataclass(frozen=True)
class Receipt:
    """外部回执。external_id=None 表示未拿到业务号（不算成功）。"""
    ok: bool
    status_code: int
    external_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ExternalStatus:
    external_id: str
    state: str           # confirmed | pending | unknown
    raw: dict = field(default_factory=dict)


@runtime_checkable
class WritebackAdapter(Protocol):
    """回写适配器协议。方法签名不得出现 conn/store/board（AC5 接口断言）。"""

    def dry_run(self, payload: dict) -> DryRunResult: ...
    def send(self, payload: dict, idempotency_key: str) -> Receipt: ...
    def fetch_status(self, external_id: str) -> ExternalStatus: ...


class StubLedgerAdapter:
    """本地 JSON 台账适配器（P0）：模拟外部业务系统。

    send() 支持故障注入（测试用）：
      fail=True          → 500 失败（可重试）
      conflict=True      → 409 冲突（不可重试，转人工）
      grant_business_id=False → 200 但不返回业务号（AC4 场景）
    """

    def __init__(self, ledger_path: str | Path = "data/writeback_ledger.json"):
        self.path = Path(ledger_path)
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self.records = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.records = []

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=1),
            encoding="utf-8")

    # ---- 协议实现 ----
    def dry_run(self, payload: dict) -> DryRunResult:
        return DryRunResult(payload=json.loads(json.dumps(payload, default=str)))

    def send(self, payload: dict, idempotency_key: str, *,
             fail: bool = False, conflict: bool = False,
             grant_business_id: bool = True) -> Receipt:
        # 幂等：同键已受理 → 返回原回执，外部不产生第二条记录（AC2）
        for r in self.records:
            if r["idempotency_key"] == idempotency_key:
                return Receipt(ok=True, status_code=200,
                               external_id=r["business_id"])
        if conflict:
            return Receipt(ok=False, status_code=409, error="外部台账冲突（409）")
        if fail:
            return Receipt(ok=False, status_code=500, error="外部系统超时（500）")
        if not grant_business_id:
            # HTTP 200 但无业务号 → 不算成功（AC4）
            return Receipt(ok=True, status_code=200, external_id=None)
        business_id = f"STUB-{len(self.records) + 1:06d}"
        record = {
            "idempotency_key": idempotency_key,
            "business_id": business_id,
            "payload": json.loads(json.dumps(payload, default=str)),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.records.append(record)
        self._flush()
        return Receipt(ok=True, status_code=200, external_id=business_id)

    def fetch_status(self, external_id: str) -> ExternalStatus:
        for r in self.records:
            if r["business_id"] == external_id:
                return ExternalStatus(external_id=external_id,
                                      state="confirmed", raw=r)
        return ExternalStatus(external_id=external_id, state="unknown")

    def record_count(self) -> int:
        return len(self.records)


class WritebackDispatcher:
    """把 outbox 中 queued 记录经 adapter 送出，并回写状态。"""

    def __init__(self, conn, adapter: WritebackAdapter):
        from core.outbox import Outbox
        self._conn = conn
        self.outbox = Outbox(conn)
        self.adapter = adapter

    def _update_action_request(self, set_clause: str, params: list) -> None:
        """action_request 由 ActionExecutor 建表；未建时（独立使用 outbox）跳过。"""
        exists = self._conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name='action_request'").fetchone()[0]
        if exists:
            self._conn.execute(f"UPDATE action_request {set_clause}", params)

    def send_pending(self) -> list[dict]:
        """发送全部 queued 记录，返回逐条结果摘要。"""
        results = []
        for row in self.outbox.list_by_status("queued"):
            results.append(self._send_one(row))
        return results

    def _send_one(self, row: dict) -> dict:
        outbox_id = row["outbox_id"]
        action_id = row["action_id"]
        payload = row["payload"]
        key = row["idempotency_key"]

        # AC1：dry_run 与真实发送 payload 一致
        dry = self.adapter.dry_run(payload)
        if dry.payload != payload:
            self.outbox.mark_failed(outbox_id, "dry_run 与 send payload 不一致")
            return {"outbox_id": outbox_id, "result": "payload_mismatch"}

        receipt = self.adapter.send(payload, key)
        if receipt.external_id:
            # 拿到业务号 → 双写确认（AC3）
            self.outbox.mark_confirmed(outbox_id, receipt.external_id)
            self._update_action_request(
                "SET status='confirmed', external_id=?, writeback_status='confirmed' "
                "WHERE action_id=?", [receipt.external_id, action_id])
            self._publish("writeback.confirmed",
                          {"action_id": action_id, "external_id": receipt.external_id})
            return {"outbox_id": outbox_id, "result": "confirmed",
                    "external_id": receipt.external_id}
        if receipt.ok:
            # 200 无业务号 → 停留待回执（AC4）
            self.outbox.mark_sent(outbox_id)
            self._publish("writeback.sent", {"action_id": action_id})
            return {"outbox_id": outbox_id, "result": "pending_receipt"}
        # 失败
        self.outbox.mark_failed(outbox_id, receipt.error or "unknown")
        self._publish("writeback.failed",
                      {"action_id": action_id, "status_code": receipt.status_code,
                       "error": receipt.error})
        return {"outbox_id": outbox_id, "result": "failed",
                "status_code": receipt.status_code, "error": receipt.error}

    def _publish(self, event_type: str, payload: dict) -> None:
        try:
            from core.event_bus import EventBus
            EventBus(self._conn).publish(event_type, payload, actor="writeback")
        except Exception:
            pass
