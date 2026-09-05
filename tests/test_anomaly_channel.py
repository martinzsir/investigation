"""
tests/test_anomaly_channel.py
REQ-G-019 异常线索通道：
  AC1 异常条目与正常线索同构且携带 source_rows
  AC2 级别恒待核实、needs_human_review 恒真、is_anomaly 恒真
  AC3 异常条目不参与五间交叉等级计算（构造异常后等级不变）
  AC4 异常条目按主体聚合输出
另：仅 empty_result_suspect 转化（clean_scan/data_absent/config_missing 不转化）。
"""
from __future__ import annotations

import unittest

import duckdb

from core import Store
from core.run_health import RunHealth, NullRunHealth
from core import anomaly_channel as ac
from core.functions import FUNCTION_IMPLS

# 与 core/rules.py 正常 finding 对齐的关键字段
_FINDING_KEYS = {"rule_id", "候选虚处", "依据", "级别", "source_rows",
                 "rule_text", "dimension", "jian_types", "assumption"}


def _conn():
    return duckdb.connect(":memory:")


def _record_mix(health, *, subject=None):
    kw = {"subject": subject} if subject else {}
    # 应转化：疑似失效的零命中
    health.record("rule_zero_hit", "warning", source="rule:R1",
                  reason="R1 季末整数存入 零命中（empty_result_suspect）",
                  rule_id="R1", function="quarter_end_integer_deposits",
                  zero_type="empty_result_suspect", scan_rows=100,
                  matched_rows=0, dimension="资金", **kw)
    # 应转化：函数空转降级
    health.record("function_empty_degraded", "warning", source="function:f2",
                  reason="通话频次检测缺表降级",
                  function="call_frequency_spike", dimension="通讯", **kw)
    # 应转化：覆盖缺口
    health.record("coverage_gap", "warning", source="miao",
                  reason="维度覆盖不足", missing=["关系", "时间"], **kw)
    # 不应转化：显式 clean / 数据缺失 / 配置缺失
    health.record("rule_zero_hit", "info", source="rule:R9",
                  reason="R9 零命中（clean_scan）", rule_id="R9",
                  zero_type="clean_scan", scan_rows=0, matched_rows=0,
                  dimension="行为", **kw)
    health.record("rule_zero_hit", "info", source="rule:R8",
                  reason="R8 零命中（data_absent）", rule_id="R8",
                  zero_type="data_absent", scan_rows=0, matched_rows=0,
                  dimension="资金", **kw)
    health.record("rule_zero_hit", "info", source="rule:R7",
                  reason="R7 零命中（config_missing）", rule_id="R7",
                  zero_type="config_missing", scan_rows=0, matched_rows=0,
                  dimension="关系", **kw)


class AnomalyChannelTests(unittest.TestCase):
    def _diag_kinds(self, health):
        return [r["kind"] for r in health.rows()]

    def test_ac1_isomorphic_and_source_rows(self):
        conn = _conn()
        h = RunHealth(conn)
        _record_mix(h)
        clues = ac.emit_anomaly_clues(h)
        self.assertTrue(clues)
        for c in clues:
            # 同构：具备正常 finding 的关键字段
            self.assertTrue(_FINDING_KEYS.issubset(c.keys()),
                            f"缺字段：{_FINDING_KEYS - set(c.keys())}")
            self.assertIn("source_rows", c)          # AC1：携带 source_rows
            self.assertEqual(c["source_rows"], [])    # 异常无证据行
            self.assertTrue(c["diagnostic_ids"])      # 但有诊断溯源

    def test_ac2_forced_markers(self):
        conn = _conn()
        h = RunHealth(conn)
        _record_mix(h, subject="张三")
        _record_mix(h, subject="李四")
        clues = ac.emit_anomaly_clues(h)
        self.assertTrue(clues)
        for c in clues:
            self.assertEqual(c["级别"], "待核实")      # 级别恒待核实
            self.assertIs(c["needs_human_review"], True)
            self.assertIs(c["is_anomaly"], True)

    def test_eligibility_only_empty_suspect(self):
        conn = _conn()
        h = RunHealth(conn)
        _record_mix(h)
        clues = ac.emit_anomaly_clues(h, record=False)
        # 全局主体 1 条合并线索，涵盖 3 类可转化诊断（clean/data/config 不转化）
        self.assertEqual(len(clues), 1)
        kinds = set(clues[0]["anomaly_kinds"])
        self.assertEqual(kinds, {"rule_zero_hit", "function_empty_degraded",
                                 "coverage_gap"})
        self.assertNotIn("clean_scan", str(clues[0]))

    def test_ac3_not_in_cross_level(self):
        store = Store(db_path=":memory:")
        fn = FUNCTION_IMPLS["jian_cross_level"]
        before = fn(store, {"pack": "default"})
        # 构造大量异常条目（多主体、多类）
        conn = store.conn
        h = RunHealth(conn)
        _record_mix(h, subject="张三")
        _record_mix(h, subject="李四")
        _record_mix(h, subject="王五")
        clues = ac.emit_anomaly_clues(h)
        self.assertTrue(len(clues) >= 3)
        after = fn(store, {"pack": "default"})
        # 红线：异常不贡献交叉命中、不升格
        self.assertEqual(after["交叉等级"], before["交叉等级"])
        self.assertEqual(after["命中间类"], before["命中间类"])
        # 交叉/升格前过滤：异常全被剔除
        self.assertEqual(ac.non_anomaly(clues), [])
        mixed = [{"rule_id": "R1", "级别": "待核实"}] + clues
        normal, anomalies = ac.partition(mixed)
        self.assertEqual(len(normal), 1)
        self.assertEqual(len(anomalies), len(clues))
        store.close()

    def test_ac4_group_by_subject(self):
        conn = _conn()
        h = RunHealth(conn)
        _record_mix(h, subject="张三")   # 张三 3 条可转化
        _record_mix(h, subject="李四")   # 李四 3 条
        _record_mix(h)                   # 全局 3 条
        clues = ac.emit_anomaly_clues(h, record=False)
        # 每个主体聚合成 1 条
        subjects = {c["subject"] for c in clues}
        self.assertEqual(subjects, {"张三", "李四", "全局"})
        zs = next(c for c in clues if c["subject"] == "张三")
        self.assertEqual(zs["anomaly_count"], 3)
        self.assertEqual(len(zs["diagnostic_ids"]), 3)
        grouped = ac.group_by_subject(clues)
        self.assertEqual(set(grouped), {"张三", "李四", "全局"})
        self.assertTrue(all(len(v) == 1 for v in grouped.values()))

    def test_null_health_safe(self):
        # health=None（NullRunHealth）→ 无异常线索，不报错
        self.assertEqual(ac.emit_anomaly_clues(None), [])
        self.assertIsInstance(NullRunHealth(), NullRunHealth)

    def test_emits_audit_diagnostic(self):
        conn = _conn()
        h = RunHealth(conn)
        _record_mix(h)
        ac.emit_anomaly_clues(h)
        kinds = self._diag_kinds(h)
        self.assertIn("anomaly_clue_emitted", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
