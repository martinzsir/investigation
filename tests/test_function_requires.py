"""
tests/test_function_requires.py
R3: FunctionSpec.requires 声明 + loader 校验

验证用例：
  META-TC-01: requires.objects 引用未声明对象 → 硬失败
  META-TC-02: requires.props 引用未声明属性 → 硬失败
  META-TC-03: 合法 requires → 装载通过，FunctionSpec.requires 非空
  META-TC-04: 7 个 py 函数均声明 requires → 全通过
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.ontology_loader import _load_functions, invalidate_pack_cache, load_pack


def _write_functions(path: Path, functions: list[dict]) -> None:
    path.write_text(json.dumps({"schema_version": 2, "functions": functions},
                               ensure_ascii=False), encoding="utf-8")


class FunctionRequiresTests(unittest.TestCase):
    """R3: requires 声明校验。"""

    def _get_objects_links(self):
        """从 default 包获取已声明的 objects/links 供 _load_functions 使用。"""
        invalidate_pack_cache()
        pack = load_pack("default")
        return pack.objects, pack.links

    def test_meta_tc_03_legal_requires_passes(self):
        """META-TC-03: 合法 requires 装载通过，FunctionSpec.requires 非空。"""
        objects, links = self._get_objects_links()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "functions.json"
            _write_functions(p, [{
                "name": "test_fn", "output_type": "report", "impl": "py",
                "impl_ref": "call_frequency_spike",
                "inputs": ["obj_call"],
                "requires": {"objects": ["call"], "props": {"call": ["caller_raw"]}},
            }])
            out = _load_functions(p, objects, links, required=False)
            self.assertEqual(out["test_fn"].requires["objects"], ["call"])

    def test_meta_tc_01_undeclared_object_fails(self):
        """META-TC-01: requires.objects 引用未声明对象 → 硬失败。"""
        objects, links = self._get_objects_links()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "functions.json"
            _write_functions(p, [{
                "name": "test_fn", "output_type": "report", "impl": "py",
                "impl_ref": "call_frequency_spike",
                "inputs": ["obj_call"],
                "requires": {"objects": ["nonexist_obj_xyz"]},
            }])
            with self.assertRaises(ValueError):
                _load_functions(p, objects, links, required=False)

    def test_meta_tc_02_undeclared_prop_fails(self):
        """META-TC-02: requires.props 引用未声明属性 → 硬失败。"""
        objects, links = self._get_objects_links()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "functions.json"
            _write_functions(p, [{
                "name": "test_fn", "output_type": "report", "impl": "py",
                "impl_ref": "call_frequency_spike",
                "inputs": ["obj_call"],
                "requires": {"objects": ["call"], "props": {"call": ["nonexist_prop"]}},
            }])
            with self.assertRaises(ValueError):
                _load_functions(p, objects, links, required=False)

    def test_meta_tc_04_all_py_functions_have_requires(self):
        """META-TC-04: 7 个 py 函数均声明 requires。"""
        invalidate_pack_cache()
        pack = load_pack("default")
        py_fns = {n: f for n, f in pack.functions.items() if f.impl == "py"}
        self.assertEqual(len(py_fns), 7)
        for name, f in py_fns.items():
            self.assertIn("requires", f.to_dict(),
                          f"{name} 应声明 requires")

    def test_meta_tc_05_undeclared_link_fails(self):
        """requires.links 引用未声明链接 → 硬失败。"""
        objects, links = self._get_objects_links()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "functions.json"
            _write_functions(p, [{
                "name": "test_fn", "output_type": "report", "impl": "py",
                "impl_ref": "call_frequency_spike",
                "inputs": ["obj_call"],
                "requires": {"links": ["nonexist_link_xyz"]},
            }])
            with self.assertRaises(ValueError):
                _load_functions(p, objects, links, required=False)


if __name__ == "__main__":
    unittest.main()
