"""
tests/test_gateway.py
REQ-002 OntologyReadGateway 语义层唯一读入口 测试。

覆盖 AC1-AC5：
  AC1: 只接受 objects.json/links.json 声明名；中文业务表名 → UnknownObjectError
  AC2: STALE 时禁止返回旧值（StaleOntologyError），allow_stale=True 显式放行
  AC3: explain() 含 ontology_version / source_watermark / applied_policies
  AC4: 网关读取与直查 obj_* 结果等价
  AC5: 无自由 SQL 入口（dir() 无 query/execute 等方法）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.gateway import (                                     # noqa: E402
    OntologyReadGateway, UnknownObjectError, StaleOntologyError,
)
from tests.test_ontology_version import make_store              # noqa: E402


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.conn = self.store.conn
        from core.ontology import build_ontology
        build_ontology(self.conn)
        self.gw = OntologyReadGateway(self.conn)

    def test_ac1_declared_names_only(self):
        """AC1: 中文业务表名/未声明名硬失败，声明名可读。"""
        with self.assertRaises(UnknownObjectError):
            self.gw.objects("银行流水")
        with self.assertRaises(UnknownObjectError):
            self.gw.objects("通话记录")
        with self.assertRaises(UnknownObjectError):
            self.gw.links("银行流水")
        with self.assertRaises(UnknownObjectError):
            self.gw.links("not_declared")

        persons = self.gw.objects("person")
        self.assertEqual(len(persons), 2)  # 张卫国 / 李志强
        self.assertEqual({p["raw_name"] for p in persons}, {"张卫国", "李志强"})
        self.assertTrue(self.gw.links("calls_to"))
        self.assertEqual(self.gw.count("object", "person"), 2)
        self.assertGreaterEqual(self.gw.count("link", "transfers"), 2)

    def test_ac2_stale_blocks_old_values(self):
        """AC2: STALE 抛 StaleOntologyError 不返回数据；allow_stale 显式放行。"""
        self.assertEqual(self.gw.materialization_state(), "FRESH")
        # 源端推进（更晚日期的新流水）
        self.store.execute(
            "INSERT INTO 银行流水 VALUES ('测试人', '现金', 9999, '2025-06-01')")
        self.assertEqual(self.gw.materialization_state(), "STALE")

        with self.assertRaises(StaleOntologyError):
            self.gw.objects("person")
        with self.assertRaises(StaleOntologyError):
            self.gw.links("transfers")
        with self.assertRaises(StaleOntologyError):
            self.gw.count("object", "transaction")

        # 显式 allow_stale（调试留痕通道）可读旧值
        gw_stale = OntologyReadGateway(self.conn, allow_stale=True)
        self.assertEqual(len(gw_stale.objects("person")), 2)

    def test_ac3_explain_keys(self):
        """AC3: explain 三键齐备 + 策略链可审计。"""
        ex = self.gw.explain()
        self.assertIn("ontology_version", ex)
        self.assertIn("source_watermark", ex)
        self.assertIn("applied_policies", ex)
        self.assertTrue(ex["ontology_version"])
        self.assertTrue(ex["source_watermark"])
        self.assertIn("declared_names_only", ex["applied_policies"])
        self.assertIn("stale_block", ex["applied_policies"])
        self.assertIn("no_raw_sql", ex["applied_policies"])
        self.assertFalse(ex["allow_stale"])
        # plan 段含声明清单
        self.assertIn("person", ex["plan"]["declared_objects"])
        self.assertIn("transfers", ex["plan"]["declared_links"])

    def test_ac4_equivalent_to_direct_read(self):
        """AC4: 网关读取与直查 obj_person 结果等价。"""
        got = self.gw.objects("person")
        cur = self.conn.execute("SELECT * FROM obj_person")
        cols = [d[0] for d in cur.description]
        direct = [dict(zip(cols, r)) for r in cur.fetchall()]
        key = lambda r: r["person_id"]
        self.assertEqual(sorted(got, key=key), sorted(direct, key=key))

    def test_ac5_no_raw_sql_entry(self):
        """AC5: 网关不暴露任何自由 SQL 入口。"""
        for m in ("query", "execute", "raw_sql", "sql", "cold_scan"):
            self.assertFalse(
                hasattr(self.gw, m),
                f"网关不得暴露 {m}（自由 SQL 入口）")

    def test_unbuilt_state(self):
        """未构建过语义层：count/objects 不返回假数据（表缺失抛异常而非脏读）。"""
        from core import Store
        s = Store(db_path=":memory:")
        gw = OntologyReadGateway(s.conn)
        self.assertEqual(gw.materialization_state(), "UNBUILT")
        with self.assertRaises(Exception):
            gw.objects("person")


if __name__ == "__main__":
    unittest.main()
