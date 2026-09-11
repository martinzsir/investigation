"""包缓存污染回归测试（DEFECT-FIX P0-1）。

缺陷：core/ontology.py 的 _compute_object_rows 把「本次源表缺失 → 裁剪 UNION 分支」
的结果就地写回 `b.source_sql`（以及 _rerender_source_sql 的降级结果）。b 来自
core/ontology_loader.load_pack() 的进程级缓存 _PACK_CACHE，而缓存指纹只取包 JSON
的 mtime —— 一次残缺构建会把「缺某一路」固化给之后的所有构建。

线上症状（cases/demoD，Web 端全量操作）：银行流水 17:48:53 完成导入，BUILD v6 在
17:48:48 启动（早 5 秒）→ person 六路 UNION 少一路；之后每次补传/重建都无效，
obj_person 恒 19 人（应为 27），只有重启 Worker 进程才恢复。

修复：
  1) P0-1  当次 effective SQL 走局部变量 src_sql，不再写回 binding；
  2) P0-1b build_ontology / materialize_changed 入口用 _localize_spec 换成本次副本。
本测试同时锁住这两层：即便有人再次写出 `b.xxx = ...`，副本层也应保住 _PACK_CACHE。
"""
import tempfile
import unittest
from pathlib import Path

import duckdb

import core.ontology_loader as ol
from core.ontology import build_ontology

_PERSON_OBJ = {
    "name": "person", "title": "人", "pk": "person_id", "kind": "entity",
    "name_property": "raw_name", "properties": {"raw_name": "string"},
}

_UNION_SQL = (
    "SELECT 主体 AS raw_name FROM 通话记录 "
    "UNION SELECT 对方 AS raw_name FROM 银行流水"
)

_PERSON_BIND = {"object": "person", "source_table": "通话记录",
                "source_sql": _UNION_SQL}

# 可选列缺列降级场景（第二条污染路径：_rerender_source_sql 写回）
_TRACK_OBJ = {
    "name": "trackpoint", "title": "轨迹", "pk": "track_id", "kind": "event",
    "name_property": "person_raw",
    "properties": {"person_raw": "string", "location": "string",
                   "source_sys": "string"},
}
_TRACK_BIND = {
    "object": "trackpoint",
    "source": {"table": "轨迹出行",
               "columns": {"person_raw": "主体", "location": "地点",
                           "source_sys": "数据来源系统"}},
    "optional_columns": ["数据来源系统"],
}


class _PackCtx:
    """临时 PACK_ROOT 上下文（与 test_ontology / test_missing_column 同口径）。"""

    def __init__(self, objects, bindings):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name) / "p"
        self.d.mkdir()
        from tests.test_ontology import _write_v2_pack
        _write_v2_pack(self.d, objects=objects, object_bindings=bindings)
        self._orig = ol.PACK_ROOT

    def __enter__(self):
        ol.PACK_ROOT = Path(self._td.name)
        ol.invalidate_pack_cache()
        return self

    def __exit__(self, *exc):
        ol.PACK_ROOT = self._orig
        ol.invalidate_pack_cache()
        self._td.cleanup()


def _person_rows(conn):
    rows = conn.execute(
        "SELECT raw_name FROM obj_person ORDER BY raw_name").fetchall()
    return [r[0] for r in rows]


class TestUnionPruneNoCachePollution(unittest.TestCase):
    """多源 UNION：源表后到的一次重建必须把该分支补回来。"""

    def setUp(self):
        self._ctx = _PackCtx([_PERSON_OBJ], [_PERSON_BIND])
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__)

    def test_late_arriving_source_is_not_lost(self):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE "通话记录" ("主体" VARCHAR, "对端" VARCHAR)')
        conn.execute("INSERT INTO \"通话记录\" VALUES ('张三', '李四')")

        # 第一次构建：银行流水还没导入 → 裁剪该分支（degraded 留痕）
        s1 = build_ontology(conn, pack="p")
        self.assertEqual(sorted(_person_rows(conn)), ["张三"])
        self.assertTrue(any("UNION" in d and "银行流水" in d
                            for d in s1["degraded"]),
                        f"首次缺表构建应有 UNION 裁剪留痕：{s1['degraded']}")

        # 第二次构建：银行流水补传到位（同进程、不清缓存）
        conn.execute('CREATE TABLE "银行流水" ("主体" VARCHAR, "对方" VARCHAR)')
        conn.execute("INSERT INTO \"银行流水\" VALUES ('王五', '赵六')")
        s2 = build_ontology(conn, pack="p")

        self.assertEqual(sorted(_person_rows(conn)), ["张三", "赵六"],
                         "补传源表后重建必须补回缺失的 UNION 分支")
        self.assertEqual(s2["degraded"], [],
                         "源表齐全时不应再有裁剪留痕")

    def test_cached_declaration_stays_intact(self):
        """多次构建后，缓存里的包声明必须仍是原文（六路/两路全在）。"""
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE "通话记录" ("主体" VARCHAR, "对端" VARCHAR)')
        conn.execute("INSERT INTO \"通话记录\" VALUES ('张三', '李四')")
        build_ontology(conn, pack="p")

        conn.execute('CREATE TABLE "银行流水" ("主体" VARCHAR, "对方" VARCHAR)')
        conn.execute("INSERT INTO \"银行流水\" VALUES ('王五', '赵六')")
        build_ontology(conn, pack="p")

        binder = ol.load_pack("p").object_bindings["person"]
        self.assertEqual(binder.source_sql, _UNION_SQL,
                         "缓存中的 source_sql 被就地改写 —— 包声明遭编译期污染")


    def test_compute_rows_does_not_mutate_binding(self):
        """直接调 byproducts 函数（绕过 _localize_spec）也必须不改动 binding。"""
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE "通话记录" ("主体" VARCHAR, "对端" VARCHAR)')
        conn.execute("INSERT INTO \"通话记录\" VALUES ('张三', '李四')")
        from core.ontology import _compute_object_rows
        spec = ol.load_pack("p")
        otype = next(o for o in spec.objects if o.name == "person")
        b = spec.object_bindings["person"]
        before = b.source_sql

        # 只挂一半源表：裁剪分支后必须原样返回 binding 的 SQL 文本
        _compute_object_rows(conn, otype, b, set(), {})
        self.assertEqual(b.source_sql, before,
                         "_compute_object_rows 改写了 binding.source_sql（P0-1 回归）")


class TestOptionalColumnRerenderNoPollution(unittest.TestCase):
    """可选列缺列降级：表重建后必须取到真实列值，而不是被固化的类型化 NULL。"""

    def setUp(self):
        self._ctx = _PackCtx([_TRACK_OBJ], [_TRACK_BIND])
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__)

    def test_column_returns_after_schema_upgrade(self):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE "轨迹出行" ("主体" VARCHAR, "地点" VARCHAR)')
        conn.execute("INSERT INTO \"轨迹出行\" VALUES ('张三', '深圳北站')")

        s1 = build_ontology(conn, pack="p")
        rows1 = conn.execute(
            "SELECT source_sys FROM obj_trackpoint").fetchall()
        self.assertEqual([r[0] for r in rows1], [None])
        self.assertTrue(any("可选源列缺失" in d for d in s1["degraded"]),
                        f"缺列应有降级留痕：{s1['degraded']}")

        # 表重建并带上缺失列（模拟重传补齐）
        conn.execute('DROP TABLE "轨迹出行"')
        conn.execute('CREATE TABLE "轨迹出行" ("主体" VARCHAR, "地点" VARCHAR,'
                     ' "数据来源系统" VARCHAR)')
        conn.execute(
            "INSERT INTO \"轨迹出行\" VALUES ('张三', '深圳北站', '交警卡口')")
        build_ontology(conn, pack="p")

        rows2 = conn.execute(
            "SELECT source_sys FROM obj_trackpoint").fetchall()
        self.assertEqual([r[0] for r in rows2], ["交警卡口"],
                         "列补齐后必须取到真实值，不应被固化的 NULL 投影卡住")


if __name__ == "__main__":
    unittest.main()
