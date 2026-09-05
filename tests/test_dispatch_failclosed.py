"""
tests/test_dispatch_failclosed.py
REQ-G-020 派发 fail-closed：
  - outbox 不可用（ImportError）时，action_request 不得停在 'dispatching'
    （那会用"进行中"掩盖失败），须置 'dispatch_failed' 并 critical 留痕
  - 正常路径（outbox 可用）仍流转 pending_receipt，不受影响
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.action_executor import ActionExecutor               # noqa: E402
from core.registry import LineageClue                         # noqa: E402
from core.run_health import RunHealth                         # noqa: E402


class DispatchFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.health = RunHealth(self.store.conn)
        self.ex = ActionExecutor(self.store, health=self.health)
        self.clue = LineageClue(title="派发失败测试线索")

    def tearDown(self):
        self.store.close()

    def test_outbox_unavailable_marks_dispatch_failed(self):
        aid = self.ex.submit("verify", self.clue, "王检察官", {"note": "x"})
        self.ex.approve(aid, "李主办")
        # 模拟 outbox 不可用：sys.modules 置 None 使 `from core.outbox import Outbox`
        # 抛 ImportError（与 outbox 依赖缺失同态）
        saved = sys.modules.get("core.outbox")
        sys.modules["core.outbox"] = None
        try:
            result = self.ex.dispatch(aid, self.clue)
        finally:
            if saved is None:
                sys.modules.pop("core.outbox", None)
            else:
                sys.modules["core.outbox"] = saved
        # 不再假装在途
        self.assertEqual(result["status"], "dispatch_failed")
        req = self.ex.request_status(aid)
        self.assertEqual(req["status"], "dispatch_failed")
        self.assertNotEqual(req["status"], "dispatching")
        self.assertTrue(req.get("last_error"))
        # critical 留痕
        diags = self.health.rows()
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["kind"], "dispatch_failed")
        self.assertEqual(diags[0]["severity"], "critical")

    def test_normal_path_still_pending_receipt(self):
        # outbox 正常 → 仍入队 pending_receipt（回归：fail-closed 不影响正常流转）
        ex2 = ActionExecutor(self.store, health=self.health)
        clue2 = LineageClue(title="正常派发红索")
        aid = ex2.submit("verify", clue2, "王检察官", {"note": "ok"})
        ex2.approve(aid, "李主办")
        result = ex2.dispatch(aid, clue2)
        self.assertEqual(result["status"], "pending_receipt")
        self.assertEqual(ex2.request_status(aid)["status"], "pending_receipt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
