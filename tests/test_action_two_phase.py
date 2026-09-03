"""
tests/test_action_two_phase.py
REQ-012 Action 两阶段提交 测试。

覆盖 AC1-AC5：
  AC1: submit() 后状态 proposed，不直接执行（线索状态/副作用不变）
  AC2: 未 approve() 就 dispatch() → NotApprovedError
  AC3: approve() 匿名/占位 operator → ValueError
  AC4: file 缺 legal_basis → submit 阶段硬失败（红线保持）
  AC5: 幂等键相同 → 第二次 submit 返回同一 action_id
附加：完整流程 submit→approve→dispatch 生效；submit 校验不通过不登记。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.action_executor import (                            # noqa: E402
    ActionExecutor, NotApprovedError, ActionRequestNotFound,
)
from core.registry import ClueStatusMachine, LineageClue      # noqa: E402


class TestActionTwoPhase(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.ex = ActionExecutor(self.store)
        self.clue = LineageClue(title="两阶段测试线索")

    def tearDown(self):
        self.store.close()

    def test_ac1_submit_is_proposed_not_executed(self):
        """AC1: submit 只登记 proposed，线索状态不变。"""
        aid = self.ex.submit("verify", self.clue, "王检察官")
        self.assertTrue(aid.startswith("act_"))
        req = self.ex.request_status(aid)
        self.assertEqual(req["status"], "proposed")
        # 未执行：线索仍是初始状态
        self.assertEqual(self.clue.status, "待查")

    def test_ac2_dispatch_without_approve_rejected(self):
        """AC2: 未 approve 就 dispatch → NotApprovedError。"""
        aid = self.ex.submit("verify", self.clue, "王检察官")
        with self.assertRaises(NotApprovedError):
            self.ex.dispatch(aid, self.clue)
        # 线索未被改动
        self.assertEqual(self.clue.status, "待查")

    def test_ac3_anonymous_approve_rejected(self):
        """AC3: approve 必须具名 operator。"""
        aid = self.ex.submit("verify", self.clue, "王检察官")
        for bad in ("", "   ", "system", "ai", "assistant"):
            with self.assertRaises(ValueError):
                self.ex.approve(aid, bad)
        # 具名放行
        self.ex.approve(aid, "李主办")
        self.assertEqual(self.ex.request_status(aid)["status"], "approved")

    def test_ac4_file_requires_legal_basis_at_submit(self):
        """AC4: file 缺 legal_basis 在 submit 校验阶段硬失败。"""
        with self.assertRaises(ValueError):
            self.ex.submit("file", self.clue, "王检察官", {})
        # 不合格不登记 action_request
        cnt = self.store.conn.execute(
            "SELECT COUNT(*) FROM action_request").fetchone()[0]
        self.assertEqual(cnt, 0)

    def test_ac5_idempotent_submit_same_action_id(self):
        """AC5: 相同幂等键返回同一 action_id，不重复创建。"""
        a1 = self.ex.submit("verify", self.clue, "王检察官",
                            idempotency_key="key-001")
        a2 = self.ex.submit("verify", self.clue, "王检察官",
                            idempotency_key="key-001")
        self.assertEqual(a1, a2)
        cnt = self.store.conn.execute(
            "SELECT COUNT(*) FROM action_request").fetchone()[0]
        self.assertEqual(cnt, 1)
        # 默认幂等键（同动作同线索同参数）也幂等
        a3 = self.ex.submit("verify", self.clue, "王检察官")
        a4 = self.ex.submit("verify", self.clue, "王检察官")
        self.assertEqual(a3, a4)

    def test_full_flow_submit_approve_dispatch(self):
        """完整两阶段：dispatch 后本地提交生效，请求进入 dispatching/pending_receipt。"""
        aid = self.ex.submit("verify", self.clue, "王检察官", {"note": "人审通过"})
        self.ex.approve(aid, "李主办")
        result = self.ex.dispatch(aid, self.clue)
        self.assertEqual(self.clue.status, "查证中")          # 本地提交生效
        self.assertIn(result["status"], ("dispatching", "pending_receipt"))
        # 事件留痕
        evts = self.store.conn.execute(
            "SELECT type FROM event_log ORDER BY seq").fetchall()
        types = [e[0] for e in evts]
        self.assertIn("action.submitted", types)
        self.assertIn("action.approved", types)
        self.assertIn("action.dispatched", types)

    def test_approve_unknown_action_raises(self):
        with self.assertRaises(ActionRequestNotFound):
            self.ex.approve("act_nonexistent", "王检察官")


if __name__ == "__main__":
    unittest.main()
