"""
tests/test_wake_condition.py
REQ-G-005 唤醒条件显式化：
  - waiting 任务携带 evidence_count_gte 条件，事件 evidence_count 非整数 →
    任务转 condition_error（区别于沉睡），落 wake_condition_unparseable（critical）
  - 合法整数证据计数正常唤醒（不误伤）
  - 事件不带 evidence_count 字段时与该条件无关（保持沉睡，不报错）
  - 登记时畸形条件（阈值非整数）直接被拒，不入池
"""
from __future__ import annotations

import unittest

import duckdb

from core.deferred import (DeferredBoard, condition_parse_error,
                           validate_wake_conditions, _CONDITION_ERROR, _WAITING)
from core.run_health import RunHealth


class WakeConditionTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.h = RunHealth(self.con)
        self.board = DeferredBoard(self.con, health=self.h)

    def tearDown(self):
        self.con.close()

    def test_non_integer_evidence_count_marks_condition_error(self):
        task = self.board.defer(candidate_id="cand-x",
                                wake_conditions={"evidence_count_gte": 3})
        woken = self.board.on_event(
            {"type": "dataset.updated", "payload": {"evidence_count": "lots"}})
        self.assertEqual(woken, [], "条件不可解析不应唤醒")
        # 任务状态变为 condition_error
        rows = self.board.list_all(_CONDITION_ERROR)
        self.assertEqual([t.task_id for t in rows], [task.task_id])
        # critical 诊断
        diags = [r for r in self.h.rows()
                 if r["kind"] == "wake_condition_unparseable"]
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["severity"], "critical")
        # 回捞统计含 condition_error
        self.assertGreaterEqual(self.board.recall_stats()["condition_error"], 1)

    def test_integer_evidence_count_wakes_normally(self):
        task = self.board.defer(candidate_id="cand-y",
                                wake_conditions={"evidence_count_gte": 3})
        woken = self.board.on_event(
            {"type": "dataset.updated", "payload": {"evidence_count": 5}})
        self.assertIn(task.task_id, [w.task_id for w in woken])
        # 无误报条件错误
        self.assertFalse([r for r in self.h.rows()
                          if r["kind"] == "wake_condition_unparseable"])

    def test_event_without_count_field_stays_waiting(self):
        self.board.defer(candidate_id="cand-z",
                         wake_conditions={"evidence_count_gte": 3})
        woken = self.board.on_event(
            {"type": "dataset.updated", "payload": {"other": "x"}})
        self.assertEqual(woken, [])
        self.assertEqual(len(self.board.list_all(_WAITING)), 1)

    def test_validate_rejects_non_integer_threshold(self):
        with self.assertRaises(ValueError):
            validate_wake_conditions({"evidence_count_gte": "three"})

    def test_condition_parse_error_helper(self):
        from core.deferred import DeferredTask
        task = DeferredTask(
            task_id="t1", candidate_id="c", entity_type="", canonical="",
            wake_conditions={"evidence_count_gte": 3},
            scheduled_at="", status=_WAITING, created_by="sys")
        bad = {"type": "e", "payload": {"evidence_count": "N/A"}}
        good = {"type": "e", "payload": {"evidence_count": 10}}
        nofield = {"type": "e", "payload": {}}
        self.assertIsNotNone(condition_parse_error(task, bad))
        self.assertIsNone(condition_parse_error(task, good))
        self.assertIsNone(condition_parse_error(task, nofield))


if __name__ == "__main__":
    unittest.main(verbosity=2)
