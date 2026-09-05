"""
tests/test_run_health.py
REQ-G-010 统一降级层 RunHealth：
  - record/rows/summary/health_section 基本留痕与聚合
  - 状态滚定：info→healthy / warning→degraded / critical→critical
  - fail-loud：未知 kind / 非法 severity 直接抛错（编程错误不静默）
  - NullRunHealth 全空操作（health=None 兼容红线）
  - _dropped 实例隔离（不跨实例共享）
"""
from __future__ import annotations

import unittest

import duckdb

from core.run_health import (RunHealth, NullRunHealth, get_health,
                             KINDS, SEVERITIES)


class RunHealthTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.h = RunHealth(self.con)

    def tearDown(self):
        self.con.close()

    def test_record_and_rows(self):
        self.h.record("rule_zero_hit", "warning", source="rule:R1",
                      reason="零命中", rule_id="R1", zero_type="empty_result_suspect")
        rows = self.h.rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["kind"], "rule_zero_hit")
        self.assertEqual(r["severity"], "warning")
        self.assertEqual(r["source"], "rule:R1")
        # detail JSON 被解析回 dict
        self.assertEqual(r["detail"]["rule_id"], "R1")
        self.assertEqual(r["detail"]["zero_type"], "empty_result_suspect")

    def test_status_rollup(self):
        # 仅 info → healthy
        self.h.record("entity_table_skipped", "info", source="t", reason="x")
        self.assertEqual(self.h.health_section()["status"], "healthy")
        # warning → degraded
        h2 = RunHealth(self.con)
        h2.record("function_empty_degraded", "warning", source="f", reason="x")
        self.assertEqual(h2.health_section()["status"], "degraded")
        # critical → critical（即便也有 warning）
        h3 = RunHealth(self.con)
        h3.record("dispatch_failed", "critical", source="a", reason="x")
        h3.record("function_empty_degraded", "warning", source="f", reason="x")
        self.assertEqual(h3.health_section()["status"], "critical")

    def test_summary_aggregates_by_kind_severity(self):
        self.h.record("rule_zero_hit", "warning", source="r:1", reason="x")
        self.h.record("rule_zero_hit", "info", source="r:2", reason="x")
        self.h.record("entity_table_skipped", "info", source="t", reason="x")
        s = self.h.summary()
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_kind"]["rule_zero_hit"], 2)
        self.assertEqual(s["by_kind"]["entity_table_skipped"], 1)
        self.assertEqual(s["by_severity"]["warning"], 1)
        self.assertEqual(s["by_severity"]["info"], 2)

    def test_zero_hit_rules_collected(self):
        self.h.record("rule_zero_hit", "warning", source="rule:R9", reason="x",
                      rule_id="R9", zero_type="empty_result_suspect",
                      scan_rows=10, matched_rows=0)
        sec = self.h.health_section()
        zh = sec["零命中规则"]
        self.assertEqual(len(zh), 1)
        self.assertEqual(zh[0]["rule_id"], "R9")
        self.assertEqual(zh[0]["zero_type"], "empty_result_suspect")
        self.assertEqual(zh[0]["scan_rows"], 10)

    def test_fail_loud_bad_kind(self):
        with self.assertRaises(ValueError):
            self.h.record("not_a_kind", "info", source="x", reason="y")

    def test_fail_loud_bad_severity(self):
        with self.assertRaises(ValueError):
            self.h.record("rule_zero_hit", "fatal", source="x", reason="y")

    def test_all_kinds_and_severities_valid(self):
        # 枚举内每个 kind/severity 都应可写（防枚举与校验不一致）
        for i, k in enumerate(KINDS):
            self.h.record(k, SEVERITIES[i % len(SEVERITIES)], source="x", reason="y")
        self.assertEqual(len(self.h.rows()), len(KINDS))

    def test_null_run_health_noop(self):
        n = get_health(None)
        self.assertIsInstance(n, NullRunHealth)
        n.record("rule_zero_hit", "warning", source="x", reason="y")  # 不抛
        self.assertEqual(n.rows(), [])
        self.assertEqual(n.summary()["total"], 0)
        self.assertEqual(n.health_section()["status"], "healthy")
        # 传入真 RunHealth 原样返回
        self.assertIs(get_health(self.h), self.h)

    def test_dropped_is_instance_attribute(self):
        # 回归：_dropped 不得为类属性（多实例共享会串计数）
        a = RunHealth(self.con)
        b = RunHealth(self.con)
        self.assertIsNot(a._dropped, b._dropped)
        self.assertEqual(a._dropped, [])
        self.assertEqual(b._dropped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
