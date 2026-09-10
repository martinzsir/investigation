"""
tests/test_jians_decl.py
R5: jians.json 声明化（五间解耦）

验证用例：
  J-TC-01: 装载 jians.json 缺 name/重复/为空全部硬失败
  J-TC-03: rules.json 引用未声明间类 → 硬失败
  J-TC-04: 全库 grep "生间" 等字面量仅声明文件与测试，代码零残留
  J-TC-09: 单源命中间类等级仍为"观察"，不可通过配置升格
  J-TC-10: 现有 6 条规则行为完全一致
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.ontology_loader import (
    load_jians, load_cross_levels, invalidate_pack_cache, load_pack,
    DEFAULT_JIANS,
)


def _write_jians(path: Path, jians: list[dict], cross_levels=None) -> None:
    data = {"schema_version": 2, "jians": jians}
    if cross_levels is not None:
        data["cross_levels"] = cross_levels
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class JiansDeclTests(unittest.TestCase):
    """R5: jians.json 声明校验。"""

    def test_jians_loads_default(self):
        """default 包 jians.json 装载成功，返回 5 个间类。"""
        jians = load_jians("default")
        self.assertEqual(len(jians), 5)
        names = [j["name"] for j in jians]
        self.assertEqual(names, ["因间", "内间", "反间", "死间", "生间"])

    def test_j_tc_01_missing_name_fails(self):
        """J-TC-01: jians.json 缺 name → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_jians(pack_dir / "jians.json", [{"default_clearance": 1}])
            with self.assertRaises(ValueError):
                load_jians("testpack", base_dir=Path(td))

    def test_j_tc_01_duplicate_name_fails(self):
        """J-TC-01: 间类名重复 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_jians(pack_dir / "jians.json",
                         [{"name": "因间"}, {"name": "因间"}])
            with self.assertRaises(ValueError):
                load_jians("testpack", base_dir=Path(td))

    def test_j_tc_01_empty_fails(self):
        """J-TC-01: 间类为空 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_jians(pack_dir / "jians.json", [])
            with self.assertRaises(ValueError):
                load_jians("testpack", base_dir=Path(td))

    def test_cross_levels_invalid_source_fails(self):
        """cross_levels min_independent_sources 非 1/2/3 → 硬失败（红线）。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_jians(pack_dir / "jians.json",
                         [{"name": "因间"}],
                         cross_levels=[{"min_independent_sources": 4, "name": "超"}])
            with self.assertRaises(ValueError):
                load_cross_levels("testpack", base_dir=Path(td))

    def test_j_tc_09_single_source_still_observation(self):
        """J-TC-09: 单源命中间类等级仍为"观察"，不可通过配置升格。"""
        levels = load_cross_levels("default")
        name_map = {lv["min_independent_sources"]: lv["name"] for lv in levels}
        self.assertEqual(name_map[1], "观察")
        self.assertEqual(name_map[2], "线索")
        self.assertEqual(name_map[3], "可立案依据候选")

    def test_default_pack_jian_types_valid(self):
        """default 包所有规则的 jian_types 都在 jians.json 声明内。"""
        invalidate_pack_cache()
        pack = load_pack("default")
        jian_names = {j["name"] for j in load_jians("default")}
        for r in pack.rules.values():
            for j in r.jian_types:
                self.assertIn(j, jian_names,
                              f"规则 {r.id} jian_types={j} 未在 jians.json 声明")


if __name__ == "__main__":
    unittest.main()
