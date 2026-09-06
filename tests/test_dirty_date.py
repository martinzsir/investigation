"""脏日期 TRY_CAST 降级测试（鲁棒性 B2-08/09/10 修复回归）。

修复前：语义层结构化源编译期严格 CAST，源表含 2024-02-30 / 2025-13-01 等脏值时
        DuckDB 抛 ConversionException，build_ontology 全链路中断。
修复后：编译期 TRY_CAST → 脏值降级 NULL 不中断 build；构建期按列计数进
        stats["dirty"]，经 run_health.record_build_dirty 落 run_diagnostic
        （kind=source_value_cast_failed）——不崩溃 + 降级标注，不静默丢失。
"""
import unittest

import duckdb

from core.ontology import build_ontology
from core.ontology_loader import load_pack
from core.run_health import RunHealth, record_build_dirty


class TestCompileTryCast(unittest.TestCase):
    def test_structured_source_compiles_try_cast_and_typed_raw(self):
        """默认包 transaction binding：日期/金额编译为 TRY_CAST，typed_raw 带溯源。"""
        b = load_pack("default").object_bindings["transaction"]
        self.assertIn("TRY_CAST", b.source_sql)
        self.assertNotIn(" CAST(", b.source_sql)   # 不再有严格 CAST
        self.assertIn(("date", "日期", "date"), b.typed_raw)
        # typed_raw 只收非 string 属性（string 直通不 CAST）
        self.assertTrue(all(t != "string" for _, _, t in b.typed_raw))

    def test_raw_source_sql_binding_has_no_typed_raw(self):
        """手写 source_sql（如 person）不受影响：typed_raw 为空，维持 fail-fast。"""
        b = load_pack("default").object_bindings["person"]
        self.assertEqual(b.typed_raw, ())


class TestRecordBuildDirty(unittest.TestCase):
    def test_records_diagnostic_and_returns_count(self):
        conn = duckdb.connect(":memory:")
        stats = {"dirty": ["obj_transaction.date<-日期: 2 行不可转 date（已置 NULL）"]}
        n = record_build_dirty(conn, stats, run_id="r1")
        self.assertEqual(n, 1)
        rows = conn.execute(
            "SELECT kind, severity, run_id, reason FROM run_diagnostic").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "source_value_cast_failed")
        self.assertEqual(rows[0][1], "warning")
        self.assertEqual(rows[0][2], "r1")
        self.assertIn("2 行", rows[0][3])

    def test_no_dirty_no_diagnostic(self):
        conn = duckdb.connect(":memory:")
        RunHealth(conn)   # 先建 run_diagnostic 表（无脏值时 record_build_dirty 不建表）
        self.assertEqual(record_build_dirty(conn, {"dirty": []}), 0)
        self.assertEqual(record_build_dirty(conn, None), 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM run_diagnostic").fetchone()[0], 0)


class TestEndToEndDirtyDate(unittest.TestCase):
    """端到端：VARCHAR 源表混入脏日期 → build 完成、脏值 NULL、计数与诊断齐备。"""

    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        # 全 VARCHAR 源表（覆盖 person raw UNION 引用的全部表，缺表会硬失败）
        self.conn.execute('CREATE TABLE "银行流水" ("主体" VARCHAR, "对方" VARCHAR, '
                          '"金额" VARCHAR, "日期" VARCHAR)')
        self.conn.execute('CREATE TABLE "通话记录" ("主体" VARCHAR, "对端" VARCHAR, '
                          '"日期" VARCHAR, "次数" VARCHAR)')
        self.conn.execute('CREATE TABLE "轨迹出行" ("主体" VARCHAR, "地点" VARCHAR, '
                          '"日期" VARCHAR)')
        self.conn.execute('CREATE TABLE "公开OSINT" ("主体" VARCHAR, "公开信息" VARCHAR, '
                          '"发布日期" VARCHAR, "来源" VARCHAR, "采集时间" VARCHAR, '
                          '"保留天数" VARCHAR)')
        self.conn.execute('CREATE TABLE "举报材料" ("分类" VARCHAR, "举报日期" VARCHAR, '
                          '"被举报人" VARCHAR, "举报人" VARCHAR, "内容" VARCHAR)')
        self.conn.execute('CREATE TABLE "工商信息" ("主体" VARCHAR, "法人" VARCHAR, '
                          '"状态" VARCHAR, "关联" VARCHAR)')
        self.conn.execute('CREATE TABLE "招投标档案" ("项目" VARCHAR, "中标方" VARCHAR, '
                          '"中标公示日" VARCHAR, "分管领导" VARCHAR)')
        # 5 行流水：1 行合法日期、2 行非法日期、1 行 NULL、1 行斜杠格式
        rows = [
            ("陈学勤", "王润芳", "50000", "2024-03-15"),
            ("李四", "陈学勤", "12000", "2024-02-30"),   # 不存在的日期
            ("王五", "李四", "8000", "2025-13-01"),      # 非法月份
            ("陈学勤", "王五", "3000", None),
            ("李四", "王五", "7000", "2024/03/20"),
        ]
        self.conn.executemany('INSERT INTO "银行流水" VALUES (?, ?, ?, ?)', rows)

    def test_build_survives_dirty_dates(self):
        """修复核心断言：脏日期不再中断 build；obj_transaction 全行保留。"""
        stats = build_ontology(self.conn, pack="default")   # 修复前此处抛 ConversionException
        self.assertGreater(stats["objects"].get("transaction", 0), 0)
        self.assertEqual(stats["objects"]["transaction"], 5)

    def test_dirty_values_degrade_to_null_with_audit(self):
        """脏值置 NULL、合法值保留；stats["dirty"] 与 run_diagnostic 双留痕。"""
        stats = build_ontology(self.conn, pack="default")
        date_idx = [i for i, c in enumerate(self.conn.execute(
            "SELECT * FROM obj_transaction LIMIT 0").description) if c[0] == "date"][0]
        dates = [r[date_idx] for r in self.conn.execute(
            "SELECT * FROM obj_transaction").fetchall()]
        # 3 行 NULL = 2 行非法日期降级 + 1 行源值本就是 NULL；
        # 斜杠格式 2024/03/20 DuckDB TRY_CAST 可解析（保留非 NULL，锁定该口径）
        self.assertEqual(dates.count(None), 3)
        kept = [str(d)[:10] for d in dates if d is not None]
        self.assertIn("2024-03-15", kept)
        self.assertIn("2024-03-20", kept)   # 斜杠行被正确解析
        # 计数留痕：仅 2 行真正不可转（源列非空且 TRY_CAST 为 NULL）
        self.assertEqual(len(stats["dirty"]), 1)
        self.assertIn("transaction", stats["dirty"][0])
        self.assertIn("2 行", stats["dirty"][0])
        # 诊断落账
        n = record_build_dirty(self.conn, stats, run_id="e2e")
        self.assertEqual(n, 1)
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM run_diagnostic").fetchall()]
        self.assertIn("source_value_cast_failed", kinds)

    def test_clean_rows_not_affected(self):
        """合法日期行的检测链路不受降级影响（lnk_transfers 可物化）。"""
        stats = build_ontology(self.conn, pack="default")
        self.assertIn("transfers", stats["links"])


class TestIngestCoerceCount(unittest.TestCase):
    def test_coerce_types_counts_unparseable_dates(self):
        """入库层：无法解析的日期 coerce 为 NaT 并计数（消灭静默 coerce）。"""
        import pandas as pd
        from data_ingest import _coerce_types
        df = pd.DataFrame({
            "主体": ["a", "b", "c", "d"],
            "日期": ["2024-01-01", "2024-02-30", None, "2024年3月5日"],
        })
        out = _coerce_types(df)
        self.assertEqual(out.attrs.get("coerce_lost", {}).get("日期"), 2)
        self.assertTrue(pd.isna(out["日期"].iloc[1]))
        self.assertTrue(pd.isna(out["日期"].iloc[3]))
        self.assertFalse(pd.isna(out["日期"].iloc[0]))

    def test_coerce_types_clean_dates_no_report(self):
        import pandas as pd
        from data_ingest import _coerce_types
        df = pd.DataFrame({"日期": ["2024-01-01", "2024-03-15"]})
        out = _coerce_types(df)
        self.assertNotIn("coerce_lost", out.attrs)


if __name__ == "__main__":
    unittest.main()
