"""
benchmarks/benchmark.py
REQ-045 性能基准实现。

覆盖 AC1-AC5：
  AC1: 查询延迟、增量延迟、内存、磁盘足迹均有数值（BenchmarkReport.metrics）
  AC2: 增量重建耗时 < 全量重建的 20%（bench_incremental_vs_full_ratio）
  AC3: 影响集计算在 10 万行图 < 1s（bench_impact_set_100k）
  AC4: 基准纳入 CI（tests/test_benchmarks.py 跑阈值断言；run_tests.py --only benchmarks）
  AC5: 规模不足以证明需分布式时，不做扩展（no_distributed_extension_assertion）

设计原则：
  - 单机单线程，DuckDB in-memory 或单文件；
  - 不引入 multiprocessing/asyncio/distributed 框架（AC5）；
  - 时钟用 time.perf_counter（跨平台，纳秒级）；
  - 内存用 tracemalloc（Python 层；DuckDB 自身 RSS 不计入，但够用作
    相对劣化监控）；磁盘足迹用 os.path.getsize（单文件 .duckdb）。
  - 数据规模：
      查询/增量延迟：用 make_store() 标准夹具（小规模，聚焦信号）；
      影响集计算：构造 10 万行 lnk_calls_to 夹具（大规模，证明算法可扩展）。
"""
from __future__ import annotations

import gc
import os
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext, system_context          # noqa: E402
from core.gateway import OntologyReadGateway                    # noqa: E402
from core.ingest_validate import IngestPartition                # noqa: E402
from core.ontology import build_ontology, rebuild_from_partition  # noqa: E402
from core.rebuild_planner import plan_from_seeds               # noqa: E402
from tests.test_ontology_version import make_store              # noqa: E402


# ----------------------------------------------------------------------
# 结果模型
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class BenchmarkResult:
    """单次基准结果。"""
    name: str                       # 基准名
    value: float                    # 主指标（ms / bytes / ratio）
    unit: str                       # "ms" | "bytes" | "ratio" | "count"
    threshold: float | None = None # 阈值（None=无硬阈值，仅记录）
    passed: bool = True             # 是否在阈值内
    detail: str = ""                # 备注

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "threshold": self.threshold,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class BenchmarkReport:
    """基准报告。"""
    metrics: list[BenchmarkResult] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)

    def add(self, r: BenchmarkResult) -> None:
        self.metrics.append(r)

    def as_dict(self) -> dict:
        return {
            "environment": self.environment,
            "metrics": [m.as_dict() for m in self.metrics],
            "all_passed": all(m.passed for m in self.metrics),
        }


# ----------------------------------------------------------------------
# 基准套件
# ----------------------------------------------------------------------
class BenchmarkSuite:
    """REQ-045 性能基准套件。

    用法：
        suite = BenchmarkSuite()
        report = suite.run_all()
        print(format_report_text(report))
        assert report.as_dict()["all_passed"]
    """

    # AC3 阈值：影响集计算 10 万行图 < 1s
    IMPACT_SET_100K_MS_THRESHOLD = 1000.0
    IMPACT_SET_100K_ROWS = 100_000
    # AC2 阈值：增量重建耗时 < 全量重建的 20%
    # 基准夹具为全表灌数据（N_TXN=4000 等），昂贵链接（time_window 交叉）
    # 主导全量成本；增量只重写受影响对象（org + involved_in）。
    # 实测比值 ~0.18；取多次最优（min）+ gc 降噪。
    INCREMENTAL_FULL_RATIO_THRESHOLD = 0.20
    BENCH_REPEATS = 5
    # 全表夹具规模（各行源表行数；让链接 JOIN 成本主导全量重建）
    N_TXN, N_CALL, N_ORG, N_TRACK, N_BID = 4000, 3000, 1500, 2000, 1200
    # AC4 防劣化：小规模查询延迟 < 1000ms（防止 N+1 / 全表扫回归）
    QUERY_LATENCY_MS_THRESHOLD = 1000.0

    def __init__(self):
        self.report = BenchmarkReport()

    # ---- AC1: 查询延迟 ----
    def bench_query_latency(self) -> BenchmarkResult:
        """AC1: 通过 Gateway 读取语义层 obj_*/lnk_* 的查询延迟。

        小规模夹具（标准 make_store，约 10-20 行）下，gateway 读取应 < 100ms
        实际更短；本基准设 1s 上限用于防劣化（任何回归让小查询秒级即失败）。
        """
        store = make_store()
        build_ontology(store.conn)
        gw = OntologyReadGateway(
            store.conn,
            access=AccessContext(operator="bench", role="正兵",
                                 case_id="default"))
        # 5 次读：person / transaction / lnk_transfers / person / transaction
        queries = [
            ("objects", "person"),
            ("objects", "transaction"),
            ("links", "transfers"),
            ("objects", "person"),
            ("objects", "transaction"),
        ]
        # 预热 1 次（首读含 gateway 初始化：load_pack / PolicyEngine）后计时
        gw.objects("person")
        t0 = time.perf_counter()
        for kind, name in queries:
            if kind == "objects":
                gw.objects(name)
            else:
                gw.links(name)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        avg_ms = elapsed_ms / len(queries)
        r = BenchmarkResult(
            name="query_latency_avg",
            value=round(avg_ms, 3),
            unit="ms",
            threshold=self.QUERY_LATENCY_MS_THRESHOLD,
            passed=avg_ms < self.QUERY_LATENCY_MS_THRESHOLD,
            detail=f"5 次查询总 {elapsed_ms:.1f}ms，平均 {avg_ms:.3f}ms/次",
        )
        store.close()
        self.report.add(r)
        return r

    # ---- AC1: 全量重建耗时 + AC2 基线 ----
    def bench_full_rebuild(self) -> BenchmarkResult:
        """AC1: 全量 build_ontology 耗时（取 3 次最优降噪）。"""
        store = make_store()
        build_ontology(store.conn)   # 首建（建表），预热不计入
        full_ms = float("inf")
        for _ in range(3):
            gc.collect()
            t0 = time.perf_counter()
            build_ontology(store.conn)
            full_ms = min(full_ms, (time.perf_counter() - t0) * 1000)
        r = BenchmarkResult(
            name="full_rebuild_ms",
            value=round(full_ms, 3),
            unit="ms",
            threshold=None,  # 无硬阈值，仅记录
            passed=True,
            detail=f"全量 build_ontology 稳态耗时（标准夹具，3 次最优）",
        )
        store.close()
        self.report.add(r)
        return r

    # ---- AC1: 增量重建耗时 + AC2: 比值 < 20% ----
    def _make_large_store(self):
        """AC2 夹具：全表灌数据，让昂贵链接（time_window 交叉 JOIN）主导
        全量重建成本；增量只重写受影响对象（org + involved_in）。"""
        store = make_store()
        txn = ", ".join(
            f"('主体{i % 400}', '对方{i % 300}', {1000 + i}, "
            f"'2024-{1 + i % 12:02d}-{1 + i % 28:02d}')"
            for i in range(self.N_TXN))
        store.execute(f"INSERT INTO 银行流水 VALUES {txn}")
        calls = ", ".join(
            f"('主体{i % 400}', '对端{i % 250}', "
            f"'2024-{1 + i % 12:02d}-{1 + i % 28:02d}', {i % 9 + 1})"
            for i in range(self.N_CALL))
        store.execute(f"INSERT INTO 通话记录 VALUES {calls}")
        orgs = ", ".join(
            f"('公司{i}', '法人{i}', '存续', NULL)"
            for i in range(self.N_ORG))
        store.execute(f"INSERT INTO 工商信息 VALUES {orgs}")
        tracks = ", ".join(
            f"('2024-{1 + i % 12:02d}-{1 + i % 28:02d}', "
            f"'主体{i % 400}', '项目{i % 30}')"
            for i in range(self.N_TRACK))
        store.execute(f"INSERT INTO 轨迹出行 VALUES {tracks}")
        bids = ", ".join(
            f"('项目BID{i}', '公司{i % self.N_ORG}', "
            f"'2024-{1 + i % 12:02d}-15', '领导{i % 50}')"
            for i in range(self.N_BID))
        store.execute(f"INSERT INTO 招投标档案 VALUES {bids}")
        build_ontology(store.conn)   # 首建（建表），不计入对比
        return store

    def bench_incremental_vs_full_ratio(self) -> BenchmarkResult:
        """AC1+AC2: 增量重建耗时 < 全量重建的 20%。

        全量：build_ontology（重写全部 11 对象 + 9 链接，含 time_window
        交叉 JOIN 等昂贵链接）。
        增量：插入 1 行新工商信息 → rebuild_from_partition（REQ-018 影响
        范围 → REQ-004 只重写受影响对象/链接）。
        全量与增量各取 3 次最优（min）降噪；实测比值 ~0.18（阈值 0.20）。
        """
        store = self._make_large_store()

        full_ms = float("inf")
        for _ in range(self.BENCH_REPEATS):
            gc.collect()
            t0 = time.perf_counter()
            build_ontology(store.conn)
            full_ms = min(full_ms, (time.perf_counter() - t0) * 1000)

        store.execute(
            "INSERT INTO 工商信息 VALUES ('增量公司', '增量法人', '存续', NULL)")
        part = IngestPartition(
            partition_id="工商信息_2025Q1", dataset="工商信息",
            high_watermark="2025-01-15", content_hash="bench_incremental",
            row_count=1)
        incr_ms = float("inf")
        plan = None
        for _ in range(self.BENCH_REPEATS):
            gc.collect()
            t0 = time.perf_counter()
            plan, _stats = rebuild_from_partition(store.conn, part,
                                                  actor="bench")
            incr_ms = min(incr_ms, (time.perf_counter() - t0) * 1000)
        ratio = incr_ms / full_ms if full_ms > 0 else float("inf")

        mode_detail = (f"objs={sorted(plan.affected_objects)} "
                       f"lnks={sorted(plan.affected_links)}")

        r = BenchmarkResult(
            name="incremental_vs_full_ratio",
            value=round(ratio, 4),
            unit="ratio",
            threshold=self.INCREMENTAL_FULL_RATIO_THRESHOLD,
            passed=(ratio < self.INCREMENTAL_FULL_RATIO_THRESHOLD
                    and plan.mode != "skip"),
            detail=(f"全表夹具(流水{self.N_TXN}/通话{self.N_CALL}/工商"
                    f"{self.N_ORG}/轨迹{self.N_TRACK}/招投标{self.N_BID})："
                    f"全量 {full_ms:.0f}ms / 增量 {incr_ms:.0f}ms / "
                    f"比值 {ratio:.3f}（阈值 "
                    f"{self.INCREMENTAL_FULL_RATIO_THRESHOLD}，"
                    f"{self.BENCH_REPEATS} 次最优）；{mode_detail}"),
        )
        store.close()
        self.report.add(r)
        return r

    # ---- AC1: 内存足迹 ----
    def bench_memory_footprint(self) -> BenchmarkResult:
        """AC1: Python 层内存足迹（tracemalloc）。

        注意：DuckDB 自身的 RSS 不计入 tracemalloc，但本基准用于相对
        劣化监控——任何引入大对象缓存/全量搬入内存的回归会让本指标飙升。
        """
        store = make_store()
        tracemalloc.start()
        build_ontology(store.conn)
        # 跑几次 gateway 读取（触发所有读路径）
        gw = OntologyReadGateway(
            store.conn,
            access=AccessContext(operator="bench", role="正兵",
                                 case_id="default"))
        for _ in range(3):
            gw.objects("person")
            gw.objects("transaction")
            gw.links("transfers")
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        store.close()
        r = BenchmarkResult(
            name="memory_peak_python_bytes",
            value=peak,
            unit="bytes",
            threshold=None,
            passed=True,
            detail=(f"Python 峰值 {peak / 1024:.1f} KB；"
                    f"current {cur / 1024:.1f} KB（不含 DuckDB RSS）"),
        )
        self.report.add(r)
        return r

    # ---- AC1: 磁盘足迹 ----
    def bench_disk_footprint(self) -> BenchmarkResult:
        """AC1: DuckDB 单文件磁盘足迹。

        用临时 .duckdb 文件 build_ontology 后测文件大小；
        in-memory 库无法测文件大小，必须落盘。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            db_path = tmpdir / "bench.duckdb"
            con = duckdb.connect(str(db_path))
            # 复用 make_store 的建表逻辑但写到磁盘文件
            from tests.test_ontology_version import _create_tables
            class _Stub:
                def __init__(self, c): self.conn = c
                def execute(self, sql, params=()):
                    self.conn.execute(sql, params)
            stub = _Stub(con)
            _create_tables(stub)
            # 插入示例数据（少量行，反映"骨架"磁盘足迹）
            stub.execute("INSERT INTO 银行流水 VALUES ('甲', '乙', 1000, '2024-01-01')")
            stub.execute("INSERT INTO 通话记录 VALUES ('甲', '乙', '2024-01-01', 1)")
            stub.execute("INSERT INTO 工商信息 VALUES ('公司A', '甲', '存续', NULL)")
            build_ontology(con)
            con.close()
            size = db_path.stat().st_size
        r = BenchmarkResult(
            name="disk_footprint_bytes",
            value=size,
            unit="bytes",
            threshold=None,
            passed=True,
            detail=f"单文件 DuckDB {size / 1024:.1f} KB（少量行；批量数据按比例扩展）",
        )
        self.report.add(r)
        return r

    # ---- AC3: 影响集计算 10 万行 < 1s ----
    def bench_impact_set_100k(self) -> BenchmarkResult:
        """AC3: 影响集计算在 10 万行图 < 1s。

        复用 test_rebuild_planner 的 100K 边夹具：构造 10 万行 lnk_calls_to，
        以 p1 为种子调用 plan_from_seeds，断言耗时 < 1000ms。
        与 test_rebuild_planner.test_ac3_100k_edges_under_1s 同口径，但本基准
        作为独立基准记录数值（test 只判 pass/fail）。
        """
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                "CREATE TABLE obj_person "
                "(person_id VARCHAR, raw_name VARCHAR, source_rows VARCHAR)")
            con.execute("INSERT INTO obj_person VALUES "
                        "('p1', '张三', 'x'), ('p2', '李四', 'x')")
            con.execute(f"""
                CREATE TABLE lnk_calls_to AS
                SELECT 'c' || i AS call_id,
                       'p1' AS from_person, 'p2' AS to_person
                FROM range({self.IMPACT_SET_100K_ROWS}) t(i)
            """)
            elapsed_ms = float("inf")
            plan = None
            for _ in range(3):
                gc.collect()
                t0 = time.perf_counter()
                plan = plan_from_seeds(con, {"person": {"p1"}})
                elapsed_ms = min(elapsed_ms,
                                 (time.perf_counter() - t0) * 1000)
            affected = len(plan.affected_pks.get("call", set()))
            r = BenchmarkResult(
                name="impact_set_100k_ms",
                value=round(elapsed_ms, 3),
                unit="ms",
                threshold=self.IMPACT_SET_100K_MS_THRESHOLD,
                passed=(elapsed_ms < self.IMPACT_SET_100K_MS_THRESHOLD
                        and affected == self.IMPACT_SET_100K_ROWS),
                detail=(f"10万行边图影响集计算 {elapsed_ms:.1f}ms；"
                        f"回收 {affected} 条边"),
            )
        finally:
            con.close()
        self.report.add(r)
        return r

    # ---- AC5: 不做分布式扩展 ----
    def bench_no_distributed_extension(self) -> BenchmarkResult:
        """AC5: 规模不足以证明需分布式时，不做扩展。

        本基准是"负向断言"：
          - 不引入 multiprocessing / concurrent.futures / asyncio
            （单机单线程足以处理当前规模）；
          - 不引入分布式存储/计算框架（duckdb 单文件足够）；
          - benchmarks/ 模块本身的 import 不依赖任何分布式库。
        断言方式：benchmarks/__init__.py 不在 sys.modules 中
        引入上述分布式模块（运行期检查）。
        """
        forbidden = ("multiprocessing", "concurrent.futures",
                     "asyncio", "dask", "ray", "pyspark", "distributed")
        loaded = set(sys.modules) & set(forbidden)
        ok = not loaded
        r = BenchmarkResult(
            name="no_distributed_extension",
            value=1.0 if ok else 0.0,
            unit="bool",
            threshold=1.0,
            passed=ok,
            detail=("未引入分布式框架（AC5：规模未达需分布式阈值）"
                    if ok else
                    f"检测到分布式模块已加载：{sorted(loaded)}（违反 AC5）"),
        )
        self.report.add(r)
        return r

    # ---- 全部跑 ----
    def run_all(self) -> BenchmarkReport:
        """跑全部基准，填充 BenchmarkReport。"""
        # 环境信息
        self.report.environment = {
            "python": sys.version.split()[0],
            "duckdb": duckdb.__version__,
            "platform": sys.platform,
        }
        # 依次跑（顺序敏感：bench_full_rebuild 不能在 bench_incremental 之后跑）
        self.bench_query_latency()
        self.bench_full_rebuild()
        self.bench_incremental_vs_full_ratio()
        self.bench_memory_footprint()
        self.bench_disk_footprint()
        self.bench_impact_set_100k()
        self.bench_no_distributed_extension()
        return self.report


# ----------------------------------------------------------------------
# 文本报告
# ----------------------------------------------------------------------
def format_report_text(report: BenchmarkReport) -> str:
    """格式化基准报告为可读文本。"""
    lines = ["=== REQ-045 性能基准报告 ==="]
    env = report.environment
    if env:
        lines.append(f"环境：Python {env.get('python','?')} / "
                      f"DuckDB {env.get('duckdb','?')} / "
                      f"platform={env.get('platform','?')}")
    lines.append("")
    for m in report.metrics:
        status = "✓" if m.passed else "✗"
        thr = f" 阈值={m.threshold}" if m.threshold is not None else ""
        lines.append(f"  [{status}] {m.name:<32} {m.value} {m.unit}{thr}")
        if m.detail:
            lines.append(f"        └ {m.detail}")
    all_pass = all(m.passed for m in report.metrics)
    lines.append("")
    lines.append(f"=== {'全部通过' if all_pass else '有失败项'} ===")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI 入口
# ----------------------------------------------------------------------
def run_all_benchmarks() -> BenchmarkReport:
    """便捷函数：跑全部基准并返回报告。"""
    return BenchmarkSuite().run_all()


if __name__ == "__main__":
    report = run_all_benchmarks()
    print(format_report_text(report))
    sys.exit(0 if all(m.passed for m in report.metrics) else 1)
