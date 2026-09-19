"""
tests/test_decl_config.py
REQ-G-011/012/013 声明化（声明是数据）：
  - G-011 dimensions.json：维度名从声明读；规则引用未声明 dimension → 装载期硬失败；
    新增维度不改 Python 即被规则引用、参与覆盖度
  - G-012 enum_space.json：枚举空间从声明读；自定义 space 传参仍覆盖
  - P6 五间解耦：objects/links 已删除 jian/jian_source 字段、案件包删除
    jians.json；底座不认识间类。规则 jian_types 是不透明注解（保留不校验），
    词汇合法性由 packs/wujian（core/wujian）负责
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.ontology_loader import (load_pack, load_dimensions, load_enum_space,
                                  DEFAULT_DIMENSIONS)
from core.hypotheses import MiaoSuan

_RULE_TEXT = ("当对象出现某模式且无合法业务对价时命中，用于刻画反常资金往来，"
              "排除正常工资/还款等周期性交易")


def _write_pack(root: Path, *, dimensions=None, enum_space=None,
                rule_dimension="资金",
                rule_function="f_ping", rule_jian=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects.json").write_text(json.dumps({
        "schema_version": 2,
        "objects": [
            {"name": "foo", "pk": "foo_id", "kind": "entity",
             "name_property": "foo_id",
             "properties": {"name": "string"}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "links.json").write_text(json.dumps({
        "schema_version": 2,
        "links": [
            {"name": "fw", "from_obj": "foo", "to_obj": "foo",
             "runtime": True,
             "properties": {}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "bindings.json").write_text(json.dumps({
        "schema_version": 2,
        "object_bindings": [
            {"object": "foo",
             "source_sql": "SELECT 1 AS foo_id, 'x' AS name",
             "optional": True},
        ],
        "link_bindings": []}, ensure_ascii=False), encoding="utf-8")
    (root / "actions.json").write_text(
        json.dumps({"schema_version": 2, "actions": []}), encoding="utf-8")
    (root / "functions.json").write_text(json.dumps({
        "schema_version": 2,
        "functions": [
            {"name": "f_ping", "title": "探测", "inputs": ["obj_foo"],
             "parameters": {}, "output_type": "rows", "impl": "sql",
             "sql": "SELECT 1 AS x", "description": "最小探测函数"},
        ]}, ensure_ascii=False), encoding="utf-8")
    rule = {
        "id": "R99", "stage": "xu_shi", "title": "探测规则",
        "rule_text": _RULE_TEXT, "hit_when": "rows_nonempty",
        "function": rule_function, "dimension": rule_dimension,
        "params": {}, "assumption": "",
        **({"jian_types": rule_jian} if rule_jian else {}),
    }
    (root / "rules.json").write_text(json.dumps({
        "schema_version": 2, "rules": [rule] if rule_dimension is not None else []},
        ensure_ascii=False), encoding="utf-8")
    if dimensions is not None:
        (root / "dimensions.json").write_text(json.dumps(
            {"schema_version": 2, "dimensions": dimensions}, ensure_ascii=False),
            encoding="utf-8")
    if enum_space is not None:
        (root / "enum_space.json").write_text(json.dumps(
            {"schema_version": 2, "space": enum_space}, ensure_ascii=False),
            encoding="utf-8")


class DimensionsDeclTests(unittest.TestCase):
    def test_default_pack_dimensions_from_declaration(self):
        dims = load_dimensions("default")
        self.assertEqual(dims, ["资金", "通讯", "行为", "关系", "时间"])
        # MiaoSuan 维度来自声明（实例属性覆盖类默认）
        m = MiaoSuan()
        self.assertEqual(m.DIMENSIONS, dims)

    def test_missing_dimensions_file_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, dimensions=None)
            # 无 dimensions.json → 回落内置 5 维
            self.assertEqual(load_dimensions("p", base_dir=Path(td)),
                             DEFAULT_DIMENSIONS)

    def test_undeclared_dimension_hard_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, rule_dimension="心理",
                        dimensions=[{"name": "资金", "note": "",
                                     "source_object_types": ["foo"]}])
            with self.assertRaises(ValueError) as ctx:
                load_pack("p", base_dir=Path(td))
            self.assertIn("dimension", str(ctx.exception))

    def test_new_dimension_without_code_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, rule_dimension="心理",
                        dimensions=[{"name": "资金", "note": "",
                                     "source_object_types": ["foo"]},
                                    {"name": "心理", "note": "新增维度",
                                     "source_object_types": ["foo"]}])
            spec = load_pack("p", base_dir=Path(td))  # 不抛
            self.assertEqual(spec.rules["R99"].dimension, "心理")
            dims = load_dimensions("p", base_dir=Path(td))
            self.assertIn("心理", dims)
            # 新维度即被覆盖度模型消费：分母随声明变化（2 维），无需改 Python
            m = MiaoSuan()
            m.DIMENSIONS = dims
            self.assertEqual(m.DIMENSIONS, dims)
            dc = m.dimension_coverage()
            self.assertEqual(set(dc["missing"]), {"资金", "心理"})
            self.assertEqual(dc["score"], 0.0)


class EnumSpaceDeclTests(unittest.TestCase):
    def test_default_enum_space_from_declaration(self):
        space = load_enum_space("default")
        self.assertIsNotNone(space)
        # REQ-D-003：内置枚举空间禁止手写人名主体维度（主体从案件数据派生）
        self.assertNotIn("主体", space)
        self.assertIn("行为", space)
        m = MiaoSuan()
        self.assertEqual(m.ENUM_SPACE, space)

    def test_enum_space_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(load_enum_space("nope", base_dir=Path(td)))

    def test_new_enum_value_no_code_change_and_override(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, enum_space={"主体": ["新主体"],
                                          "行为": ["新行为"]})
            space = load_enum_space("p", base_dir=Path(td))
            self.assertEqual(space["主体"], ["新主体"])
            m = MiaoSuan()
            m.ENUM_SPACE = space
            out = m.enumerate_space()
            self.assertEqual(out["total_combos"], 1)  # 1×1
            # 传参覆盖仍生效
            out2 = m.enumerate_space({"主体": ["a", "b"], "行为": ["x"]})
            self.assertEqual(out2["total_combos"], 2)


class BaseWithoutJianTests(unittest.TestCase):
    """P6：底座不认识间类——案件包无 jians.json、objects/links 无 jian 字段。"""

    def setUp(self):
        from core.wujian import reset_wujian
        reset_wujian()

    def tearDown(self):
        from core.wujian import reset_wujian
        reset_wujian()

    def test_pack_loads_without_jians_file(self):
        """无 jians.json、无 jian 字段 → 案件包正常装载（底座独立）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root)
            spec = load_pack("p", base_dir=Path(td))   # 不抛
            self.assertEqual([o.name for o in spec.objects], ["foo"])
            self.assertEqual([l.name for l in spec.links], ["fw"])

    def test_objects_links_have_no_jian(self):
        """ObjectType/LinkType 已无 jian/jian_source 属性。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root)
            spec = load_pack("p", base_dir=Path(td))
            self.assertFalse(hasattr(spec.objects[0], "jian"))
            self.assertFalse(hasattr(spec.objects[0], "jian_source"))
            self.assertFalse(hasattr(spec.links[0], "jian"))

    def test_rule_jian_tag_is_opaque(self):
        """规则 jian_types 是不透明注解：底座不校验，标签原样保留。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, rule_jian=["生间", "自造间"])
            spec = load_pack("p", base_dir=Path(td))
            self.assertEqual(spec.rules["R99"].jian_types,
                             ("生间", "自造间"))

    def test_default_pack_has_no_jians_file(self):
        """default 案件包不再含 jians.json（词汇唯一权威在 packs/wujian）。"""
        default_dir = Path(__file__).resolve().parent.parent / \
            "ontology" / "default"
        self.assertFalse((default_dir / "jians.json").exists())

    def test_wujian_vocab_supplies_mapping(self):
        """装包后：五间词汇经全局注册表提供（transaction=生间/transfers=反间）。"""
        from core.wujian import build_wujian, register_wujian
        root = Path(__file__).resolve().parent.parent
        wj = build_wujian(root / "packs" / "wujian", "default")
        register_wujian(wj)
        self.assertEqual(wj.jians_for_types(["transaction"]), ["生间"])
        self.assertEqual(wj.jians_for_types(["transfers"]), ["反间"])
        self.assertEqual(wj.jians_for_types(["org"]), ["因间", "死间"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
