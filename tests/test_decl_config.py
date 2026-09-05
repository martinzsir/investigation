"""
tests/test_decl_config.py
REQ-G-011/012/013 声明化（声明是数据）：
  - G-011 dimensions.json：维度名从声明读；规则引用未声明 dimension → 装载期硬失败；
    新增维度不改 Python 即被规则引用、参与覆盖度
  - G-012 enum_space.json：枚举空间从声明读；自定义 space 传参仍覆盖
  - G-013 objects/links 的 jian 字段：非法 jian 硬失败；五间交叉遍历声明而非硬编码表名；
    交叉**等级规则/展示顺序保持硬编码**（改声明不改变单/双/三源等级）
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
                rule_dimension="资金", obj_jian=None, link_jian=None,
                rule_function="f_ping"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects.json").write_text(json.dumps({
        "schema_version": 2,
        "objects": [
            {"name": "foo", "pk": "foo_id", "kind": "entity",
             "name_property": "foo_id",
             **({"jian": obj_jian} if obj_jian else {}),
             "properties": {"name": "string"}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "links.json").write_text(json.dumps({
        "schema_version": 2,
        "links": [
            {"name": "fw", "from_obj": "foo", "to_obj": "foo",
             "runtime": True,
             **({"jian": link_jian} if link_jian else {}),
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
        self.assertIn("主体", space)
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


class JianDeclTests(unittest.TestCase):
    def test_illegal_jian_hard_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, obj_jian="仙间")  # 非法间类
            with self.assertRaises(ValueError) as ctx:
                load_pack("p", base_dir=Path(td))
            self.assertIn("jian", str(ctx.exception))

    def test_legal_jian_loads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "p"
            _write_pack(root, obj_jian="生间", link_jian="反间")
            spec = load_pack("p", base_dir=Path(td))
            foo = next(o for o in spec.objects if o.name == "foo")
            fw = next(l for l in spec.links if l.name == "fw")
            self.assertEqual(foo.jian, "生间")
            self.assertEqual(fw.jian, "反间")

    def test_default_pack_jian_declared(self):
        spec = load_pack("default")
        jians = {o.name: o.jian for o in spec.objects if o.jian}
        self.assertEqual(jians.get("transaction"), "生间")
        self.assertEqual(jians.get("bid_project"), "因间")
        self.assertEqual(jians.get("org"), "死间")
        self.assertEqual(jians.get("tipoff"), "内间")
        tw = next(l for l in spec.links if l.name == "time_window")
        self.assertEqual(tw.jian, "反间")

    def test_level_rule_remains_hardcoded(self):
        # 红线：无论声明多少对象，等级阈值单源=观察/双源=线索/三源+=可立案依据候选
        from core.functions import _jian_entries, _JIAN_ORDER
        entries = _jian_entries("default")
        # 声明驱动出多条数据源
        self.assertTrue(len(entries) >= 7)
        # 展示顺序固定
        self.assertEqual(_JIAN_ORDER, ["因间", "内间", "反间", "死间", "生间"])
        jians = {j for _t, j, _s in entries}
        self.assertTrue(jians.issubset(set(_JIAN_ORDER)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
