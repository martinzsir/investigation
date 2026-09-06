"""REQ-D-013 复合列声明（composite: true 显式降级 + 画像期检出）测试。

路径 B（显式降级，声明是数据）：
  - properties 值支持映射 {"type": "string", "composite": true}（未知键 fail-closed）；
  - composite 属性必须 string 类型（整列保留，不参与 CAST）；
  - composite 不得为 name_property（AC-5：身份列不参与复合降级）；
  - composite 不参与实体关联（connectable 排除，AC-3）；
  - composite 不得作为 link normalize 的 ON 键（AC-3，编译期可判定）；
  - composite 不参与事件去重哈希（AC-5：仅复合列差异的两行同 digest 加 _02）；
  - 画像显示 declared_unsplit 状态（AC-4，无告警）。
路径 A（上游拆分）：
  - source_sql 拆分复合源列为原子字段 → 保持 1:1 可构建（AC-1）；
  - 未声明 composite 的可连接列检出分隔符复合值 → 画像告警提示出路（AC-4）。
"""
import tempfile
import unittest
from pathlib import Path

import duckdb

from core.ontology import build_ontology
from core.gateway import OntologyReadGateway
from core.ontology_profile import OntologyProfiler, connectable_props
from core.run_health import RunHealth
import core.ontology_loader as ol
from tests.test_one2one import _PackCtx
from tests.test_ontology import _write_v2_pack

_PA = {"name": "pa", "title": "主体", "pk": "pa_id", "kind": "entity",
       "name_property": "raw_name",
       "properties": {"raw_name": "string", "phone": "string",
                      "tags": {"type": "string", "composite": True}}}


def _pa_bind():
    return {"object": "pa",
            "source": {"table": "SRC",
                       "columns": {"raw_name": "主体", "phone": "电话",
                                   "tags": "标签"}}}


def _load(objects, obj_bindings, link_bindings=None, links=None):
    """临时案件包（含链接绑定）→ load_pack（装载期校验异常直接抛出）。"""
    td = tempfile.TemporaryDirectory()
    d = Path(td.name) / "p"
    d.mkdir()
    _write_v2_pack(d, objects=objects, object_bindings=obj_bindings,
                   link_bindings=link_bindings, links=links)
    orig = ol.PACK_ROOT
    ol.PACK_ROOT = Path(td.name)
    try:
        return ol.load_pack("p")
    finally:
        ol.PACK_ROOT = orig
        td.cleanup()


class TestLoaderValidation(unittest.TestCase):
    def test_mapping_form_parsed(self):
        """映射形式解析：composite 属性进 ObjectType.composite_props，类型归一 string。"""
        pack = _load([_PA], [_pa_bind()])
        o = pack.objects[0]
        self.assertEqual(o.composite_props, ("tags",))
        self.assertEqual(o.properties["tags"], "string")

    def test_composite_non_string_hard_fail(self):
        """composite 降级仅支持 string（整列保留不参与 CAST）→ 硬失败。"""
        bad = {"name": "pa", "title": "T", "pk": "pa_id", "kind": "entity",
               "name_property": "raw_name",
               "properties": {"raw_name": "string",
                              "tags": {"type": "decimal", "composite": True}}}
        with _PackCtx([bad], [{"object": "pa",
                               "source": {"table": "SRC",
                                          "columns": {"raw_name": "主体"}}}]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("composite", str(cm.exception))
            self.assertIn("REQ-D-013", str(cm.exception))

    def test_mapping_unknown_key_hard_fail(self):
        """声明映射含未知键 → fail-closed 硬失败（合法键仅 type/composite/
        data_element，其余一律拒绝）。"""
        bad = {"name": "pa", "title": "T", "pk": "pa_id", "kind": "entity",
               "name_property": "raw_name",
               "properties": {"raw_name": "string",
                              "tags": {"type": "string", "composite": True,
                                       "bogus_key": "x"}}}
        with _PackCtx([bad], [{"object": "pa",
                               "source": {"table": "SRC",
                                          "columns": {"raw_name": "主体"}}}]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("bogus_key", str(cm.exception))

    def test_composite_name_property_hard_fail(self):
        """composite 不得为 name_property（AC-5：身份列不参与复合降级）。"""
        bad = {"name": "pa", "title": "T", "pk": "pa_id", "kind": "entity",
               "name_property": "tags",
               "properties": {"tags": {"type": "string", "composite": True}}}
        with _PackCtx([bad], [{"object": "pa",
                               "source": {"table": "SRC",
                                          "columns": {"tags": "标签"}}}]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("name_property", str(cm.exception))

    def test_connectable_excludes_composite(self):
        """AC-3：composite 属性不参与实体关联（connectable_props 排除）。"""
        with _PackCtx([_PA], [_pa_bind()]):
            cp = connectable_props("p")
            self.assertIn("raw_name", cp["pa"])
            self.assertIn("phone", cp["pa"])
            self.assertNotIn("tags", cp["pa"])

    def test_normalize_on_composite_hard_fail(self):
        """AC-3：composite 属性不得作为 link normalize 的 ON 键（编译期可判定）。"""
        ev = {"name": "ev", "title": "事件", "pk": "ev_id", "kind": "event",
              "name_property": "person_raw",
              "properties": {"person_raw": "string", "tag": "string"}}
        binds = [_pa_bind(),
                 {"object": "ev", "source": {"table": "SRC",
                                             "columns": {"person_raw": "主体",
                                                         "tag": "标签"}}}]
        links = [{"name": "ln", "from_obj": "pa", "to_obj": "ev",
                  "title": "关联"}]
        lbinds = [{"link": "ln",
                   "build_sql": "SELECT p.raw_name AS who FROM obj_ev e "
                                "JOIN obj_pa p ON p.tags = e.tag",
                   "normalize": [{"as": "who", "table": "obj_pa",
                                  "alias": "p", "on": "p.tags = e.tag",
                                  "select": "p.raw_name"}]}]
        with self.assertRaises(ValueError) as cm:
            _load([_PA, ev], binds, lbinds, links)
        msg = str(cm.exception)
        self.assertIn("tags", msg)
        self.assertIn("REQ-D-013", msg)


class TestBuild(unittest.TestCase):
    def _build(self, objects, binds, rows, **src_cols):
        with _PackCtx(objects, binds):
            conn = duckdb.connect(":memory:")
            cols = ", ".join(f'"{c}" VARCHAR' for c in src_cols)
            conn.execute(f"CREATE TABLE SRC ({cols})")
            conn.executemany(
                f"INSERT INTO SRC VALUES ({', '.join('?' * len(src_cols))})",
                rows)
            stats = build_ontology(conn, pack="p")
            return conn, stats

    def test_event_hash_excludes_composite(self):
        """AC-5：仅 composite 列差异的两行同 digest → 第二行加 _02 后缀；
        复合列整列保留物化（值不丢）。"""
        trk = {"name": "trk", "title": "轨迹", "pk": "trk_id", "kind": "event",
               "name_property": "person_raw",
               "properties": {"person_raw": "string", "location": "string",
                              "tags": {"type": "string", "composite": True}}}
        bind = {"object": "trk",
                "source": {"table": "SRC",
                           "columns": {"person_raw": "主体",
                                       "location": "地点", "tags": "标签"}}}
        rows = [("张三", "项目A", "客户|供应商"),
                ("张三", "项目A", "内部")]
        conn, _ = self._build([trk], [bind], rows, 主体="x", 地点="x", 标签="x")
        keys = [r[0] for r in
                conn.execute("SELECT trk_id FROM obj_trk ORDER BY trk_id").fetchall()]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[1], keys[0] + "_02")
        tags = {r[0] for r in
                conn.execute("SELECT tags FROM obj_trk").fetchall()}
        self.assertEqual(tags, {"客户|供应商", "内部"})

    def test_source_sql_upstream_split_builds(self):
        """AC-1：source_sql 上游拆分复合源列为原子字段（split_part）→ 保持 1:1 可构建。"""
        obj = {"name": "pa", "title": "T", "pk": "pa_id", "kind": "entity",
               "name_property": "raw_name",
               "properties": {"raw_name": "string", "tag_main": "string",
                              "tag_sub": "string"}}
        bind = {"object": "pa", "source_table": "SRC",
                "source_sql": "SELECT 主体 AS raw_name, "
                              "split_part(标签, '|', 1) AS tag_main, "
                              "split_part(标签, '|', 2) AS tag_sub FROM SRC"}
        conn, stats = self._build([obj], [bind],
                                  [("张三", "客户|供应商")],
                                  主体="x", 标签="x")
        row = conn.execute(
            "SELECT raw_name, tag_main, tag_sub FROM obj_pa").fetchone()
        self.assertEqual(row, ("张三", "客户", "供应商"))
        self.assertEqual(stats["quarantine"], [])


class TestProfile(unittest.TestCase):
    def _profile(self, properties, rows):
        """构建 + 网关 + RunHealth → (report, conn)。"""
        obj = {"name": "pa", "title": "T", "pk": "pa_id", "kind": "entity",
               "name_property": "raw_name", "properties": properties}
        bind = {"object": "pa",
                "source": {"table": "SRC",
                           "columns": {"raw_name": "主体", "note": "备注"}}}
        with _PackCtx([obj], [bind]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE SRC ("主体" VARCHAR, "备注" VARCHAR)')
            conn.executemany("INSERT INTO SRC VALUES (?,?)", rows)
            build_ontology(conn, pack="p")
            gw = OntologyReadGateway(conn, pack="p")
            prof = OntologyProfiler(gw, pack="p", health=RunHealth(conn))
            report = prof.profile_all()
        return report, conn

    def _entry(self, report, prop):
        for e in report["l1_l2"]:
            if e["obj"] == "pa" and e["prop"] == prop:
                return e
        raise AssertionError(f"画像条目缺失：pa.{prop}")

    def test_undeclared_composite_value_detected_with_alert(self):
        """AC-4：未声明复合值（分隔符分片 ≥2）→ composite_suspect + 告警诊断。"""
        report, conn = self._profile(
            {"raw_name": "string", "note": "string"},
            [("张三", "手机138****0001|微信abc"),
             ("李四", "普通备注")])
        e = self._entry(report, "note")
        sus = e["composite_suspect"]
        self.assertGreaterEqual(sus["count"], 1)
        self.assertEqual(len(sus["samples"]), 1)
        self.assertTrue(all("*" in s for s in sus["samples"]))   # 样本脱敏
        self.assertIn("REQ-D-013", sus["suggestion"])
        n = conn.execute(
            "SELECT COUNT(*) FROM run_diagnostic "
            "WHERE kind='composite_column_detected'").fetchone()[0]
        self.assertEqual(n, 1)
        # 未拆分不扣分：deductions 中无 composite 相关项
        codes = {d["code"] for d in report["l5"]["deductions"]}
        self.assertNotIn("composite_column_detected", codes)

    def test_declared_composite_shows_declared_unsplit(self):
        """AC-4：声明 composite 的属性画像显示 declared_unsplit，且不触发检出告警。"""
        obj = {"name": "pa", "title": "T", "pk": "pa_id", "kind": "entity",
               "name_property": "raw_name",
               "properties": {"raw_name": "string",
                              "tags": {"type": "string", "composite": True}}}
        bind = {"object": "pa",
                "source": {"table": "SRC",
                           "columns": {"raw_name": "主体", "tags": "标签"}}}
        with _PackCtx([obj], [bind]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE SRC ("主体" VARCHAR, "标签" VARCHAR)')
            conn.executemany("INSERT INTO SRC VALUES (?,?)",
                             [("张三", "客户|供应商"), ("李四", "内部")])
            build_ontology(conn, pack="p")
            gw = OntologyReadGateway(conn, pack="p")
            prof = OntologyProfiler(gw, pack="p", health=RunHealth(conn))
            report = prof.profile_all()
        e = self._entry(report, "tags")
        self.assertEqual(e["composite"], "declared_unsplit")
        self.assertNotIn("composite_suspect", e)   # 不参与检出（非可连接）
        n = conn.execute(
            "SELECT COUNT(*) FROM run_diagnostic "
            "WHERE kind='composite_column_detected'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_single_token_not_suspect(self):
        """无分隔符/分片含空的值不误报（半角分号分片 ≥2 且全片非空才疑似）。"""
        report, _ = self._profile(
            {"raw_name": "string", "note": "string"},
            [("张三", "a;b"),          # 分片含空？否——a/b 均非空 → 疑似
             ("李四", "a;"),           # 分片含空 → 不疑似
             ("王五", "普通备注")])     # 无分隔符 → 不疑似
        e = self._entry(report, "note")
        sus = e.get("composite_suspect")
        self.assertIsNotNone(sus)
        self.assertEqual(sus["count"], 1)   # 仅 "a;b"


if __name__ == "__main__":
    unittest.main()
