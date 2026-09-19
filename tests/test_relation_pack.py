"""
tests/test_relation_pack.py
P4 关系镜头包测试（packs/relation 自枚举挂载 → skill_invoke 调度 → 线索化）。

覆盖：
  ① discover 从真实 packs/ 目录挂载 relation 包（零注册代码）；
  ② 三个镜头经 skill_invoke 产出 LineageClue；
  ③ 证据引用通过 P1/P3 契约校验（obj_* 带 key_column、lnk_* 带 key_column）；
  ④ 主体不存在 → 零线索不造数；
  ⑤ spec 元数据（pack_id/params_schema/scope_reads）正确。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.pack_loader import discover
from core.registry import SkillRegistry, skill_invoke

from tests.test_relation_functions import make_graph_store


class RelationPackTests(unittest.TestCase):

    def setUp(self):
        self.reg = SkillRegistry()
        self.store = make_graph_store()
        report = discover(self.reg)  # 真实 packs/ 目录（含 packs/relation）
        self.assertIn("relation", report["loaded"], msg=str(report))

    def tearDown(self):
        self.store.close()

    def test_pack_mounted_with_metadata(self):
        spec = self.reg.skill("relation_neighborhood")
        self.assertEqual(spec.pack_id, "relation")
        self.assertEqual(spec.mode, "deterministic")
        self.assertTrue(spec.enabled)
        self.assertIn("target_subject", spec.params_schema)
        for t in ("person", "account", "org", "bid_project",
                  "transfers", "calls_to", "owns", "involved_in",
                  "co_located"):
            self.assertIn(t, spec.scope_reads)
        self.assertTrue(callable(spec.handler))

    def test_neighborhood_lens_produces_clue(self):
        clues = skill_invoke(self.reg, "relation_neighborhood",
                             store=self.store,
                             params={"target_subject": "宏业建设",
                                     "depth": 2})
        self.assertEqual(len(clues), 1)
        c = clues[0]
        self.assertEqual(c.skill_id, "relation_neighborhood")
        self.assertIn("宏业建设", c.title)
        kinds = [r["kind"] for r in c.evidence_refs]
        self.assertIn("node", kinds)
        self.assertIn("edge", kinds)
        self.assertIn("aggregate", kinds)
        # 证据引用全部通过校验（skill_invoke 内硬失败即不过）
        node_refs = [r for r in c.evidence_refs if r["kind"] == "node"]
        self.assertTrue(all(r.get("key_column") for r in node_refs))
        self.assertIn("反间", c.jian_types)

    def test_common_neighbors_lens(self):
        clues = skill_invoke(self.reg, "relation_common_neighbors",
                             store=self.store,
                             params={"subject_a": "张三",
                                     "subject_b": "李四"})
        self.assertEqual(len(clues), 1)
        self.assertIn("王五", clues[0].title)
        self.assertEqual(clues[0].detail["count"], 1)

    def test_paths_lens_one_clue_per_path(self):
        clues = skill_invoke(self.reg, "relation_paths",
                             store=self.store,
                             params={"subject_a": "宏业建设",
                                     "subject_b": "张卫国配偶",
                                     "depth": 2})
        self.assertEqual(len(clues), 1)
        self.assertEqual(clues[0].detail["length"], 2)
        edge_refs = [r for r in clues[0].evidence_refs
                     if r["kind"] == "edge"]
        self.assertEqual(len(edge_refs), 2)
        for r in edge_refs:
            self.assertTrue(r["key_column"].startswith("txn"))

    def test_unknown_subject_no_clue(self):
        self.assertEqual(skill_invoke(
            self.reg, "relation_neighborhood", store=self.store,
            params={"target_subject": "查无此人"}), [])
        self.assertEqual(skill_invoke(
            self.reg, "relation_common_neighbors", store=self.store,
            params={"subject_a": "张三", "subject_b": "查无此人"}), [])

    def test_params_validated_against_schema(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "relation_neighborhood",
                         store=self.store, params={"bogus": 1})
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "relation_neighborhood",
                         store=self.store, params={})  # 缺必填 target_subject

    def test_scope_enforced_on_pack_lens(self):
        from core.access import AccessContext
        from core.registry import scoped_rows
        spec = self.reg.skill("relation_neighborhood")
        access = AccessContext(operator="tester", role="system")
        rows = scoped_rows(spec, access, self.store, "person")
        self.assertGreater(len(rows), 0)
        with self.assertRaises(PermissionError):
            scoped_rows(spec, access, self.store, "tipoff")


if __name__ == "__main__":
    unittest.main(verbosity=2)
