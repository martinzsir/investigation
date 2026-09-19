"""
tests/test_wujian_optional.py
P6 验收①：不装五间包时底座可独立装载，并可跑关系/时间镜头。

底座不认识间类（ObjectType/LinkType 无 jian，core 无映射表）。本测试在
清空五间词汇注册表（reset_wujian）的前提下验证：
  - default 案件包可装载、语义层可构建；
  - 间类消费点全部优雅降级（空列表/空字典），不硬失败；
  - 关系/时间镜头包技能照常产出线索，只是 jian_types 留空（交叉页以缺口展示）；
  - prioritize 计分不依赖词汇也能跑（间类项计 0，不崩）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology_loader import load_pack
from core.pack_loader import discover
from core.registry import SkillRegistry, skill_invoke, LineageClue

from tests.test_ontology_version import make_store
from tests.test_relation_functions import make_graph_store
from tests.test_timeline_functions import make_timeline_store


class BaseWithoutWujianTests(unittest.TestCase):
    """无五间词汇：底座独立可用。"""

    def setUp(self):
        from core.wujian import reset_wujian
        reset_wujian()

    def tearDown(self):
        from core.wujian import reset_wujian
        reset_wujian()

    def test_default_pack_loads(self):
        """案件包无 jians.json 也能装载，对象/链接无 jian 属性。"""
        spec = load_pack("default")
        self.assertTrue(spec.objects)
        self.assertTrue(spec.links)
        self.assertFalse(hasattr(spec.objects[0], "jian"))
        self.assertFalse(hasattr(spec.links[0], "jian"))

    def test_build_ontology_without_wujian(self):
        """语义层可独立构建（obj_* 正常物化）。"""
        from core.ontology import build_ontology
        s = make_store()
        build_ontology(s.conn)
        n = s.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name='obj_transaction'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_jian_consumers_degrade(self):
        """各间类消费点无词汇 → 空集合降级，不抛。"""
        from core import functions as fns
        from core.lineage import _jian_weights
        from server.app import ontology_meta
        self.assertEqual(fns._jian_order("default"), [])
        self.assertEqual(fns._jian_entries("default"), [])
        self.assertEqual(_jian_weights("default"), {})
        self.assertEqual(ontology_meta.jian_clearances("default"), {})
        self.assertEqual(ontology_meta.cross_level_names("default"), [])

    def test_prioritize_runs_without_vocab(self):
        """无间类权重也能计分（间类项=0），不崩。"""
        from core.lineage import prioritize_clues
        c = LineageClue(clue_id="x", title="x",
                        jian_types=["反间"], source_rows=[{"r": 1}])
        out = prioritize_clues([c], pack="default")
        self.assertTrue(out)
        self.assertGreaterEqual(out[0].detail["priority_score"], 0.0)

    def test_relation_lens_runs_without_vocab(self):
        """关系镜头：技能照常出线索，jian_types 留空。"""
        reg = SkillRegistry()
        store = make_graph_store()
        try:
            discover(reg)                 # 装载技能（顺带挂词汇）
            from core.wujian import reset_wujian
            reset_wujian()                # 立即撤下词汇，模拟底座无包
            clues = skill_invoke(reg, "relation_neighborhood",
                                 store=store,
                                 params={"target_subject": "宏业建设",
                                         "depth": 2})
            self.assertEqual(len(clues), 1)
            self.assertEqual(clues[0].jian_types, [])
        finally:
            store.close()

    def test_timeline_lens_runs_without_vocab(self):
        """时间镜头：技能照常出线索，jian_types 留空。"""
        reg = SkillRegistry()
        store = make_timeline_store()
        try:
            discover(reg)
            from core.wujian import reset_wujian
            reset_wujian()
            clues = skill_invoke(reg, "timeline_sequence",
                                 store=store,
                                 params={"target_subject": "张三"})
            self.assertEqual(len(clues), 1)
            self.assertEqual(clues[0].jian_types, [])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
