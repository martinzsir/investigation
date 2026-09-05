"""
tests/test_entity_trace.py
REQ-G-006 实体解析留痕：
  - 组织回写缺表（DESCRIBE 失败）→ entity_table_skipped（info），不中断
  - 表在但缺目标列 → entity_table_skipped（info）
  - 正常表正常回写，不产生跳过诊断
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import duckdb

from core.entity import apply_org_to_duckdb
from core.run_health import RunHealth


class EntityTraceTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.h = RunHealth(self.con)
        # 造一个强合并映射非空的 org 替身（apply 只消费 .mapping()）
        self.org = SimpleNamespace(mapping=lambda: {"宏业建设": "宏业建设集团"})

    def tearDown(self):
        self.con.close()

    def test_missing_table_and_column_are_recorded(self):
        # 存在但缺目标列的表
        self.con.execute('CREATE TABLE t_no_col (杂项 VARCHAR)')
        applied = apply_org_to_duckdb(
            self.con, self.org,
            tables=["t_missing", "t_no_col"],   # t_missing 不存在
            name_columns=["主体", "对方"],
            health=self.h)
        self.assertEqual(applied, 0)
        skips = [r for r in self.h.rows() if r["kind"] == "entity_table_skipped"]
        # t_missing 缺表 1 条 + t_no_col 缺 主体/对方 2 条 = 3
        self.assertEqual(len(skips), 3)
        sources = {r["source"] for r in skips}
        self.assertIn("table:t_missing", sources)
        self.assertTrue(any("t_no_col" in s for s in sources))
        for r in skips:
            self.assertEqual(r["severity"], "info")

    def test_normal_table_writes_without_skip(self):
        self.con.execute('CREATE TABLE t_good (主体 VARCHAR)')
        self.con.execute("INSERT INTO t_good VALUES ('宏业建设')")
        applied = apply_org_to_duckdb(
            self.con, self.org,
            tables=["t_good"],
            name_columns=["主体"],
            health=self.h)
        self.assertEqual(applied, 1)
        # 回写后的 canonical 列取映射值
        val = self.con.execute(
            "SELECT canonical_org_主体 FROM t_good").fetchone()[0]
        self.assertEqual(val, "宏业建设集团")
        skips = [r for r in self.h.rows() if r["kind"] == "entity_table_skipped"]
        self.assertEqual(skips, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
