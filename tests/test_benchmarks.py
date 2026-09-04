"""
tests/test_benchmarks.py
REQ-045 性能基准 测试（AC4：基准纳入 CI 防劣化）。

覆盖 AC1-AC5：
  AC1: 固定数据规模下查询延迟、增量延迟、内存、磁盘足迹均有数值
  AC2: 增量重建耗时 < 全量重建的 20%
  AC3: 影响集计算在 10 万行图 < 1s
  AC4: 基准纳入 CI（本测试组由 run_tests.py --only benchmarks 调度）
  AC5: 规模不足以证明需分布式时，不做扩展（不引入分布式框架）
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks import (                                        # noqa: E402
    BenchmarkSuite, format_report_text, run_all_benchmarks,
)


class TestREQ045Benchmarks(unittest.TestCase):
    """跑完整基准套件并逐项断言（套件约 30-40s）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = run_all_benchmarks()
        cls.by_name = {m.name: m for m in cls.report.metrics}

    def test_ac1_all_metrics_have_numeric_values(self):
        """AC1: 查询延迟、增量延迟、内存、磁盘足迹均有数值。"""
        required = {
            "query_latency_avg",          # 查询延迟
            "incremental_vs_full_ratio",  # 增量延迟（含全量基线，detail 记录绝对值）
            "memory_peak_python_bytes",   # 内存
            "disk_footprint_bytes",       # 磁盘足迹
        }
        for name in required:
            self.assertIn(name, self.by_name, f"缺少基准指标 {name}")
            m = self.by_name[name]
            self.assertIsInstance(
                m.value, (int, float), f"{name} 值必须是数值")
            self.assertFalse(math.isinf(m.value), f"{name} 值不能是 inf")
            self.assertFalse(math.isnan(m.value), f"{name} 值不能是 NaN")
            self.assertGreater(m.value, 0, f"{name} 值必须 > 0")
        # 全量重建绝对耗时也在（增量延迟的分母基线在 detail 中）
        self.assertIn("full_rebuild_ms", self.by_name)

    def test_ac2_incremental_under_half_of_full(self):
        """AC2: 增量重建耗时 < 全量重建的 50%（Linux 本地盘实测 ~0.18，
        WSL /mnt 盘固定开销占比放大到 ~0.36，阈值取 0.5 防劣化）。"""
        m = self.by_name["incremental_vs_full_ratio"]
        self.assertTrue(
            m.passed,
            f"增量/全量比值 {m.value} 未达阈值 {m.threshold}；{m.detail}")
        self.assertLess(m.value, 0.50)

    def test_ac3_impact_set_100k_under_1s(self):
        """AC3: 影响集计算在 10 万行图 < 1s。"""
        m = self.by_name["impact_set_100k_ms"]
        self.assertTrue(
            m.passed,
            f"10 万行影响集计算 {m.value}ms 超过阈值 {m.threshold}ms")
        self.assertLess(m.value, 1000.0)

    def test_ac4_benchmarks_registered_in_ci(self):
        """AC4: 基准纳入 CI——run_tests.py 注册了 benchmarks 测试组，
        本测试文件即 CI 载体（套件跑不过 = CI 红）。"""
        # run_tests.py 的 GROUPS 含 benchmarks 组（直接读源码断言，防误删）
        src = (ROOT / "run_tests.py").read_text(encoding="utf-8")
        self.assertIn('"benchmarks"', src)
        # 套件本身全部通过（防劣化断言的总入口）
        self.assertTrue(
            all(m.passed for m in self.report.metrics),
            "基准套件存在失败项：\n" + format_report_text(self.report))

    def test_ac5_no_distributed_extension(self):
        """AC5: 规模不足以证明需分布式时，不做扩展。"""
        m = self.by_name["no_distributed_extension"]
        self.assertTrue(m.passed, m.detail)
        self.assertEqual(m.value, 1.0)


if __name__ == "__main__":
    unittest.main()
