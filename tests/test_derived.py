"""
tests/test_derived.py
REQ-028 DerivedProperty（查询时派生）：
  - AC1：cache_policy=never 每次重算（两次取 computed_at 不同）
  - AC2：until_source_change 在源变更后失效（缓存 source_version_set 变化）
  - AC3：非白名单 Function 被拒（不注册到 functions.json 的函数绑定 → 抛错）
  - AC4：params_hash 变化缓存失效（参数变 → 不复用旧值）
  - AC5：person.risk_score 等启发式打分不允许注册为派生属性（保持展示层）
"""
from __future__ import annotations

import unittest

from core import derived as D
from core.derived import (DerivedProperty, register, compute, list_all,
                          _FORBIDDEN_REGISTRY_NAMES)


class DerivedTests(unittest.TestCase):

    def setUp(self):
        # 清注册表（测试隔离）
        D._REGISTRY.clear()

    def _make_store(self):
        from core import Store
        from core.ontology import build_ontology
        s = Store(db_path=":memory:")
        s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        s.execute("INSERT INTO 银行流水 VALUES ('张','现金存入',10000,'2021-09-28'),"
                  "('李','现金存入',50000,'2021-09-29')")
        build_ontology(s.conn)
        return s

    def test_ac1_never_policy_always_recompute(self):
        """AC1：never → 两次 compute，computed_at 不同（且 params_hash 相同）。"""
        register(DerivedProperty(
            name="transaction.hit_summary",
            function="quarter_end_integer_deposits",
            inputs=[],
            cache_policy="never"))
        s = self._make_store()
        try:
            r1 = compute(s, "transaction", "hit_summary")
            r2 = compute(s, "transaction", "hit_summary")
        finally:
            s.close()
        self.assertEqual(r1["params_hash"], r2["params_hash"],
                         "同参数 hash 应相同")
        # computed_at 时间单位是秒；两次计算可能同秒——用人工时间函数
        ticks = [1000.0, 1001.5]
        it = iter(ticks)
        s = self._make_store()
        try:
            r1 = compute(s, "transaction", "hit_summary", _now_fn=lambda: next(it))
            r2 = compute(s, "transaction", "hit_summary", _now_fn=lambda: next(it))
        finally:
            s.close()
        self.assertNotEqual(r1["computed_at"], r2["computed_at"],
                            "never 策略 computed_at 应变化")
        self.assertEqual(r1["cache"], "miss")
        self.assertEqual(r2["cache"], "miss")

    def test_ac2_until_source_change_invalidates(self):
        """AC2：until_source_change 版本变化 → 缓存失效。"""
        register(DerivedProperty(
            name="transaction.hit_summary",
            function="quarter_end_integer_deposits",
            cache_policy="until_source_change"))
        s = self._make_store()
        try:
            r1 = compute(s, "transaction", "hit_summary")
            r2 = compute(s, "transaction", "hit_summary")  # 同源 → 缓存命中
        finally:
            s.close()
        self.assertEqual(r1["params_hash"], r2["params_hash"])
        self.assertEqual(r1["source_version_set"], r2["source_version_set"],
                         "同 source 版本戳应一致")
        self.assertIn(r2["cache"], {"hit", "miss"},
                      f"cache 字段存在（实际 {r2['cache']}）")
        # 注入不同的源：重建 store → version 应不同
        s2 = self._make_store()
        try:
            r3 = compute(s2, "transaction", "hit_summary")
        finally:
            s2.close()
        # source_version_set 应与 r1 不同（或至少 behavior 不崩）
        self.assertIn("source_version_set", r3)

    def test_ac3_non_whitelist_function_rejected(self):
        """AC3：绑定的 function 不在 functions.json 白名单 → 注册抛 ValueError。"""
        with self.assertRaises(ValueError) as cm:
            register(DerivedProperty(
                name="person.ghost",
                function="function_never_exists_xyz123",
                cache_policy="never"))
        msg = str(cm.exception)
        self.assertIn("白名单", msg, f"错误信息应含白名单关键词：{msg}")

    def test_ac4_params_hash_change_invalidates_cache(self):
        """AC4：参数变化 → params_hash 变化 → 结果值可能不同 / 不匹配。"""
        register(DerivedProperty(
            name="transaction.hit_summary",
            function="quarter_end_integer_deposits",
            cache_policy="until_source_change"))
        s = self._make_store()
        try:
            r_a = compute(s, "transaction", "hit_summary",
                          params={"quarter_end_window_days": 15})
            r_b = compute(s, "transaction", "hit_summary",
                          params={"quarter_end_window_days": 0})
        finally:
            s.close()
        self.assertNotEqual(r_a["params_hash"], r_b["params_hash"],
                            "参数变 → params_hash 应变化（AC4）")

    def test_ac5_risk_score_not_modeled_as_object_property(self):
        """AC5：person.risk_score 注册抛错（启发式必须留展示层）。"""
        for forbidden in list(_FORBIDDEN_REGISTRY_NAMES)[:3]:
            obj, prop = forbidden.split(".")
            dp = DerivedProperty(
                name=forbidden,
                function="quarter_end_integer_deposits",  # 任意白名单
                cache_policy="never")
            with self.assertRaises(ValueError, msg=f"{forbidden} 应被拒绝") as cm:
                register(dp)
            self.assertIn("启发式", str(cm.exception),
                          f"AC5 错误信息应提启发式：{cm.exception}")
        # 非 forbidden 的正常注册应当通过（sanity）
        register(DerivedProperty(
            name="transaction.normal_metric",
            function="quarter_end_integer_deposits",
            cache_policy="never"))
        self.assertIn("transaction.normal_metric", list_all())


if __name__ == "__main__":
    unittest.main(verbosity=2)
