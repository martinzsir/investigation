"""
tests/test_reconcile.py
REQ-014 回写对账 / 重试 / 死信 测试。

覆盖：
  - 指数退避：刚失败不重试，退避到期才重试
  - 409 冲突不重试、转人工
  - 5 次失败 → dead_letter + writeback.dead_letter 事件 + action_request 同步
  - pending_receipt 超时 → 幂等重投成功确认
  - 对账差异只报告不自动覆盖
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.outbox import Outbox                                # noqa: E402
from core.writeback import (                                  # noqa: E402
    StubLedgerAdapter, WritebackDispatcher, Receipt)
from core.reconcile import Reconciler                         # noqa: E402


def _enqueue_failed(store, action_id="a1", error="外部系统超时（500）",
                    attempts=1, last=None):
    """造一条 failed outbox 记录。"""
    from core.action_executor import ActionExecutor
    ActionExecutor(store)  # 确保 action_request 表存在
    ob = Outbox(store.conn)
    ob.enqueue(action_id=action_id, action_name="verify", clue_id="c",
               payload={"x": 1}, created_by="王")
    oid = store.conn.execute(
        "SELECT outbox_id FROM writeback_outbox WHERE action_id=?",
        [action_id]).fetchone()[0]
    store.conn.execute(
        "UPDATE writeback_outbox SET status='failed', attempts=?, last_error=?, "
        "last_attempt_at=? WHERE outbox_id=?",
        [attempts, error, last or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), oid])
    store.conn.execute(
        "INSERT INTO action_request (action_id, idempotency_key, action_name, "
        "clue_id, target_status, params_json, status, submitted_by, submitted_at) "
        "VALUES (?,'k','verify','c','查证中','{}','pending_receipt','王','t')",
        [action_id])
    return oid


class FailAdapter(StubLedgerAdapter):
    """永远 500。"""
    def send(self, payload, idempotency_key, **kw):
        return Receipt(ok=False, status_code=500, error="外部系统超时（500）")


class ConflictAdapter(StubLedgerAdapter):
    def send(self, payload, idempotency_key, **kw):
        return Receipt(ok=False, status_code=409, error="外部台账冲突（409）")


class FlakyAdapter(StubLedgerAdapter):
    """前 no_id_times 次返回 200 无业务号，之后正常给业务号。"""
    def __init__(self, path, no_id_times=1):
        super().__init__(path)
        self.no_id_times = no_id_times
        self.calls = 0

    def send(self, payload, idempotency_key, **kw):
        self.calls += 1
        if self.calls <= self.no_id_times:
            return Receipt(ok=True, status_code=200, external_id=None)
        return super().send(payload, idempotency_key)


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.store = Store(db_path=":memory:")

    def tearDown(self):
        self.store.close()

    def test_backoff_not_due_skips_retry(self):
        """刚失败（退避未到）→ 不重试。"""
        _enqueue_failed(self.store, attempts=1)
        r = Reconciler(self.store.conn, FailAdapter(self.ledger)).run_reconcile()
        self.assertEqual(len(r["skipped_backoff"]), 1)
        self.assertEqual(r["retried"], [])
        self.assertEqual(r["dead_lettered"], [])

    def test_backoff_due_retries(self):
        """退避到期 → 重试（再次失败，attempts+1）。"""
        old = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        _enqueue_failed(self.store, attempts=1, last=old)
        r = Reconciler(self.store.conn, FailAdapter(self.ledger)).run_reconcile()
        self.assertEqual(len(r["retried"]), 1)
        row = self.store.conn.execute(
            "SELECT attempts, status FROM writeback_outbox").fetchone()
        self.assertEqual(row[1], "failed")
        self.assertEqual(row[0], 2)

    def test_409_never_retried_manual(self):
        """409 冲突：即使退避到期也不重试，转人工。"""
        old = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        _enqueue_failed(self.store, error="外部台账冲突（409）",
                        attempts=3, last=old)
        r = Reconciler(self.store.conn, ConflictAdapter(self.ledger)).run_reconcile()
        self.assertEqual(len(r["manual_409"]), 1)
        self.assertEqual(r["retried"], [])
        row = self.store.conn.execute(
            "SELECT attempts, status FROM writeback_outbox").fetchone()
        self.assertEqual(row, (3, "failed"))  # 原样不动

    def test_five_failures_dead_letter(self):
        """5 次失败 → dead_letter + 告警事件 + action_request 同步。"""
        old = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        _enqueue_failed(self.store, attempts=5, last=old)
        r = Reconciler(self.store.conn, FailAdapter(self.ledger)).run_reconcile()
        self.assertEqual(len(r["dead_lettered"]), 1)
        self.assertEqual(r["retried"], [])
        ob = self.store.conn.execute(
            "SELECT status FROM writeback_outbox").fetchone()[0]
        ar = self.store.conn.execute(
            "SELECT status, writeback_status FROM action_request").fetchone()
        self.assertEqual(ob, "dead_letter")
        self.assertEqual(ar, ("dead_letter", "dead_letter"))
        evts = [e[0] for e in self.store.conn.execute(
            "SELECT type FROM event_log").fetchall()]
        self.assertIn("writeback.dead_letter", evts)

    def test_pending_receipt_timeout_redelivers(self):
        """pending_receipt 超时 → 幂等重投，外部恢复后确认。"""
        adapter = FlakyAdapter(self.ledger, no_id_times=1)
        # 第一次发送：200 无业务号 → sent
        Outbox(self.store.conn).enqueue(
            action_id="a9", action_name="verify", clue_id="c",
            payload={"x": 1}, created_by="王")
        disp = WritebackDispatcher(self.store.conn, adapter)
        r1 = disp.send_pending()
        self.assertEqual(r1[0]["result"], "pending_receipt")
        # 未超时：等待
        rec = Reconciler(self.store.conn, adapter)
        rep0 = rec.run_reconcile()
        self.assertEqual(len(rep0["pending_waiting"]), 1)
        # 超时后重投 → 外部恢复 → confirmed
        old = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.conn.execute(
            "UPDATE writeback_outbox SET last_attempt_at=? WHERE action_id='a9'",
            [old])
        rep1 = rec.run_reconcile()
        self.assertEqual(len(rep1["retried"]), 1)
        self.assertEqual(rep1["retried"][0]["result"]["result"], "confirmed")
        self.assertEqual(self.store.conn.execute(
            "SELECT status FROM writeback_outbox").fetchone()[0], "confirmed")

    def test_discrepancy_reported_not_overwritten(self):
        """本地 confirmed 但外部查无 → 只报告，本地状态不被覆盖。"""
        Outbox(self.store.conn).enqueue(
            action_id="a8", action_name="verify", clue_id="c",
            payload={"x": 1}, created_by="王")
        # 直接置 confirmed 并给一个外部不存在的业务号
        self.store.conn.execute(
            "UPDATE writeback_outbox SET status='confirmed', external_id='STUB-999999'")
        r = Reconciler(self.store.conn, StubLedgerAdapter(self.ledger)).run_reconcile()
        self.assertEqual(len(r["discrepancies"]), 1)
        self.assertEqual(r["discrepancies"][0]["external_state"], "unknown")
        # 本地状态原样保持
        self.assertEqual(self.store.conn.execute(
            "SELECT status, external_id FROM writeback_outbox").fetchone(),
            ("confirmed", "STUB-999999"))
        # 报告可渲染
        text = Reconciler.render_report(r)
        self.assertIn("对账差异：1 条", text)


if __name__ == "__main__":
    unittest.main()
