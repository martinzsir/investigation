"""
tests/test_jians_decl.py
P6：五间词汇唯一权威在 packs/wujian/jians.json，经 core/wujian.py 装载校验
（不再寄生 ontology_loader）。

验证用例：
  J-TC-01: 装载词汇缺 name/重复/为空全部硬失败
  J-TC-03: 规则 jian_types 引用未声明间类 → 五间 validator 硬失败
  J-TC-09: 单源命中间类等级仍为"观察"，不可通过配置升格（红线 1/2/3）
  P2 映射：transaction=生间、transfers=反间、org 一对多（因间/死间）
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.wujian import (
    build_wujian, register_wujian, reset_wujian,
)
from core.ontology_loader import invalidate_pack_cache, load_pack

ROOT = Path(__file__).resolve().parent.parent
WUJIAN_DIR = ROOT / "packs" / "wujian"


def _default_wujian():
    return build_wujian(WUJIAN_DIR, "default")


def _write_jians(path: Path, jians: list[dict], cross_levels=None) -> None:
    data: dict = {"schema_version": 2, "jians": jians}
    if cross_levels is not None:
        data["cross_levels"] = cross_levels
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class WujianVocabTests(unittest.TestCase):
    """P6：五间词汇 packs/wujian 声明校验。"""

    def setUp(self):
        reset_wujian()
        invalidate_pack_cache()

    def tearDown(self):
        reset_wujian()

    def test_jians_loads_default(self):
        """packs/wujian 装载成功，返回 5 个间类（固定次序）。"""
        wj = _default_wujian()
        self.assertEqual(
            wj.jian_order, ["因间", "内间", "反间", "死间", "生间"])

    def test_j_tc_01_missing_name_fails(self):
        """J-TC-01: 缺 name → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            _write_jians(Path(td) / "jians.json",
                         [{"default_clearance": 1}],
                         cross_levels=[
                             {"min_independent_sources": 1, "name": "观察"}])
            with self.assertRaises(ValueError):
                build_wujian(td, "testpack")

    def test_j_tc_01_duplicate_name_fails(self):
        """J-TC-01: 间类名重复 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            _write_jians(Path(td) / "jians.json",
                         [{"name": "因间"}, {"name": "因间"}],
                         cross_levels=[
                             {"min_independent_sources": 1, "name": "观察"}])
            with self.assertRaises(ValueError):
                build_wujian(td, "testpack")

    def test_j_tc_01_empty_fails(self):
        """J-TC-01: 间类为空 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            _write_jians(Path(td) / "jians.json", [])
            with self.assertRaises(ValueError):
                build_wujian(td, "testpack")

    def test_cross_levels_invalid_source_fails(self):
        """cross_levels min_independent_sources 非 1/2/3 → 硬失败（红线）。"""
        with tempfile.TemporaryDirectory() as td:
            _write_jians(Path(td) / "jians.json",
                         [{"name": "因间"}],
                         cross_levels=[
                             {"min_independent_sources": 4, "name": "超"}])
            with self.assertRaises(ValueError):
                build_wujian(td, "testpack")

    def test_j_tc_09_single_source_still_observation(self):
        """J-TC-09: 等级映射固定（单源=观察），不可通过配置升格。"""
        wj = _default_wujian()
        self.assertEqual(wj.cross_level_name(1), "观察")
        self.assertEqual(wj.cross_level_name(2), "线索")
        self.assertEqual(wj.cross_level_name(3), "可立案依据候选")

    def test_default_rules_jian_tags_valid(self):
        """J-TC-03: default 所有规则 jian_types 经五间 validator 合法。"""
        wj = _default_wujian()
        pack = load_pack("default")
        for r in pack.rules.values():
            wj.validate_rule_jian_types(r.jian_types)   # 不抛即通过

    def test_rule_unknown_jian_tag_fails(self):
        """J-TC-03: 规则引用词汇外间类 → validator 硬失败。"""
        wj = _default_wujian()
        with self.assertRaises(ValueError):
            wj.validate_rule_jian_types(["外星间"])

    def test_p2_transaction_is_sheng_jian(self):
        """P2：流水事件 transaction 归生间（与通话/轨迹同构）。"""
        wj = _default_wujian()
        self.assertEqual(wj.jians_for_types(["transaction"]), ["生间"])

    def test_p2_transfers_is_fan_jian(self):
        """P2：过桥派生边 transfers 归反间；transaction 不归反间。"""
        wj = _default_wujian()
        self.assertEqual(wj.jians_for_types(["transfers"]), ["反间"])
        self.assertNotIn("反间", wj.jians_for_types(["transaction"]))

    def test_p2_org_multi_jian(self):
        """P2：org 同时属因间/死间（一对多映射合法）。"""
        wj = _default_wujian()
        self.assertEqual(wj.jians_for_types(["org"]), ["因间", "死间"])

    def test_p2_jian_entries_reflect_mapping(self):
        """P2：_jian_entries 反查映射，transaction=生间、transfers=反间。"""
        register_wujian(_default_wujian())
        from core.functions import _jian_entries
        entries = _jian_entries("default")
        by_name = {e[3]: e[1] for e in entries}
        self.assertEqual(by_name.get("transaction"), "生间")
        self.assertEqual(by_name.get("transfers"), "反间")
        # org 一对多：entries 中应同时出现因间和死间
        org_jians = {e[1] for e in entries if e[3] == "org"}
        self.assertEqual(org_jians, {"因间", "死间"})


if __name__ == "__main__":
    unittest.main()
