"""
tests/test_writeback.py
REQ-013 Writeback Adapter + 幂等 + 回执 测试。

覆盖 AC1-AC5：
  AC1: dry_run() 与 send() 产出 payload 一致
  AC2: 相同 idempotency_key 重复 send → 外部台账只有一条记录
  AC3: 外部返回业务号 → action_request/outbox 置 confirmed
  AC4: HTTP 200 无业务号 → pending_receipt，不置 confirmed
  AC5: adapter 接口不含 conn/store/board 参数（无法反向读本体）
附加：409 冲突失败路径；dispatch→outbox→confirm 端到端。
"""
from __future__ import annotations

import inspect
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
from core.writeback import (                                  # noqa: E402
    StubLedgerAdapter, WritebackDispatcher, WritebackAdapter,
)


class TestWriteback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.adapter = StubLedgerAdapter(self.ledger)
        self.store = Store(db_path=":memory:")
        self.ex = ActionExecutor(self.store)
        self.disp = WritebackDispatcher(self.store.conn, self.adapter)

    def tearDown(self):
        self.store.close()

    def test_ac1_dry_run_matches_send_payload(self):
        """AC1: dry_run 与 send 的 payload 一致。"""
        payload = {"target_status": "查证中", "operator": "王检察官",
                   "amount": 100000.0, "note": "测试*"}
        dry = self.adapter.dry_run(payload)
        self.assertEqual(dry.payload, payload)
        r = self.adapter.send(payload, "wb:ac1")
        self.assertTrue(r.ok and r.external_id)
        saved = [x for x in self.adapter.records
                 if x["idempotency_key"] == "wb:ac1"][0]
        self.assertEqual(saved["payload"], payload)

    def test_ac2_duplicate_send_single_ledger_record(self):
        """AC2: 同键重复 send，外部只产生一条记录。"""
        payload = {"v": 1}
        r1 = self.adapter.send(payload, "wb:dup")
        r2 = self.adapter.send(payload, "wb:dup")
        r3 = self.adapter.send(payload, "wb:dup")
        self.assertEqual(r1.external_id, r2.external_id)
        self.assertEqual(r2.external_id, r3.external_id)
        self.assertEqual(self.adapter.record_count(), 1)
        # 台账持久化后重载仍只一条
        reloaded = StubLedgerAdapter(self.ledger)
        self.assertEqual(reloaded.record_count(), 1)
        self.assertEqual(reloaded.send(payload, "wb:dup").external_id, r1.external_id)

    def test_ac3_business_id_confirms(self):
        """AC3: 拿到业务号 → outbox/action_request 均 confirmed。"""
        # 先有 pending_receipt 的 action_request（dispatch 后的状态）
        self.store.conn.execute(
            "INSERT INTO action_request (action_id, idempotency_key, action_name, "
            "clue_id, target_status, params_json, status, submitted_by, submitted_at) "
            "VALUES ('act_demo1','k','verify','c1','查证中','{}','pending_receipt','王','t')")
        outbox_id = Outbox(self.store.conn).enqueue(
            action_id="act_demo1", action_name="verify", clue_id="c1",
            payload={"target_status": "查证中"}, created_by="王检察官")
        results = self.disp.send_pending()
        self.assertEqual(results[0]["result"], "confirmed")
        ob = self.disp.outbox.get(outbox_id)
        self.assertEqual(ob["status"], "confirmed")
        self.assertTrue(ob["external_id"].startswith("STUB-"))
        row = self.store.conn.execute(
            "SELECT status, writeback_status, external_id FROM action_request "
            "WHERE action_id='act_demo1'").fetchone()
        self.assertEqual(row[0], "confirmed")
        self.assertEqual(row[1], "confirmed")
        self.assertTrue(row[2].startswith("STUB-"))

    def test_ac4_http200_without_business_id_pending(self):
        """AC4: 200 无业务号 → outbox=sent（待回执），action_request 不 confirmed。"""
        class NoBusinessIdAdapter(StubLedgerAdapter):
            def send(self, payload, idempotency_key, **kw):
                return super().send(payload, idempotency_key,
                                    grant_business_id=False)

        self.store.conn.execute(
            "INSERT INTO action_request (action_id, idempotency_key, action_name, "
            "clue_id, target_status, params_json, status, submitted_by, submitted_at) "
            "VALUES ('act_demo2','k2','verify','c2','查证中','{}','pending_receipt','王','t')")
        disp2 = WritebackDispatcher(
            self.store.conn, NoBusinessIdAdapter(Path(self.tmp.name) / "ledger2.json"))
        Outbox(self.store.conn).enqueue(
            action_id="act_demo2", action_name="verify", clue_id="c2",
            payload={"x": 1}, created_by="王")
        results = disp2.send_pending()
        self.assertEqual(results[0]["result"], "pending_receipt")
        ob = disp2.outbox.list_by_status("sent")
        self.assertEqual(len(ob), 1)
        self.assertIsNone(ob[0]["external_id"])
        row = self.store.conn.execute(
            "SELECT status FROM action_request WHERE action_id='act_demo2'").fetchone()
        self.assertEqual(row[0], "pending_receipt")  # 未置 confirmed

    def test_409_conflict_marked_failed(self):
        """409 冲突 → failed（REQ-014 将据此不重试转人工）。"""
        class ConflictAdapter(StubLedgerAdapter):
            def send(self, payload, idempotency_key, **kw):
                return super().send(payload, idempotency_key, conflict=True)

        disp3 = WritebackDispatcher(
            self.store.conn, ConflictAdapter(Path(self.tmp.name) / "ledger3.json"))
        Outbox(self.store.conn).enqueue(
            action_id="act_demo3", action_name="verify", clue_id="c3",
            payload={"x": 1}, created_by="王")
        results = disp3.send_pending()
        self.assertEqual(results[0]["result"], "failed")
        self.assertEqual(results[0]["status_code"], 409)

    def test_ac5_adapter_interface_no_backdoor(self):
        """AC5: adapter 方法签名不含 conn/store/board，无法反向读本体。"""
        for meth in ("dry_run", "send", "fetch_status"):
            params = inspect.signature(getattr(StubLedgerAdapter, meth)).parameters
            self.assertFalse(
                {"conn", "store", "board", "gateway"} & set(params),
                f"adapter.{meth} 不得接收本体访问句柄")
        self.assertIsInstance(self.adapter, WritebackAdapter)

    def test_end_to_end_dispatch_to_confirm(self):
        """两阶段 → outbox → 回执确认 全链路。"""
        clue = LineageClue(title="回写链路线索")
        aid = self.ex.submit("verify", clue, "王检察官")
        self.ex.approve(aid, "李主办")
        result = self.ex.dispatch(aid, clue)
        self.assertEqual(result["status"], "pending_receipt")
        # dispatcher 送出
        sent = self.disp.send_pending()
        self.assertEqual(sent[0]["result"], "confirmed")
        req = self.ex.request_status(aid)
        self.assertEqual(req["status"], "confirmed")
        self.assertTrue(req["external_id"].startswith("STUB-"))
        self.assertEqual(clue.status, "查证中")


if __name__ == "__main__":
    unittest.main()
