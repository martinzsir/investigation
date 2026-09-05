"""
tests/test_empty_degrade.py
REQ-G-003 空转/结构降级：
  - SQL Function 消费的 obj_*/lnk_* 语义表缺失（CatalogException）→ 降级零命中，不抛错
  - 表在但列缺失/绑定失败（BinderException）→ 同样降级（REQ-G-003 加宽）
  - 降级落 function_empty_degraded 诊断（warning），结果带 degraded/degraded_reason
  - 非结构异常（真实 bug）不被吞，照抛
"""
from __future__ import annotations

import unittest

from core.functions import FunctionExecutor, _is_structural_degrade
from core.run_health import RunHealth
import duckdb


class EmptyDegradeTests(unittest.TestCase):
    def _make_store(self):
        from core import Store
        from core.ontology import build_ontology
        s = Store(db_path=":memory:")
        s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        s.execute("INSERT INTO 银行流水 VALUES ('张','现金存入',10000,'2021-09-28')")
        build_ontology(s.conn)
        return s

    def test_missing_semantic_table_degrades(self):
        s = self._make_store()
        h = RunHealth(s.conn)
        try:
            s.execute("DROP TABLE IF EXISTS lnk_co_located")
            fx = FunctionExecutor(s, "default", health=h)
            out = fx.invoke("co_located_pairs", {})
            diags = [r for r in h.rows() if r["kind"] == "function_empty_degraded"]
        finally:
            s.close()
        self.assertTrue(out.get("degraded"), "缺语义表应降级")
        self.assertEqual(out.get("rows"), [])
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["severity"], "warning")

    def test_missing_column_binder_degrades(self):
        s = self._make_store()
        h = RunHealth(s.conn)
        try:
            # 表在但 schema 不符（缺 person_1/person_2/location/date 列）→ BinderException
            s.execute("DROP TABLE IF EXISTS lnk_co_located")
            s.execute("CREATE TABLE lnk_co_located (wrong_col VARCHAR)")
            fx = FunctionExecutor(s, "default", health=h)
            out = fx.invoke("co_located_pairs", {})
            diags = [r for r in h.rows() if r["kind"] == "function_empty_degraded"]
        finally:
            s.close()
        self.assertTrue(out.get("degraded"), "列缺失（Binder）应降级")
        self.assertEqual(len(diags), 1)

    def test_structural_degrade_predicate(self):
        cat = duckdb.CatalogException("Table lnk_co_located does not exist")
        bnd = duckdb.BinderException("Binder Error: column \"person_1\" not found in lnk_co_located")
        other = duckdb.BinderException("Binder Error: column \"x\" not found in 银行流水")
        self.assertTrue(_is_structural_degrade(cat))
        self.assertTrue(_is_structural_degrade(bnd))
        # 引用的是非语义层（源表）→ 不算结构降级，应照抛（真实 bug）
        self.assertFalse(_is_structural_degrade(other))


if __name__ == "__main__":
    unittest.main(verbosity=2)
