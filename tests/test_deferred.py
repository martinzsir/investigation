"""
tests/test_deferred.py
REQ-017 defer 回捞机制 测试。

覆盖 AC1-AC5：
  AC1: 无 wake_conditions 的 defer → ValueError
  AC2: 新数据集到达 → on_dataset 匹配任务唤醒并重入 ReviewQueue
  AC3: TTL（after）到期 → scan_due 唤醒
  AC4: 已唤醒任务不重复唤醒
  AC5: 回捞率统计（decided_after_wake / total）
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                          # noqa: E402
from core.deferred import DeferredBoard, DeferredTask, match_wake  # noqa: E402
from core.review import ReviewQueue, ReviewDecision, Decision   # noqa: E402


def _event(etype: str, payload: dict, occurred: str | None = None):
    return SimpleNamespace(
        type=etype, payload=payload,
        occurred_at=occurred or datetime.now().isoformat(timespec="seconds"))


class TestDeferred(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.board = DeferredBoard(self.store.conn)

    def tearDown(self):
        self.store.close()

    def test_ac1_defer_requires_wake_conditions(self):
        """AC1: 无条件/非法条件 → ValueError。"""
        with self.assertRaises(ValueError):
            self.board.defer(candidate_id="rev_person_1", wake_conditions={})
        with self.assertRaises(ValueError):
            self.board.defer(candidate_id="rev_person_1",
                             wake_conditions={"bogus": "x"})
        with self.assertRaises(ValueError):
            self.board.defer(candidate_id="rev_person_1",
                             wake_conditions={"after": "2026/10/01"})
        # 合法登记
        t = self.board.defer(candidate_id="rev_person_1",
                             wake_conditions={"on_dataset": "通话记录"},
                             entity_type="person", canonical="张卫国")
        self.assertEqual(t.status, "waiting")

    def test_ac2_dataset_event_wakes_and_reenters_queue(self):
        """AC2: 新通话数据到达 → on_dataset=通话记录 任务唤醒并重入队列。"""
        self.board.defer(candidate_id="rev_person_1",
                         wake_conditions={"on_dataset": "通话记录"})
        self.board.defer(candidate_id="rev_person_2",
                         wake_conditions={"on_dataset": "轨迹出行"})
        # 队列中候选 1 已暂缓
        q = ReviewQueue([
            ReviewDecision(candidate_id="rev_person_1", entity_type="person",
                           canonical="张卫国", variants=["张伟"],
                           reason="x", status=Decision.DEFERRED),
        ])
        ev = _event("source.partition.arrived",
                    {"dataset": "通话记录", "rows": 50})
        woken = self.board.on_event(ev)
        self.assertEqual([t.candidate_id for t in woken], ["rev_person_1"])
        # 不匹配的任务仍 waiting
        self.assertEqual(self.board.list_all("waiting")[0].candidate_id, "rev_person_2")
        # 重入 ReviewQueue：状态回到待确认
        n = self.board.reenter_review_queue(q)
        self.assertEqual(n, 1)
        self.assertEqual(q.get("rev_person_1").status, Decision.PENDING)

    def test_ac3_ttl_due_wakes(self):
        """AC3: after 到期 → scan_due 唤醒；未到期不动。"""
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.board.defer(candidate_id="rev_person_3",
                         wake_conditions={"after": future})
        self.board.defer(candidate_id="rev_person_4",
                         wake_conditions={"after": past})
        due = self.board.scan_due()
        self.assertEqual([t.candidate_id for t in due], ["rev_person_4"])
        self.assertEqual(self.board.list_all("waiting")[0].candidate_id, "rev_person_3")

    def test_ac4_woken_not_rewoken(self):
        """AC4: 已唤醒任务不再被后续事件/扫描重复唤醒。"""
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        t = self.board.defer(candidate_id="rev_person_5",
                             wake_conditions={"on_dataset": "通话记录",
                                              "after": past})
        ev = _event("source.partition.arrived", {"dataset": "通话记录"})
        first = self.board.on_event(ev)
        self.assertEqual(len(first), 1)
        # 再来事件 / 再扫描：不重复
        self.assertEqual(self.board.on_event(ev), [])
        self.assertEqual(self.board.scan_due(), [])
        self.assertEqual(self.board.list_all("woken")[0].task_id, t.task_id)

    def test_match_wake_evidence_count(self):
        """evidence_count_gte 条件。"""
        t = DeferredTask(task_id="x", candidate_id="c",
                         wake_conditions={"evidence_count_gte": 3},
                         scheduled_at="2026-01-01")
        self.assertTrue(match_wake(t, _event("source.partition.arrived",
                                             {"dataset": "通话记录", "evidence_count": 3})))
        self.assertFalse(match_wake(t, _event("source.partition.arrived",
                                              {"evidence_count": 2})))

    def test_ac5_recall_rate(self):
        """AC5: 回捞率 = 唤醒后已二次决策 / 总暂缓。"""
        self.board.defer(candidate_id="c1", wake_conditions={"after": "2020-01-01"})
        self.board.defer(candidate_id="c2", wake_conditions={"after": "2099-01-01"})
        self.board.defer(candidate_id="c3", wake_conditions={"on_dataset": "通话记录"})
        # c1 TTL 到期唤醒并已二次决策；c3 被事件唤醒未决策；c2 等待
        self.board.scan_due()
        self.board.mark_decided("c1")
        self.board.on_event(_event("source.partition.arrived", {"dataset": "通话记录"}))
        stats = self.board.recall_stats()
        self.assertEqual(stats["total_deferred"], 3)
        self.assertEqual(stats["woken"], 2)
        self.assertEqual(stats["decided_after_wake"], 1)
        self.assertAlmostEqual(stats["recall_rate"], 1 / 3, places=3)


if __name__ == "__main__":
    unittest.main()
