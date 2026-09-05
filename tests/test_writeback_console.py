"""
tests/test_writeback_console.py
REQ-043 真实 Writeback Adapter —— Console 适配器验证测试。

覆盖 AC1–AC5：
  AC1: dry_run() 与 send() payload 一致
  AC2: 幂等键在真实系统生效（重复 send 只产生一条记录）
  AC3: 回执含唯一业务号、版本、终态
  AC4: 涉密字段不出受控环境（控制台输出脱敏）
  AC5: 对账差异可检出（fetch_status 查无此单 → unknown）
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.action_executor import ActionExecutor               # noqa: E402
from core.registry import LineageClue                         # noqa: E402
from core.outbox import Outbox                                # noqa: E402
from core.writeback import WritebackDispatcher, WritebackAdapter  # noqa: E402
from core.reconcile import Reconciler                         # noqa: E402
from writeback.adapters.console_adapter import ConsoleAdapter  # noqa: E402


class TestConsoleAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "console_ledger.json"
        self.capture = io.StringIO()
        self.adapter = ConsoleAdapter(
            self.ledger, stream=self.capture)
        self.store = Store(db_path=":memory:")
        self.disp = WritebackDispatcher(self.store.conn, self.adapter)

    def tearDown(self):
        self.store.close()

    def test_ac1_dry_run_matches_send_payload(self):
        """AC1: dry_run 与 send 的 payload 一致。"""
        payload = {"target_status": "查证中", "operator": "王检察官",
                   "amount": 100000.0}
        dry = self.adapter.dry_run(payload)
        self.assertEqual(dry.payload, payload)
        r = self.adapter.send(payload, "wb:ac1")
        self.assertTrue(r.ok and r.external_id)

    def test_ac2_idempotency_single_record(self):
        """AC2: 同幂等键重复 send，控制台只输出一次，台账只有一条。"""
        payload = {"v": 1, "note": "测试"}
        r1 = self.adapter.send(payload, "wb:dup")
        r2 = self.adapter.send(payload, "wb:dup")
        r3 = self.adapter.send(payload, "wb:dup")
        self.assertEqual(r1.external_id, r2.external_id)
        self.assertEqual(r2.external_id, r3.external_id)
        self.assertEqual(self.adapter.record_count(), 1)
        # 重启后仍只一条
        reloaded = ConsoleAdapter(self.ledger, stream=io.StringIO())
        self.assertEqual(reloaded.record_count(), 1)
        self.assertEqual(
            reloaded.send(payload, "wb:dup").external_id, r1.external_id)

    def test_ac3_receipt_has_business_id_and_state(self):
        """AC3: 回执含唯一业务号，fetch_status 返回终态 confirmed。"""
        payload = {"target_status": "已查证"}
        r = self.adapter.send(payload, "wb:ac3")
        self.assertTrue(r.ok)
        self.assertTrue(r.external_id.startswith("CON-"))
        st = self.adapter.fetch_status(r.external_id)
        self.assertEqual(st.state, "confirmed")
        self.assertEqual(st.external_id, r.external_id)

    def test_ac4_sensitive_fields_masked_in_console(self):
        """AC4: 涉密字段（手机号/身份证/银行卡）在控制台输出中脱敏。"""
        payload = {
            "phone": "13812345678",
            "id_card": "110101199001011234",
            "bank_card": "6222021234567890123",
        }
        self.adapter.send(payload, "wb:ac4")
        output = self.capture.getvalue()
        self.assertNotIn("13812345678", output)       # 手机号不出现
        self.assertNotIn("110101199001011234", output)  # 身份证不出现
        self.assertNotIn("6222021234567890123", output)  # 银行卡不出现
        self.assertIn("138****5678", output)            # 脱敏后出现
        # 真实 payload 不受影响
        record = self.adapter._records[-1]
        self.assertEqual(record["payload"]["phone"], "13812345678")

    def test_ac5_reconciliation_discrepancy_detected(self):
        """AC5: 外部查无此单 → unknown 状态，对账差异可检出。"""
        st = self.adapter.fetch_status("CON-999999")
        self.assertEqual(st.state, "unknown")
        # 构造差异场景：本地 confirmed 但外部无此记录
        from core.outbox import Outbox
        outbox = Outbox(self.store.conn)
        outbox.enqueue(
            action_id="act_fake", action_name="verify",
            clue_id="c_fake",
            payload={"x": 1}, created_by="test")
        # 手动标记 confirmed 用一个不存在的 external_id
        queued = outbox.list_by_status("queued")
        outbox.mark_confirmed(queued[0]["outbox_id"], "CON-999999")
        reconciler = Reconciler(self.store.conn, self.adapter)
        report = reconciler.run_reconcile()
        self.assertTrue(len(report["discrepancies"]) > 0)
        self.assertEqual(report["discrepancies"][0]["external_state"], "unknown")

    def test_protocol_conformance(self):
        """ConsoleAdapter 符合 WritebackAdapter 协议。"""
        self.assertIsInstance(self.adapter, WritebackAdapter)

    def test_end_to_end_dispatch_to_confirm(self):
        """全链路：submit → approve → dispatch → ConsoleAdapter → confirmed。"""
        clue = LineageClue(title="REQ-043 端到端验证线索")
        ex = ActionExecutor(self.store)
        aid = ex.submit("verify", clue, "王检察官")
        ex.approve(aid, "李主办")
        result = ex.dispatch(aid, clue)
        self.assertEqual(result["status"], "pending_receipt")
        sent = self.disp.send_pending()
        self.assertEqual(sent[0]["result"], "confirmed")
        req = ex.request_status(aid)
        self.assertEqual(req["status"], "confirmed")
        self.assertTrue(req["external_id"].startswith("CON-"))
        # 控制台有输出
        self.assertIn("CONSOLE ADAPTER", self.capture.getvalue())


if __name__ == "__main__":
    unittest.main()
