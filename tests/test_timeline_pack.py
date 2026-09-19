"""
tests/test_timeline_pack.py
P5 时间镜头包测试（packs/timeline 自枚举挂载 → skill_invoke 调度 → 线索化）。

覆盖：
  ① discover 从真实 packs/ 目录挂载 timeline 包（零注册代码）；
  ② 三个镜头经 skill_invoke 产出 LineageClue；
  ③ 证据引用通过 P1/P3 契约校验（事件 node 带 key_column、
     time_window 引用真实 lnk_time_window 行、aggregate 聚合量）；
  ④ 主体/项目不存在 → 零线索不造数；
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

from tests.test_timeline_functions import make_timeline_store


class TimelinePackTests(unittest.TestCase):

    def setUp(self):
        self.reg = SkillRegistry()
        self.store = make_timeline_store()
        report = discover(self.reg)  # 真实 packs/ 目录（relation + timeline）
        self.assertIn("timeline", report["loaded"], msg=str(report))

    def tearDown(self):
        self.store.close()

    def test_pack_mounted_with_metadata(self):
        spec = self.reg.skill("timeline_sequence")
        self.assertEqual(spec.pack_id, "timeline")
        self.assertEqual(spec.mode, "deterministic")
        self.assertTrue(spec.enabled)
        self.assertIn("target_subject", spec.params_schema)
        for t in ("transaction", "call", "trackpoint",
                  "bid_project", "time_window"):
            self.assertIn(t, spec.scope_reads)
        self.assertTrue(callable(spec.handler))
        spec_c = self.reg.skill("timeline_cross_collision")
        self.assertIn("project", spec_c.params_schema)

    def test_sequence_lens_produces_clue(self):
        clues = skill_invoke(self.reg, "timeline_sequence",
                             store=self.store,
                             params={"target_subject": "张三"})
        self.assertEqual(len(clues), 1)
        c = clues[0]
        self.assertEqual(c.skill_id, "timeline_sequence")
        self.assertIn("张三", c.title)
        kinds = [r["kind"] for r in c.evidence_refs]
        self.assertIn("node", kinds)
        self.assertIn("aggregate", kinds)
        # 事件引用 key_column 覆盖资金/通话/轨迹行键
        key_cols = {r.get("key_column") for r in c.evidence_refs
                    if r["kind"] == "node"}
        self.assertTrue({"txn_id", "call_id", "track_id"} <= key_cols)
        self.assertEqual(c.jian_types, ["生间"])

    def test_rhythm_lens_produces_clue(self):
        clues = skill_invoke(self.reg, "timeline_rhythm",
                             store=self.store,
                             params={"target_subject": "张三"})
        self.assertEqual(len(clues), 1)
        c = clues[0]
        metrics = {r["metric"] for r in c.evidence_refs
                   if r["kind"] == "aggregate"}
        self.assertIn("burst_count", metrics)
        self.assertIn("median_gap_days", metrics)
        self.assertEqual(c.detail["subject"]["name"], "张三")
        values = {r["metric"]: r.get("value") for r in c.evidence_refs
                  if r["kind"] == "aggregate"}
        self.assertEqual(values["burst_count"], 1)

    def test_cross_collision_lens_clues_and_time_window(self):
        clues = skill_invoke(self.reg, "timeline_cross_collision",
                             store=self.store,
                             params={"project": "市政道路工程"})
        self.assertEqual(len(clues), 3)
        zhang = next(c for c in clues
                     if c.detail["主体"] == "张三")
        kinds = [r["kind"] for r in zhang.evidence_refs]
        self.assertIn("node", kinds)
        self.assertIn("aggregate", kinds)
        # time_window 证据：真实 lnk_time_window 行 + 窗口起止
        tw = [r for r in zhang.evidence_refs
              if r["kind"] == "time_window"]
        self.assertEqual(len(tw), 1)
        self.assertEqual(tw[0]["ref"], "lnk_time_window#project_p1")
        self.assertEqual(tw[0]["key_column"], "project_id")
        self.assertEqual(tw[0]["time_from"], "2021-10-08")
        self.assertEqual(tw[0]["time_to"], "2021-10-22")
        self.assertIn("反间", zhang.jian_types)
        self.assertIn("生间", zhang.jian_types)

    def test_unknown_subject_or_project_no_clue(self):
        self.assertEqual(skill_invoke(
            self.reg, "timeline_sequence", store=self.store,
            params={"target_subject": "查无此人"}), [])
        self.assertEqual(skill_invoke(
            self.reg, "timeline_cross_collision", store=self.store,
            params={"project": "查无此项目"}), [])

    def test_params_validated_against_schema(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "timeline_sequence",
                         store=self.store, params={"bogus": 1})
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "timeline_sequence",
                         store=self.store, params={})
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "timeline_cross_collision",
                         store=self.store, params={})

    def test_scope_enforced_on_pack_lens(self):
        from core.access import AccessContext
        from core.registry import scoped_rows
        spec = self.reg.skill("timeline_cross_collision")
        access = AccessContext(operator="tester", role="system")
        rows = scoped_rows(spec, access, self.store, "transaction")
        self.assertGreater(len(rows), 0)
        with self.assertRaises(PermissionError):
            scoped_rows(spec, access, self.store, "person")


if __name__ == "__main__":
    unittest.main(verbosity=2)
