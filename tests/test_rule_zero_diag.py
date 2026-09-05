"""
tests/test_rule_zero_diag.py
REQ-G-002 规则零命中诊断：
  - 零命中不再裸 continue：产 rule_zero_hit 诊断（带 zero_type/scan_rows/matched_rows）
  - 四分类互不混淆：data_absent / config_missing / empty_result_suspect / clean_scan
  - 诊断进 run_diagnostic，绝不进 findings 主列表
  - 机器不擅自下"排除/clean"结论：clean_scan 必须规则显式 zero_is_clean=true
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.rules import _classify_zero, run_rules
from core.run_health import RunHealth

ZERO_TYPES = {"data_absent", "config_missing", "empty_result_suspect", "clean_scan"}


class ClassifyZeroTests(unittest.TestCase):
    def _rule(self, zero_is_clean=False):
        return SimpleNamespace(zero_is_clean=zero_is_clean)

    def test_degraded_is_data_absent_info(self):
        zt, sev = _classify_zero(self._rule(), {"degraded": True}, scan_rows=0)
        self.assertEqual(zt, "data_absent")
        self.assertEqual(sev, "info")

    def test_config_missing_is_warning(self):
        zt, sev = _classify_zero(
            self._rule(), {"result": {"config_missing": True}}, scan_rows=5)
        self.assertEqual(zt, "config_missing")
        self.assertEqual(sev, "warning")

    def test_no_rows_is_data_absent(self):
        zt, sev = _classify_zero(self._rule(), {}, scan_rows=0)
        self.assertEqual(zt, "data_absent")
        self.assertEqual(sev, "info")

    def test_explicit_clean_scan(self):
        zt, sev = _classify_zero(self._rule(zero_is_clean=True), {}, scan_rows=5)
        self.assertEqual(zt, "clean_scan")
        self.assertEqual(sev, "info")

    def test_suspect_when_rows_present_but_zero_match(self):
        # 有输入却零命中、且未声明 clean → 疑似匹配失效（warning），机器不擅自判 clean
        zt, sev = _classify_zero(self._rule(zero_is_clean=False), {}, scan_rows=5)
        self.assertEqual(zt, "empty_result_suspect")
        self.assertEqual(sev, "warning")


class RunRulesZeroDiagTests(unittest.TestCase):
    def _make_store(self):
        from core import Store
        from core.ontology import build_ontology
        s = Store(db_path=":memory:")
        s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        s.execute("INSERT INTO 银行流水 VALUES ('张','现金存入',10000,'2021-09-28'),"
                  "('李','财政局',18532,'2021-09-29')")
        build_ontology(s.conn)
        return s

    def test_zero_hit_emits_diagnostic_not_finding(self):
        s = self._make_store()
        h = RunHealth(s.conn)
        try:
            findings = run_rules(s, stage="xu_shi", health=h)
            diags = [r for r in h.rows() if r["kind"] == "rule_zero_hit"]
        finally:
            s.close()
        # 数据稀疏：至少有规则零命中（如通话类 obj_call 缺失）
        self.assertGreaterEqual(len(diags), 1, "应记录零命中诊断")
        for d in diags:
            self.assertIn(d["detail"]["zero_type"], ZERO_TYPES)
            self.assertIn(d["severity"], {"info", "warning"})
            self.assertIn("rule_id", d["detail"])
            self.assertIn("scan_rows", d["detail"])
        # findings 主列表不得混入诊断对象（findings 是命中线索，带 rule_id/级别）
        for f in findings:
            self.assertNotIn("zero_type", f)
            self.assertEqual(f.get("级别"), "待核实")

    def test_health_none_keeps_legacy_behavior(self):
        # 兼容红线：不传 health 不报错、findings 结构不变
        s = self._make_store()
        try:
            findings = run_rules(s, stage="xu_shi")
        finally:
            s.close()
        self.assertIsInstance(findings, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
