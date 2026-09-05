"""
tests/test_event_trace.py
REQ-G-004 事件发布留痕：
  - 事件落盘失败不再被 except: pass 静默吞掉 → 落 event_publish_failed 诊断（warning）
  - 发布失败不阻断主流程（defer/wake 仍返回任务）

手段：把 event_log 替换成 schema 不符的同名表，使 EventBus.publish 的 INSERT 抛
BinderException（EventBus.__init__ 的 CREATE IF NOT EXISTS 不会重建已存在的表）。
"""
from __future__ import annotations

import unittest

import duckdb

from core.deferred import DeferredBoard
from core.run_health import RunHealth


class EventPublishTraceTests(unittest.TestCase):
    def test_publish_failure_is_recorded_not_swallowed(self):
        con = duckdb.connect()
        h = RunHealth(con)
        board = DeferredBoard(con, health=h)
        # 预置坏 event_log：EventBus 初始化不重建（IF NOT EXISTS），INSERT 绑定失败
        con.execute("CREATE TABLE event_log (dummy VARCHAR)")
        # defer 内部 _publish 发布失败 → 被捕获留痕，defer 本身仍成功
        task = board.defer(candidate_id="cand-1",
                           wake_conditions={"evidence_count_gte": 3})
        self.assertEqual(task.candidate_id, "cand-1")
        diags = [r for r in h.rows() if r["kind"] == "event_publish_failed"]
        self.assertGreaterEqual(len(diags), 1, "发布失败应留 event_publish_failed 诊断")
        self.assertEqual(diags[0]["severity"], "warning")
        con.close()

    def test_publish_failure_does_not_block_wake(self):
        con = duckdb.connect()
        h = RunHealth(con)
        board = DeferredBoard(con, health=h)
        con.execute("CREATE TABLE event_log (dummy VARCHAR)")
        task = board.defer(candidate_id="cand-2",
                           wake_conditions={"evidence_count_gte": 1})
        woken = board.on_event(
            {"type": "dataset.updated", "payload": {"evidence_count": 5}})
        # 唤醒逻辑不受发布失败影响
        self.assertIn(task.task_id, [w.task_id for w in woken])
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
