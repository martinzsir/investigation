"""
tests/test_store_guard.py
REQ-003 Store 读接口直查拦截 测试。

覆盖 AC1-AC4 + 静态扫描：
  AC1: 默认 query() 直查中文业务源表 → DirectSourceAccessError；语义层放行
  AC2: unsafe=True 缺 operator/reason → ValueError
  AC3: unsafe 齐全 → 落 meta_unsafe_query 审计（operator/reason/行数）
  AC4: max_rows 生效（自动套 LIMIT）
  AC5: scripts/audit_straight_sql.py 静态扫描自身通过（见 run_tests 的 audit 组）
另：read_parquet 字符串字面量内的表名不算标识符引用（冷扫描不被误杀）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                           # noqa: E402
from core.store import (                                        # noqa: E402
    DirectSourceAccessError, touches_forbidden_table,
)
from tests.test_ontology_version import make_store              # noqa: E402


class TestStoreGuard(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        from core.ontology import build_ontology
        build_ontology(self.store.conn)

    def test_ac1_default_blocks_direct_source(self):
        """AC1: 直查源表硬拦截；obj_*/lnk_* 语义层放行。"""
        with self.assertRaises(DirectSourceAccessError):
            self.store.query("SELECT * FROM 银行流水")
        with self.assertRaises(DirectSourceAccessError):
            self.store.query("SELECT COUNT(*) AS n FROM 通话记录")
        with self.assertRaises(DirectSourceAccessError):
            self.store.query(
                "SELECT * FROM obj_person p WHERE p.raw_name IN "
                "(SELECT 主体 FROM 银行流水)")  # 子查询里的源表照样拦截

        rows = self.store.query("SELECT * FROM obj_person ORDER BY person_id")
        self.assertEqual(len(rows), 2)
        n = self.store.query("SELECT COUNT(*) AS n FROM lnk_transfers")
        self.assertEqual(n[0]["n"], 2)

    def test_ac2_unsafe_requires_operator_and_reason(self):
        """AC2: unsafe 通道必须具名操作者 + 理由，缺一即 ValueError。"""
        with self.assertRaises(ValueError):
            self.store.query("SELECT * FROM obj_person", unsafe=True)
        with self.assertRaises(ValueError):
            self.store.query("SELECT * FROM obj_person", unsafe=True, reason="排查")
        with self.assertRaises(ValueError):
            self.store.query(
                "SELECT * FROM obj_person", unsafe=True, operator="侦查员甲")
        with self.assertRaises(ValueError):
            self.store.query(
                "SELECT * FROM obj_person", unsafe=True,
                operator="侦查员甲", reason="r", max_rows=0)

    def test_ac3_unsafe_audited(self):
        """AC3: unsafe 调用落审计表，记录操作者/理由/SQL/行数。"""
        rows = self.store.query(
            "SELECT * FROM obj_person ORDER BY person_id",
            unsafe=True, reason="排查测试需要", operator="侦查员甲")
        self.assertEqual(len(rows), 2)

        audit = self.store.query("SELECT * FROM meta_unsafe_query")
        self.assertEqual(len(audit), 1)
        rec = audit[0]
        self.assertEqual(rec["operator"], "侦查员甲")
        self.assertEqual(rec["reason"], "排查测试需要")
        self.assertEqual(rec["row_count"], 2)
        self.assertIn("obj_person", rec["sql_text"])
        self.assertTrue(rec["audit_id"])

    def test_ac4_max_rows_enforced(self):
        """AC4: unsafe 通道自动套 max_rows 上限。"""
        rows = self.store.query(
            "SELECT * FROM obj_person ORDER BY person_id",
            unsafe=True, reason="r", operator="o", max_rows=1)
        self.assertEqual(len(rows), 1)
        # 已有 LIMIT 的 SQL 保持原样（不重复套）
        rows2 = self.store.query(
            "SELECT * FROM obj_person ORDER BY person_id LIMIT 1",
            unsafe=True, reason="r", operator="o", max_rows=10)
        self.assertEqual(len(rows2), 1)

    def test_literal_filename_not_flagged(self):
        """read_parquet('.../银行流水_x.parquet')：字面量内表名不算直查标识符。"""
        self.assertEqual(
            touches_forbidden_table(
                "SELECT * FROM read_parquet('data/银行流水_2024Q4.parquet')"),
            [])
        self.assertEqual(touches_forbidden_table("SELECT * FROM obj_person"), [])
        self.assertIn(
            "银行流水",
            touches_forbidden_table("SELECT * FROM 银行流水"))
        self.assertIn(
            "通话记录",
            touches_forbidden_table(
                "SELECT * FROM read_parquet('x') WHERE 1=1 UNION ALL "
                "SELECT * FROM 通话记录"))

    def test_ac5_static_scanner_detects_violations(self):
        """AC5: 静态扫描器能检出直查调用，且不误报语义层/字面量。"""
        import tempfile
        from scripts.audit_straight_sql import scan_file
        bad = (
            "from core import Store\n"
            "def f(store, s):\n"
            "    store.query('SELECT * FROM 银行流水')\n"            # 违规
            "    store.execute('SELECT * FROM 通话记录')\n"          # 违规(SELECT)
            "    s.cold_scan('x', extra_where='1=1 AND 0 < (SELECT COUNT(*) FROM 工商信息)')\n"  # 违规
            "    store.execute('CREATE TABLE 银行流水 (a INT)')\n"   # 写/DDL 放行
            "    store.query('SELECT * FROM obj_person')\n"          # 语义层合规
            "    store.query(\"SELECT * FROM read_parquet('data/银行流水_x.parquet')\")\n"  # 字面量合规
        )
        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(bad)
            tmp = Path(f.name)
        try:
            v = scan_file(tmp)
            methods_tables = {(x["method"], tuple(x["tables"])) for x in v}
            self.assertIn(("query", ("银行流水",)), methods_tables)
            self.assertIn(("execute", ("通话记录",)), methods_tables)
            self.assertIn(("cold_scan", ("工商信息",)), methods_tables)
            self.assertEqual(len(v), 3)  # 三条合规/放行不计数
        finally:
            tmp.unlink(missing_ok=True)

    def test_cold_scan_through_literal(self):
        """冷扫描 Parquet（表名在字面量内）不被拦截；具名调用走审计通道。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            con = duckdb.connect()
            parquet_path = tmpdir / "冷扫描.parquet"
            con.execute(f"""
                COPY (SELECT '张三' AS 主体, '李四' AS 对方,
                             100.0 AS 金额, '2024-01-01' AS 日期)
                TO '{parquet_path}' (FORMAT PARQUET)
            """)
            con.close()

            s = Store(root=str(tmpdir), db_path=":memory:")
            rows = s.cold_scan("冷扫描.parquet")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["主体"], "张三")

            rows2 = s.cold_scan(
                "冷扫描.parquet", operator="侦查员乙", reason="采样预演")
            self.assertEqual(len(rows2), 1)
            audit = s.query("SELECT * FROM meta_unsafe_query")
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["operator"], "侦查员乙")


if __name__ == "__main__":
    unittest.main()
