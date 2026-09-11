"""鲁棒性两个内核缺口的回归测试。

缺口①：entity 型对象 name_property=NULL 的无身份行曾让 sorted 代理键分配抛
       TypeError 使整个 build 崩溃；现编译期剔除并落 stats["null_identity"]
       → run_health kind=entity_null_name_dropped（warning）。空串 "" 是确定值，
       保留（空串间可互联）。event 型按行代理键，NULL 名行保留不剔除。
缺口②：检测器整数金额判定曾用裸 CAST(amount AS BIGINT)，amount 为 Inf/越界
       DOUBLE（如脏文本 "1e400" 经 TRY_CAST AS DOUBLE 得 Inf）时抛
       ConversionException 使整条函数查询崩溃；现统一 TRY_CAST，越界行按 NULL
       被 WHERE 排除（NULL % x = NULL）。
"""
import json
import unittest
from pathlib import Path

import duckdb

from core.ontology import build_ontology
from core.run_health import record_build_null_identity
from tests.test_one2one import _PackCtx

_ENT = {"name": "person", "title": "人员", "pk": "person_id", "kind": "entity",
        "name_property": "raw_name",
        "properties": {"raw_name": "string", "note": "string"}}

_EVT = {"name": "tip", "title": "举报", "pk": "tip_id", "kind": "event",
        "name_property": "raw_name",
        "properties": {"raw_name": "string", "body": "string"}}


def _ent_bind():
    return {"object": "person",
            "source": {"table": "PER",
                       "columns": {"raw_name": "姓名", "note": "备注"}}}


def _evt_bind():
    return {"object": "tip",
            "source": {"table": "TIP",
                       "columns": {"raw_name": "分类", "body": "内容"}}}


class TestEntityNullIdentity(unittest.TestCase):
    def test_null_name_rows_dropped_and_counted(self):
        """NULL 名行剔出 obj_*，空串保留，重复行折叠；stats 按对象计数。"""
        with _PackCtx([_ENT], [_ent_bind()]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PER ("姓名" VARCHAR, "备注" VARCHAR)')
            conn.executemany(
                "INSERT INTO PER VALUES (?,?)",
                [("张三", "好行"), (None, "无身份行"),
                 ("", "空串名保留"), ("张三", "重复变体折叠")])
            stats = build_ontology(conn, pack="p")
            names = {r[0] for r in
                     conn.execute("SELECT raw_name FROM obj_person").fetchall()}
            self.assertEqual(names, {"张三", ""})
            self.assertEqual(stats["null_identity"],
                             ["obj_person.raw_name: 1 行实体名为 NULL"
                              "（无身份，不入语义层）"])

    def test_no_null_identity_when_clean(self):
        """全部有身份 → 不产生 null_identity 条目。"""
        with _PackCtx([_ENT], [_ent_bind()]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PER ("姓名" VARCHAR, "备注" VARCHAR)')
            conn.executemany("INSERT INTO PER VALUES (?,?)",
                             [("张三", "a"), ("李四", "b")])
            stats = build_ontology(conn, pack="p")
            self.assertEqual(stats["null_identity"], [])

    def test_event_null_name_retained(self):
        """event 型按行代理键：NULL 名行保留且不进 null_identity。"""
        with _PackCtx([_EVT], [_evt_bind()]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE TIP ("分类" VARCHAR, "内容" VARCHAR)')
            conn.executemany("INSERT INTO TIP VALUES (?,?)",
                             [("经济类", "x"), (None, "NULL 名事件")])
            stats = build_ontology(conn, pack="p")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM obj_tip").fetchone()[0], 2)
            self.assertEqual(stats["null_identity"], [])


class TestNullIdentityDiagnostic(unittest.TestCase):
    def test_recorder_lands_warning(self):
        """record_build_null_identity 落 run_diagnostic（kind/severity）。"""
        with _PackCtx([_ENT], [_ent_bind()]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PER ("姓名" VARCHAR, "备注" VARCHAR)')
            conn.executemany("INSERT INTO PER VALUES (?,?)",
                             [("张三", "a"), (None, "x"), (None, "y")])
            stats = build_ontology(conn, pack="p")
            n = record_build_null_identity(conn, stats, run_id="run-null-id")
            self.assertEqual(n, 1)   # 按对象聚合成 1 条（2 行）
            rows = conn.execute(
                "SELECT severity, reason FROM run_diagnostic "
                "WHERE kind='entity_null_name_dropped' AND run_id='run-null-id'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "warning")
            self.assertIn("obj_person.raw_name", rows[0][1])
            self.assertIn("2 行", rows[0][1])

    def test_recorder_zero_on_empty(self):
        self.assertEqual(record_build_null_identity(None, None), 0)
        self.assertEqual(record_build_null_identity(None, {}), 0)
        self.assertEqual(
            record_build_null_identity(None, {"null_identity": []}), 0)


class TestIntegerCastInfGuard(unittest.TestCase):
    """缺口②：TRY_CAST 越界 DOUBLE 降级 NULL，整数判定表达式不抛异常。"""

    def test_try_cast_inf_is_null(self):
        conn = duckdb.connect(":memory:")
        v = conn.execute(
            "SELECT TRY_CAST(CAST('1e400' AS DOUBLE) AS BIGINT)").fetchone()[0]
        self.assertIsNone(v)
        # NULL % 单位 = NULL → WHERE = 0 自然排除，不命中也不崩
        self.assertIsNone(
            conn.execute("SELECT TRY_CAST(CAST('1e400' AS DOUBLE) "
                         "AS BIGINT) % 10000").fetchone()[0])

    def test_default_functions_use_try_cast(self):
        """R1/R2/R6 声明 SQL 不准回退裸 CAST（防回归声明级守卫）。"""
        data = json.loads(
            Path("ontology/default/functions.json").read_text(encoding="utf-8"))
        funcs = {f["name"]: f["sql"] for f in data["functions"]}
        for name in ("quarter_end_integer_deposits",
                     "integer_transfer_aggregates",
                     "time_window_collision"):
            sql = funcs[name]
            self.assertNotIn("CAST(amount AS BIGINT)", sql)
            self.assertNotIn("CAST(l.amount AS BIGINT)", sql)
            self.assertIn("TRY_CAST", sql)


if __name__ == "__main__":
    unittest.main()
