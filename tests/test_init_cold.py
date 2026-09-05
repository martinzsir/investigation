"""
tests/test_init_cold.py
REQ-G-014 冷层建表声明推导：
  AC1 新增数据源仅在 bindings 声明即可生成对应空表
  AC2 推导出的列类型与语义层物化列类型（core TYPE_SQL）一致
  AC3 预聚合表行为不受影响（仍手工，不被推导接管）
  AC4 init_duckdb 不引入对 core 的导入依赖（避免循环依赖）
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent


def _load_init():
    spec = importlib.util.spec_from_file_location(
        "init_duckdb", ROOT / "scripts" / "init_duckdb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


init_duckdb = _load_init()


class ColdDerivationTests(unittest.TestCase):
    def test_ac4_no_core_import(self):
        import ast
        src = (ROOT / "scripts" / "init_duckdb.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("core", imported,
                         "init_duckdb 不得 import core（避免冷层→语义层循环依赖）")

    def test_default_derived_tables(self):
        tables = init_duckdb._derived_cold_tables()
        # 五个主业务表 + 两个后期接入表
        for name in ["银行流水", "通话记录", "招投标档案", "工商信息",
                     "轨迹出行", "公开OSINT", "举报材料"]:
            self.assertIn(name, tables)
        # core 自建内部表不得被冷层推导接管（主键/ON CONFLICT 归 core.lineage）
        self.assertNotIn("clue_disposal_status", tables)

    def test_ac2_types_match_semantic(self):
        from core.ontology import TYPE_SQL
        tables = init_duckdb._derived_cold_tables()
        # decimal→DOUBLE / date→DATE / integer→BIGINT 与语义层物化列同口径
        self.assertEqual(tables["银行流水"]["金额"], TYPE_SQL["decimal"])
        self.assertEqual(tables["银行流水"]["日期"], TYPE_SQL["date"])
        self.assertEqual(tables["通话记录"]["次数"], TYPE_SQL["integer"])
        self.assertEqual(tables["公开OSINT"]["采集时间"], TYPE_SQL["timestamp"])
        self.assertEqual(tables["公开OSINT"]["保留天数"], TYPE_SQL["duration_days"])

    def test_ac1_new_source_generates_empty_table(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pack = Path(td)
            (pack / "objects.json").write_text(json.dumps({
                "schema_version": 2,
                "objects": [{
                    "name": "traffic_violation", "pk": "vid", "kind": "event",
                    "name_property": "location",
                    "properties": {"person_raw": "string", "location": "string",
                                   "fine_amount": "decimal", "violation_date": "date"}}]
            }, ensure_ascii=False), encoding="utf-8")
            (pack / "bindings.json").write_text(json.dumps({
                "schema_version": 2,
                "object_bindings": [{
                    "object": "traffic_violation",
                    "source": {"table": "交通违章",
                               "columns": {"person_raw": "当事人",
                                           "location": "违法地点",
                                           "fine_amount": "罚款金额",
                                           "violation_date": "违法日期"}}}],
                "link_bindings": []
            }, ensure_ascii=False), encoding="utf-8")
            tables = init_duckdb._derived_cold_tables(pack)
            self.assertIn("交通违章", tables)
            cols = tables["交通违章"]
            self.assertEqual(cols["罚款金额"], "DOUBLE")
            self.assertEqual(cols["违法日期"], "DATE")
            # 仅在 bindings 声明即可生成空表（无 parquet）
            con = duckdb.connect(":memory:")
            cols_ddl = ", ".join(f'"{c}" {t}' for c, t in cols.items())
            con.execute(f'CREATE TABLE "交通违章" ({cols_ddl})')
            n = con.execute('SELECT COUNT(*) FROM "交通违章"').fetchone()[0]
            self.assertEqual(n, 0)
            desc = {r[0]: r[1] for r in con.execute('DESCRIBE "交通违章"').fetchall()}
            self.assertTrue(desc["罚款金额"].startswith("DOUBLE"))
            self.assertTrue(desc["违法日期"].startswith("DATE"))
            con.close()

    def test_ac3_preagg_not_derived(self):
        tables = init_duckdb._derived_cold_tables()
        for hand in ["agg_subject_month", "mv_quarterly_integer_deposits",
                     "Q1_time_window", "v_flow", "v_calls"]:
            self.assertNotIn(hand, tables)
        # 预聚合表仍由脚本手工维护
        src = (ROOT / "scripts" / "init_duckdb.py").read_text(encoding="utf-8")
        self.assertIn("agg_subject_month", src)
        self.assertIn("Q1_time_window", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
