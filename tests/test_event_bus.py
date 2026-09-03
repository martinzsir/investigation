"""
tests/test_event_bus.py
REQ-006 事件总线与持久化事件日志 测试。

覆盖 AC1-AC5：
  AC1: 发布事件落盘，进程重启后仍可读取
  AC2: 同一 idempotency_key 重复投递，handler 只执行一次
  AC3: replay() 重放后状态与首次执行一致（确定性）
  AC4: causation_id 形成链，循环可被检测
  AC5: handler 抛异常不丢事件，进 dead_letter
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                    # noqa: E402
from core.event_bus import EventBus, Event                # noqa: E402


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.bus = EventBus(self.store.conn)

    def test_ac1_persisted_across_restart(self):
        """AC1: 进程重启后仍可读取"""
        self.bus.publish("finding.created", {"clue_id": "c1"}, actor="test")
        # 模拟重启：用同一 conn 新建 EventBus
        bus2 = EventBus(self.store.conn)
        rows = bus2.list_events(type="finding.created")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "test")

    def test_ac2_idempotency_key_dedup(self):
        """AC2: 同一 idempotency_key 重复投递只执行一次"""
        calls = []
        self.bus.subscribe("finding.created",
            lambda e: calls.append(e.event_id),
            idempotency_key_fn=lambda e: e.payload["clue_id"])
        self.bus.publish("finding.created", {"clue_id": "c1"}, actor="test")
        self.bus.publish("finding.created", {"clue_id": "c1"}, actor="test")
        self.assertEqual(len(calls), 1)

    def test_ac3_replay_deterministic(self):
        """AC3: replay 后状态与首次执行一致"""
        seen1 = []
        self.bus.subscribe("finding.created",
            lambda e: seen1.append(e.payload["clue_id"]),
            idempotency_key_fn=lambda e: e.event_id)
        for i in range(3):
            self.bus.publish("finding.created", {"clue_id": f"c{i}"}, actor="test")
        self.assertEqual(seen1, ["c0", "c1", "c2"])
        # 重放
        seen2 = []
        bus2 = EventBus(self.store.conn)
        bus2.subscribe("finding.created",
            lambda e: seen2.append(e.payload["clue_id"]),
            idempotency_key_fn=lambda e: e.event_id)
        n = bus2.replay()
        self.assertEqual(n, 3)
        self.assertEqual(seen2, ["c0", "c1", "c2"])

    def test_ac4_causation_cycle_detection(self):
        """AC4: causation_id 形成链，深度 > N 告警循环"""
        e1 = self.bus.publish("finding.created", {"x": 1}, actor="t")
        e2 = self.bus.publish("clue.status_changed", {"x": 2},
                              actor="t", causation_id=e1)
        e3 = self.bus.publish("finding.changed", {"x": 3},
                              actor="t", causation_id=e2)
        # 构造深度 > 10 的链
        prev = e3
        for i in range(15):
            prev = self.bus.publish("finding.changed", {"i": i},
                                    actor="t", causation_id=prev)
        self.assertTrue(self.bus.detect_cycle(prev, max_depth=10))
        # 短链不应判为循环
        self.assertFalse(self.bus.detect_cycle(e2, max_depth=10))

    def test_ac5_dead_letter_on_handler_exception(self):
        """AC5: handler 抛异常不丢事件，进 dead_letter"""
        def bad_handler(e):
            raise RuntimeError("boom")
        self.bus.subscribe("finding.created", bad_handler,
                          idempotency_key_fn=lambda e: e.event_id)
        self.bus.publish("finding.created", {"x": 1}, actor="t")
        rows = self.store.query("SELECT COUNT(*) AS n FROM event_dead_letter")
        self.assertEqual(rows[0]["n"], 1)
        # 事件本身仍落盘
        rows = self.store.query("SELECT COUNT(*) AS n FROM event_log")
        self.assertEqual(rows[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
