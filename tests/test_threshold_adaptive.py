"""
tests/test_threshold_adaptive.py
REQ-027 阈值策略对象：
  - AC1：样本 ≥ min_samples → 走 adaptive 相对中位数
  - AC2：样本不足 → fallback + is_degraded=True
  - AC3：bounded_by 夹紧
  - AC4：固定 seed 下阈值可复现
  - AC5：run_rules 产出 finding 含 threshold_method / threshold_value / is_degraded 字段
  - AC6：换不同分布 → 阈值自动变化（代码未改）
"""
from __future__ import annotations

import unittest

from core.threshold import resolve_rule_params, load_thresholds


class ThresholdAdaptiveTests(unittest.TestCase):

    def setUp(self):
        # 用 Store 占位：R3 样本走 force_samples 注入，不真查库
        self.store = None  # resolve 内 store 仅传给 _collect_samples，force 给了就不用

    def test_ac1_sample_enough_uses_relative_median(self):
        """AC1：样本 ≥20（5 × median=3 × mult 2.0 = 6）。"""
        samples = [1, 1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 5, 5, 6, 7]
        # median=3 × multiplier 2.0 = 6，向上取整
        params = {"absolute_threshold": 999}
        new_p, method, val, degraded = resolve_rule_params(
            self.store, "R3", params, force_samples=samples)
        self.assertEqual(method, "relative_median", f"method 错误：{method}")
        self.assertFalse(degraded, "样本充足不应 degraded")
        self.assertEqual(new_p["absolute_threshold"], 6,
                         f"median=3 × 2.0 = 6；实际={val}，params={new_p}")
        self.assertEqual(val, 6)

    def test_ac2_sample_insufficient_fallback_degraded(self):
        """AC2：样本只有 2 个（< min_samples=20）→ fallback=30，is_degraded=True。"""
        samples = [2, 5]
        params = {"absolute_threshold": 999}
        new_p, method, val, degraded = resolve_rule_params(
            self.store, "R3", params, force_samples=samples)
        self.assertTrue(degraded, f"样本不足 20 应 degraded，实际 {degraded}")
        self.assertIn("fallback", method, f"method 应含 fallback 标记：{method}")
        self.assertEqual(new_p["absolute_threshold"], 30,
                         f"应取 fallback=30，实际 {new_p}")
        self.assertEqual(val, 30)

    def test_ac3_bounded_by_clamps(self):
        """AC3：夹紧边界生效（例如样本全部是 10000 次，median=10000×2=20000 超过 max=1000 → 截到 1000）。"""
        samples = [10000] * 25  # 25≥20，median=10000, ×2=20000
        params = {"absolute_threshold": 999}
        new_p, method, val, degraded = resolve_rule_params(
            self.store, "R3", params, force_samples=samples)
        self.assertEqual(val, 1000,
                         f"20000 > max=1000 → 应夹紧到 1000，实际={val}")
        self.assertEqual(new_p["absolute_threshold"], 1000)
        # 下界：样本全部 1（med=1 × 2 = 2 < min=5）→ 夹到 5
        samples2 = [1] * 22
        new_p2, _, val2, _ = resolve_rule_params(
            self.store, "R3", params, force_samples=samples2)
        self.assertEqual(val2, 5,
                         f"median=1 ×2=2 < min=5 → 应夹到 5，实际={val2}")
        self.assertEqual(new_p2["absolute_threshold"], 5)

    def test_ac4_fixed_seed_deterministic(self):
        """AC4：同样本 × 两次不同调用，结果逐字段一致（固定 seed）。"""
        samples = list(range(30))  # 30 个数
        results = []
        for _ in range(2):
            results.append(resolve_rule_params(
                self.store, "R3", {"absolute_threshold": 999},
                force_samples=samples, seed=42))
        self.assertEqual(results[0], results[1],
                         "固定 seed 下两次结果不一致：违反确定性 AC4")

    def test_ac5_finding_carries_threshold_fields(self):
        """AC5：run_rules 每条 finding 含 threshold_method/value/is_degraded。"""
        import sys
        sys.path.insert(0, ".")
        from tests.test_rule_overlap import _make_store
        from core.rules import run_rules
        store = _make_store()
        try:
            findings = run_rules(store, stage=None)
        finally:
            store.close()
        for f in findings:
            for k in ("threshold_method", "threshold_value", "is_degraded"):
                self.assertIn(k, f, f"finding {f['rule_id']} 缺 {k} 字段")
        # R1 为 absolute → 10000
        r1 = next(f for f in findings if f["rule_id"] == "R1")
        self.assertEqual(r1["threshold_method"], "absolute")
        self.assertEqual(r1["threshold_value"], 10000)
        self.assertFalse(r1["is_degraded"])

    def test_ac6_distribution_change_shifts_threshold(self):
        """AC6：两批不同分布，自适应阈值自动变化（代码未改）。"""
        params = {"absolute_threshold": 999}
        # 分布 A：低频次（median=3 → 6）
        samples_a = [1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 5]
        _, _, va, _ = resolve_rule_params(
            self.store, "R3", dict(params), force_samples=samples_a)
        # 分布 B：高频次（中位数 50 × 2 = 100，不超 max）
        samples_b = [10] * 5 + [50] * 10 + [100] * 5 + [200] * 2
        _, _, vb, _ = resolve_rule_params(
            self.store, "R3", dict(params), force_samples=samples_b)
        self.assertLess(va, vb,
                        f"分布变化阈值应变化：低={va} 高={vb}")
        # absolute 规则 R1：不随样本变（sanity）
        _, _, vr1, degraded_r1 = resolve_rule_params(self.store, "R1",
            {"round_unit": 10000})
        self.assertEqual(vr1, 10000)
        self.assertFalse(degraded_r1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
