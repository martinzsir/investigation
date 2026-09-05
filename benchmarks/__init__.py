"""
benchmarks/__init__.py
REQ-045 性能基准模块入口。

设计原则（与方案 §4.3 劝退清单一致）：
  - 单机单线程，不引入分布式/并发框架（AC5：规模不足以证明需分布式时，不做扩展）；
  - 基准纳入 CI 防劣化（AC4）：tests/test_benchmarks.py 跑阈值断言；
  - 基准数据用 make_store() 标准夹具 + 大规模 fixture（10万行）测试影响集计算（AC3）。
"""
from benchmarks.benchmark import (
    BenchmarkResult, BenchmarkReport, BenchmarkSuite,
    run_all_benchmarks, format_report_text,
)

__all__ = [
    "BenchmarkResult",
    "BenchmarkReport",
    "BenchmarkSuite",
    "run_all_benchmarks",
    "format_report_text",
]
