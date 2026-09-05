"""
tests/test_metrics.py
REQ-030 规则运行时度量：
  - AC1：跑一次管线，rule_run_metric 每规则一行（run_id × rule_id 主键）
  - AC2：人工 verdict 回流后 later_verified / later_excluded 可更新
  - AC3：hit_rate / precision_estimate / override_rate 三个函数各单测（含分母 0→None）
  - AC4：指标按 rule_version 分层，同一规则改 params → 版本变化不混淆
  - AC5：低权限会话查敏感规则（R3/R4）→ 数值量级舍入 + 后验列置空 + _access_note
"""
from __future__ import annotations

import json
import unittest

from core import Store
from core.metrics import (ensure_rule_run_metric, record_run, verdict_backfill,
                          hit_rate, precision_estimate, override_rate,
                          rule_version, list_metrics)


class MetricsTests(unittest.TestCase):

    def _conn(self):
        s = Store(db_path=":memory:")
        ensure_rule_run_metric(s.conn)
        return s

    def test_ac1_one_row_per_rule(self):
        """AC1：一次管线 record_run 写每规则一行；6 条规则 = 6 行。"""
        store = self._conn()
        try:
            rules = ["R1", "R2", "R3", "R4", "R5", "R6"]
            rows = [{"rule_id": r, "rule_version": f"v0-{r}",
                     "evaluated": 100, "hit": 3 if r in {"R1","R4","R6"} else 0}
                    for r in rules]
            record_run(store.conn, "run_001", rows)
            got = store.conn.execute(
                "SELECT rule_id FROM rule_run_metric WHERE run_id = ? "
                "ORDER BY rule_id", ["run_001"]).fetchall()
            self.assertEqual([r[0] for r in got], rules,
                             f"应写 {len(rules)} 行每规则一条，实际 {len(got)}")
            # 幂等：再写 run_001 相同内容，不应产生重复行
            record_run(store.conn, "run_001", rows)
            cnt = store.conn.execute(
                "SELECT COUNT(*) FROM rule_run_metric WHERE run_id='run_001'").fetchone()[0]
            self.assertEqual(cnt, 6, "INSERT OR IGNORE 幂等应仍为 6 行")
        finally:
            store.close()

    def test_ac2_verdict_backfill(self):
        """AC2：verdict_backfill 改 later_verified/excluded + override_count。"""
        store = self._conn()
        try:
            record_run(store.conn, "run_002", [
                {"rule_id": "R1", "rule_version": "v0", "evaluated": 10, "hit": 2}])
            verdict_backfill(store.conn, "run_002", "R1", "verified")
            verdict_backfill(store.conn, "run_002", "R1", "verified", override=True)
            verdict_backfill(store.conn, "run_002", "R1", "excluded")
            row = store.conn.execute(
                "SELECT later_verified, later_excluded, override_count "
                "FROM rule_run_metric WHERE run_id='run_002' AND rule_id='R1'"
            ).fetchone()
            self.assertEqual(row[0], 2, "later_verified 应 +2（两次 verified）")
            self.assertEqual(row[1], 1, "later_excluded 应 +1")
            self.assertEqual(row[2], 1, "override_count 应 +1（只有 1 次标记 override）")
            # 非法 verdict → ValueError
            with self.assertRaises(ValueError):
                verdict_backfill(store.conn, "run_002", "R1", "bad_verdict")
        finally:
            store.close()

    def test_ac3_three_rates(self):
        """AC3：三个函数命中/精确/override；分母 0 → None。"""
        # hit_rate
        self.assertAlmostEqual(hit_rate({"evaluated": 100, "hit": 20}), 0.2)
        self.assertIsNone(hit_rate({"evaluated": 0, "hit": 0}))
        # precision_estimate
        self.assertAlmostEqual(precision_estimate(
            {"later_verified": 90, "later_excluded": 10}), 0.9)
        self.assertIsNone(precision_estimate({"later_verified": 0, "later_excluded": 0}))
        # override_rate
        self.assertAlmostEqual(override_rate(
            {"evaluated": 100, "override_count": 5}), 0.05)
        self.assertIsNone(override_rate({"evaluated": 0, "override_count": 1}))

    def test_ac4_rule_version_layered(self):
        """AC4：改 params → rule_version 变化（不与旧版指标混）。"""
        from dataclasses import dataclass
        @dataclass
        class FakeRule:
            id: str; rule_text: str; params: dict
        a = FakeRule("R3", "通话频次突增", {"absolute_threshold": 30})
        b = FakeRule("R3", "通话频次突增", {"absolute_threshold": 50})
        self.assertNotEqual(rule_version(a), rule_version(b),
                            "params 变化版本应变化（AC4 分层）")
        c = FakeRule("R3", "通话频次突增", {"absolute_threshold": 30})
        self.assertEqual(rule_version(a), rule_version(c),
                         "同 id+文本+params 版本相同")
        # 同 run_id 不同 rule_version 可以共存两行（不冲突）
        store = self._conn()
        try:
            record_run(store.conn, "run_ac4", [
                {"rule_id": "R3", "rule_version": rule_version(a),
                 "evaluated": 10, "hit": 2},
                {"rule_id": "R3", "rule_version": rule_version(b),
                 "evaluated": 10, "hit": 5},
            ])
            rs = store.conn.execute(
                "SELECT rule_version, hit FROM rule_run_metric "
                "WHERE run_id='run_ac4' AND rule_id='R3' ORDER BY hit").fetchall()
            self.assertEqual(len(rs), 2, "不同 version 存两行，不被 PK 互吞")
            self.assertEqual([r[1] for r in rs], [2, 5])
        finally:
            store.close()

    def test_ac5_low_clearance_sensitive_rules_redacted(self):
        """AC5：低权限（正兵 clear=1）→ R3/R4 指标被遮蔽（量级舍入 + 后验置空 + note）。"""
        store = self._conn()
        try:
            record_run(store.conn, "run_ac5", [
                {"rule_id": "R3", "rule_version": "v1", "evaluated": 137, "hit": 27},
                {"rule_id": "R4", "rule_version": "v1", "evaluated": 7, "hit": 2},
                {"rule_id": "R5", "rule_version": "v1", "evaluated": 30, "hit": 5},
            ])
            verdict_backfill(store.conn, "run_ac5", "R3", "verified")
            from core.access import AccessContext
            low = AccessContext(operator="小兵", role="正兵", clearance="1",
                                network="isolated", purpose="test")
            result = list_metrics(store.conn, access=low)
            by = {r["rule_id"]: r for r in result}
            # R3 敏感：evaluated=137 → 130（10 量级），hit=27→20
            self.assertEqual(by["R3"]["evaluated"], 130,
                             f"R3 evaluated 应 10 进制舍入量级：{by['R3']['evaluated']}")
            self.assertEqual(by["R3"]["hit"], 20)
            self.assertIsNone(by["R3"]["later_verified"],
                              "敏感规则后验列应置空（AC5）")
            self.assertIn("_access_note", by["R3"])
            # R4 敏感：hit=2<10 → 舍入到 10
            self.assertEqual(by["R4"]["evaluated"], 10,
                             f"<10 应抬到 10（不暴露精确数字）：{by['R4']['evaluated']}")
            # R5 非敏感，全量保留
            self.assertEqual(by["R5"]["evaluated"], 30)
            self.assertEqual(by["R5"]["hit"], 5)
            self.assertNotIn("_access_note", by["R5"])
            # system（None）：无遮蔽
            full = list_metrics(store.conn, access=None)
            r3_full = next(r for r in full if r["rule_id"] == "R3")
            self.assertEqual(r3_full["evaluated"], 137)
            self.assertEqual(r3_full["hit"], 27)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
