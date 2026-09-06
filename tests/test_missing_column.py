"""缺列预检测试（鲁棒性 B5-01/03 修复回归）。

修复前：结构化源 binding 只处理「整表缺失」（optional 跳过），列级缺失无编译前预检——
        源表缺列时 DuckDB 裸抛 BinderException，报错不指明缺哪列、全链路中断；
        check.md S1 场景（轨迹表缺「数据来源系统」类可选列）无降级路径。
修复后：
  - 必填源列缺失 → 硬失败保留（B5-03 正确语义），但 ValueError 直指缺失列名与实际列；
  - bindings.json 声明 optional_columns 的可选源列缺失 → 该投影重渲染为类型化 NULL，
    列集/列序稳定、build 完成，stats["degraded"] + run_diagnostic(source_column_missing) 留痕。
"""
import tempfile
import unittest
from pathlib import Path

import duckdb

import core.ontology_loader as ol
from core.ontology import build_ontology
from core.run_health import RunHealth, record_build_degraded
from tests.test_ontology import _write_v2_pack

_OBJ = {
    "name": "track", "title": "轨迹", "pk": "track_id", "kind": "event",
    "name_property": "person_raw",
    "properties": {"person_raw": "string", "location": "string",
                   "date": "date", "source_sys": "string"},
}


def _bind(optional_columns=None):
    b = {"object": "track",
         "source": {"table": "轨迹出行",
                    "columns": {"person_raw": "主体", "location": "地点",
                                "date": "日期", "source_sys": "数据来源系统"}}}
    if optional_columns is not None:
        b["optional_columns"] = optional_columns
    return b


class _PackCtx:
    """临时 PACK_ROOT 上下文（与 test_ontology 同口径）。"""

    def __init__(self, objects, bindings):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name) / "p"
        self.d.mkdir()
        _write_v2_pack(self.d, objects=objects, object_bindings=bindings)
        self._orig = ol.PACK_ROOT

    def __enter__(self):
        ol.PACK_ROOT = Path(self._td.name)
        return self

    def __exit__(self, *exc):
        ol.PACK_ROOT = self._orig
        self._td.cleanup()


def _conn_with_track_table(include_optional=True, include_date=True):
    conn = duckdb.connect(":memory:")
    cols = ['"主体" VARCHAR', '"地点" VARCHAR']
    if include_date:
        cols.append('"日期" VARCHAR')
    if include_optional:
        cols.append('"数据来源系统" VARCHAR')
    conn.execute(f'CREATE TABLE "轨迹出行" ({", ".join(cols)})')
    vals = ["张三", "深圳北站"]
    if include_date:
        vals.append("2024-03-01")
    if include_optional:
        vals.append("卡口系统A")
    ph = ", ".join(["?"] * len(vals))
    conn.execute(f'INSERT INTO "轨迹出行" VALUES ({ph})', vals)
    return conn


class TestOptionalColumnDegrade(unittest.TestCase):
    def test_optional_column_missing_degrades_to_null(self):
        """B5-01：可选列缺失 → build 完成、该列类型化 NULL、degraded 留痕。"""
        with _PackCtx([_OBJ], [_bind(optional_columns=["数据来源系统"])]) as _:
            conn = _conn_with_track_table(include_optional=False)
            stats = build_ontology(conn, pack="p")
            self.assertEqual(stats["objects"].get("track"), 1)
            # obj_track 仍有 source_sys 列（列集稳定），值全为 NULL
            desc = {r[0] for r in conn.execute("DESCRIBE obj_track").fetchall()}
            self.assertIn("source_sys", desc)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM obj_track WHERE source_sys IS NOT NULL"
            ).fetchone()[0], 0)
            # 降级留痕
            self.assertEqual(len(stats["degraded"]), 1)
            self.assertIn("数据来源系统", stats["degraded"][0])
            n = record_build_degraded(conn, stats)
            self.assertEqual(n, 1)
            kinds = [r[0] for r in conn.execute(
                "SELECT kind FROM run_diagnostic").fetchall()]
            self.assertIn("source_column_missing", kinds)

    def test_optional_column_present_no_degrade(self):
        """可选列存在时正常取值，不产生降级。"""
        with _PackCtx([_OBJ], [_bind(optional_columns=["数据来源系统"])]) as _:
            conn = _conn_with_track_table(include_optional=True)
            stats = build_ontology(conn, pack="p")
            self.assertEqual(stats["degraded"], [])
            self.assertEqual(conn.execute(
                "SELECT source_sys FROM obj_track").fetchone()[0], "卡口系统A")

    def test_typed_optional_column_missing_degrades_typed_null(self):
        """可选的 date 列缺失 → CAST(NULL AS DATE)，列类型正确不崩。"""
        obj = {"name": "track", "title": "轨迹", "pk": "track_id", "kind": "event",
               "name_property": "person_raw",
               "properties": {"person_raw": "string", "location": "string",
                              "date": "date"}}
        bind = {"object": "track",
                "source": {"table": "轨迹出行",
                           "columns": {"person_raw": "主体", "location": "地点",
                                       "date": "日期"}},
                "optional_columns": ["日期"]}
        with _PackCtx([obj], [bind]) as _:
            conn = _conn_with_track_table(include_date=False)
            stats = build_ontology(conn, pack="p")
            self.assertEqual(stats["objects"].get("track"), 1)
            col_type = {r[0]: r[1] for r in conn.execute(
                "DESCRIBE obj_track").fetchall()}["date"]
            self.assertIn("DATE", col_type.upper())


class TestRequiredColumnHardFail(unittest.TestCase):
    def test_required_column_missing_raises_named_error(self):
        """B5-03：必填列缺失 → ValueError，报错直指缺失列名（不再裸抛 BinderException）。"""
        with _PackCtx([_OBJ], [_bind()]) as _:
            conn = _conn_with_track_table(include_optional=False)
            with self.assertRaises(ValueError) as cm:
                build_ontology(conn, pack="p")
            msg = str(cm.exception)
            self.assertIn("必填源列缺失", msg)
            self.assertIn("数据来源系统", msg)
            self.assertIn("轨迹出行", msg)

    def test_core_column_missing_raises_named_error(self):
        """核心必填列（日期）缺失同样硬失败且报列名。"""
        with _PackCtx([_OBJ], [_bind(optional_columns=["数据来源系统"])]) as _:
            conn = _conn_with_track_table(include_optional=False, include_date=False)
            with self.assertRaises(ValueError) as cm:
                build_ontology(conn, pack="p")
            self.assertIn("日期", str(cm.exception))


class TestOptionalBindingSkip(unittest.TestCase):
    def test_optional_binding_missing_columns_skips_not_fails(self):
        """整对象 optional:true + 源表缺列 → 跳过（旧版公开OSINT 仅 4 列场景），不硬失败。"""
        bind = _bind()
        bind["optional"] = True
        with _PackCtx([_OBJ], [bind]) as _:
            conn = _conn_with_track_table(include_optional=False)
            stats = build_ontology(conn, pack="p")
            self.assertNotIn("track", stats["objects"])
            self.assertTrue(any("源列缺失已跳过" in s for s in stats["skipped"]))


class TestOptionalColumnsValidation(unittest.TestCase):
    def test_optional_column_not_in_projection_hard_fails(self):
        """装载期：optional_columns 引用非投影源列 → 硬失败（防声明笔误）。"""
        bad = _bind(optional_columns=["不存在的列"])
        with _PackCtx([_OBJ], [bad]) as _:
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("optional_columns", str(cm.exception))

    def test_optional_columns_on_raw_sql_binding_hard_fails(self):
        """装载期：手写 source_sql 绑定不支持 optional_columns（静态不可预检）。"""
        bad = {"object": "track",
               "source_sql": "SELECT 主体 AS person_raw FROM 轨迹出行",
               "optional_columns": ["地点"]}
        with _PackCtx([_OBJ], [bad]) as _:
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("optional_columns", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
