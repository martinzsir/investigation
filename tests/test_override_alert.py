"""
tests/test_override_alert.py
REQ-G-017 规则推翻率告警：
  - override_rate = override_count / evaluated（分母 0 → None，不告警）
  - 超阈值（thresholds.json alerts.override_rate_max，缺省 0.5）→
    record override_rate_alert(warning)，按规则粒度
  - 阈值可参数化覆盖；未超阈规则不产生告警
"""
from __future__ import annotations

import unittest

from core import Store
from core.run_health import RunHealth
from core.metrics import (record_run, verdict_backfill, override_rate,
                          alert_override_rate, list_metrics)


class OverrideAlertTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.conn = self.store.conn
        self.health = RunHealth(self.conn)
        self.run_id = "run-test-1"
        record_run(self.conn, self.run_id, [
            {"rule_id": "R1", "evaluated": 10, "hit": 10},
            {"rule_id": "R2", "evaluated": 10, "hit": 2},
            {"rule_id": "R0", "evaluated": 0, "hit": 0},
        ])

    def tearDown(self):
        self.store.close()

    def _seed_overrides(self):
        # R1：8/10 被人工推翻（excluded + override）→ 0.8 > 0.5
        for _ in range(8):
            verdict_backfill(self.conn, self.run_id, "R1",
                             "excluded", override=True)
        # R2：1/10 推翻 → 0.1，不超阈
        verdict_backfill(self.conn, self.run_id, "R2", "excluded", override=True)

    def test_override_rate_basic(self):
        self._seed_overrides()
        rows = {r["rule_id"]: r for r in list_metrics(self.conn)}
        self.assertAlmostEqual(override_rate(rows["R1"]), 0.8, places=3)
        self.assertAlmostEqual(override_rate(rows["R2"]), 0.1, places=3)
        self.assertIsNone(override_rate(rows["R0"]))  # 分母 0

    def test_alert_fires_above_threshold(self):
        self._seed_overrides()
        alerts = alert_override_rate(self.conn, health=self.health)
        rids = [a["rule_id"] for a in alerts]
        self.assertIn("R1", rids)
        self.assertNotIn("R2", rids)
        self.assertNotIn("R0", rids)  # evaluated=0 → None，跳过
        kinds = [d["kind"] for d in self.health.rows()]
        self.assertEqual(kinds.count("override_rate_alert"), 1)
        self.assertEqual(self.health.rows()[0]["severity"], "warning")

    def test_threshold_param_raises_bar(self):
        self._seed_overrides()
        # 阈值抬到 0.9 → 0.8 不告警
        alerts = alert_override_rate(self.conn, health=self.health, threshold=0.9)
        self.assertEqual(alerts, [])
        self.assertEqual(self.health.rows(), [])

    def test_default_threshold_from_thresholds_json(self):
        # default 包 thresholds.json alerts.override_rate_max = 0.5
        from core.metrics import _override_alert_threshold
        self.assertEqual(_override_alert_threshold("default"), 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
