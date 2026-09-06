"""REQ-D-003 代码表接入测试。

代码表（性别/币种/证件类型/案件类别…）是标准值域，由数据元 enum 承载、经
derive_code_tables 派生；enum_space（庙算侦查维度候选池）禁止手写具体人名/地名，
主体候选一律从案件数据（obj_*）派生。

  - AC-1 代码表可从数据元值域派生（derive_code_tables / load_code_tables）；
  - AC-2 enum_space 与代码表不含具体人名/地名（scan_enum_space 零命中）；
  - AC-3 非法枚举值（非字符串/空）装载期硬失败；
  - AC-4 案件级 code_tables 可追加自定义值，但标准值在前且不被覆盖/删除；
  - AC-5 代码表变更后重建，已锁定版本快照 append 保留（旧版不被改写）。
"""
import json
import unittest

import duckdb

import core.ontology_loader as ol
from core.ontology import build_ontology
from core.ontology_version import current_version
from scripts.scan_hardcoded_names import scan_enum_space
from tests.test_one2one import _PackCtx

_PERSON = {
    "name": "person", "title": "人员", "pk": "person_id", "kind": "entity",
    "name_property": "name",
    "properties": {"name": "string", "gender": "string"},
}
_PERSON_BIND = {
    "object": "person",
    "source": {"table": "人员", "columns": {"name": "姓名", "gender": "性别"}},
}


def _write(d, fname, obj):
    (d / fname).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


class TestCodeTable(unittest.TestCase):
    def test_AC1_code_tables_derived_from_data_elements(self):
        """代码表从数据元 enum 值域派生，维度名取 enum_space_dim。"""
        # default 包：4 张标准代码表
        tables = ol.load_code_tables("default")
        self.assertIn("性别", tables)
        self.assertEqual(tables["性别"], ["男", "女", "未知"])
        self.assertIn("币种", tables)
        self.assertIn("人民币", tables["币种"])
        self.assertIn("证件类型", tables)
        self.assertIn("案件类别", tables)
        self.assertIn("电信网络诈骗", tables["案件类别"])
        # 无 enum 的数据元（DE_IDCARD）不产生代码表
        self.assertNotIn("公民身份号码", tables)
        # derive_code_tables 纯函数：自定义数据元
        derived = ol.derive_code_tables({
            "DE_X": {"name": "X", "enum": ["a", "b"], "enum_space_dim": "维度"},
            "DE_NOENUM": {"name": "无值域", "type": "string"},
        })
        self.assertEqual(derived, {"维度": ["a", "b"]})

    def test_AC2_enum_space_no_hardcoded_names(self):
        """default enum_space 与派生代码表均不含人名/地名（scan 零命中）。"""
        space = ol.load_enum_space("default")
        tables = ol.load_code_tables("default")
        self.assertEqual(scan_enum_space(space), [])
        self.assertEqual(scan_enum_space(tables), [])
        # 反面：旧风格手写主体名 / 实体指称 / demo 名 → 必须命中
        self.assertTrue(scan_enum_space({"主体": ["张三", "李四"]}))
        self.assertTrue(scan_enum_space({"关系": ["王五配偶"]}))
        self.assertTrue(scan_enum_space({"备注": ["张卫国"]}))
        self.assertTrue(scan_enum_space({"人物": ["赵六"]}))

    def test_AC3_invalid_enum_value_hard_fails(self):
        """enum_space 枚举值非字符串 / 空 → 装载期硬失败。"""
        with _PackCtx([_PERSON], [_PERSON_BIND]) as pc:
            _write(pc.d, "enum_space.json",
                   {"schema_version": 2,
                    "space": {"行为": ["现金收受", 123]}})  # 数字非字符串
            with self.assertRaises(ValueError) as cm:
                ol.load_enum_space("p")
            self.assertIn("REQ-D-003", str(cm.exception))
        with _PackCtx([_PERSON], [_PERSON_BIND]) as pc:
            _write(pc.d, "enum_space.json",
                   {"schema_version": 2,
                    "space": {"行为": ["现金收受", "  "]}})  # 空白字符串
            with self.assertRaises(ValueError):
                ol.load_enum_space("p")

    def test_AC4_case_append_does_not_override_standard(self):
        """案件级 code_tables 追加值在标准值之后，标准值不被覆盖/删除/去重错位。"""
        with _PackCtx([_PERSON], [_PERSON_BIND]) as pc:
            _write(pc.d, "data_elements.json",
                   {"schema_version": 2, "elements": {
                       "DE_CASE": {"name": "案件类别", "type": "string",
                                   "enum": ["诈骗", "洗钱"],
                                   "enum_space_dim": "案件类别"}}})
            _write(pc.d, "enum_space.json",
                   {"schema_version": 2,
                    "space": {"行为": ["现金收受"]},
                    "code_tables": {"案件类别": ["网络赌博", "诈骗"]}})  # 含重复标准值
            tables = ol.load_code_tables("p")
            vals = tables["案件类别"]
            # 标准值在前且保留
            self.assertEqual(vals[:2], ["诈骗", "洗钱"])
            # 追加值在后
            self.assertIn("网络赌博", vals)
            self.assertEqual(vals.index("网络赌博"), 2)
            # 重复追加的标准值不产生重复项
            self.assertEqual(vals.count("诈骗"), 1)

    def test_AC5_snapshot_append_only_on_code_table_change(self):
        """代码表变更后重建：版本记录 append，旧快照 is_current=false 仍保留。"""
        with _PackCtx([_PERSON], [_PERSON_BIND]) as pc:
            _write(pc.d, "data_elements.json",
                   {"schema_version": 2, "elements": {
                       "DE_CASE": {"name": "案件类别", "type": "string",
                                   "enum": ["诈骗", "洗钱"],
                                   "enum_space_dim": "案件类别"}}})
            conn = duckdb.connect()
            conn.execute("CREATE TABLE 人员 (姓名 VARCHAR, 性别 VARCHAR)")
            conn.execute("INSERT INTO 人员 VALUES ('张三', '男')")
            build_ontology(conn, pack="p")
            v1 = current_version(conn, "p")
            self.assertIsNotNone(v1)

            # 仅改代码表（数据元值域追加），不动已物化数据
            _write(pc.d, "data_elements.json",
                   {"schema_version": 2, "elements": {
                       "DE_CASE": {"name": "案件类别", "type": "string",
                                   "enum": ["诈骗", "洗钱", "网络赌博"],
                                   "enum_space_dim": "案件类别"}}})
            build_ontology(conn, pack="p")
            v2 = current_version(conn, "p")

            # 版本表 append-only：两行共存
            rows = conn.execute(
                "SELECT build_id, is_current FROM meta_ontology_state "
                "WHERE pack='p' ORDER BY built_at").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertFalse(rows[0][1])   # 旧版锁定 is_current=false
            self.assertTrue(rows[1][1])    # 新版 current
            self.assertEqual(rows[0][0], v1.build_id)
            self.assertEqual(rows[1][0], v2.build_id)
            self.assertNotEqual(v1.build_id, v2.build_id)


if __name__ == "__main__":
    unittest.main()
