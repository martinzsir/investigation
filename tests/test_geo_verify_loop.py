"""
tests/test_geo_verify_loop.py
空间产出 → 实地核查闭环（排查区转核查任务，而非止于 GeoJSON）。

覆盖：
  ① R-GEO-1/2/3 均声明 verify_playbook，且 function 槽位指向已声明 Function；
  ② render_suggested 对 R-GEO-3 线索（assumption_chain=H6）渲染 external 建议项，
     external.target/material 非空（实地走访/调取材料）；
  ③ 假设门：假设不匹配时正常筛掉（非 skip）；
  ④ 落库幂等（ADR-V-4 item_key 稳定键），重复供给不堆项；
  ⑤ R-GEO-1 的 function 槽位指向 CGT 与活动范围画像（尺度自检建议 buffer_m 后复跑）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology_loader import load_verify_playbooks
from server.app.store.state_store import StateStore
from server.app.verify_provision import render_suggested


def _clue(rule_id: str, assumptions: list[str]) -> dict:
    return {
        "clue_id": f"clue_{rule_id.lower()}",
        "skill_id": "geo_accompany",
        "title": "异主体时空反复同框（私下接触候选）",
        "detail": {"rule_id": rule_id},
        "assumption_chain": assumptions,
    }


class GeoPlaybookDeclTests(unittest.TestCase):
    """① 声明完整性：三条空间规则都有手册条目。"""

    def test_all_geo_rules_have_playbook(self):
        pbs = load_verify_playbooks("default")
        covered = {pb["rule_id"] for pb in pbs
                   if str(pb["rule_id"]).startswith("R-GEO")}
        self.assertEqual({"R-GEO-1", "R-GEO-2", "R-GEO-3"}, covered)

    def test_function_slot_declared(self):
        """function 槽位必须是 functions.json 已声明项（装载期已校验，
        此处固化"复跑 CGT / 活动范围画像"这一语义）。"""
        pbs = {pb["id"]: pb for pb in load_verify_playbooks("default")}
        rerun = pbs["rgeo1_cgt_rerun"]
        self.assertEqual("geo_profile_cgt", rerun["function"])
        self.assertEqual("geo_activity_range", rerun["fallback_function"])


class GeoRenderSuggestedTests(unittest.TestCase):
    """② 渲染：空间线索产出 external 建议项（实地核查任务）。"""

    def test_rgeo3_renders_external_items(self):
        items, skipped = render_suggested(_clue("R-GEO-3", ["H6"]), [],
                                          pack_id="default")
        self.assertEqual([], skipped)
        self.assertEqual(2, len(items))
        for it in items:
            self.assertEqual("external", it["channel"])
            self.assertEqual("建议", it["status"])
            self.assertEqual("suggested", it["origin"])
            self.assertTrue(it["external"]["target"].strip())
            self.assertTrue(it["external"]["material"].strip())
            self.assertTrue(it["falsification"].strip())

    def test_assumption_gate_filters(self):
        """③ 假设不匹配 → 正常筛掉（不是 skip）。"""
        items, skipped = render_suggested(_clue("R-GEO-3", ["H1"]), [],
                                          pack_id="default")
        self.assertEqual([], items)
        self.assertEqual([], skipped)

    def test_no_rule_id_no_item(self):
        items, _ = render_suggested({"detail": {}, "assumption_chain": ["H6"]},
                                    [], pack_id="default")
        self.assertEqual([], items)


class GeoVerifyItemPersistTests(unittest.TestCase):
    """④ 落库幂等：重复供给不堆项（ADR-V-4）。"""

    def test_upsert_idempotent(self):
        items, _ = render_suggested(_clue("R-GEO-3", ["H6"]), [],
                                    pack_id="default")
        with tempfile.TemporaryDirectory() as td:
            st = StateStore("case1", Path(td) / "state.sqlite")
            first = st.upsert_verify_items("case1", "clue_r-geo-3", items)
            second = st.upsert_verify_items("case1", "clue_r-geo-3", items)
            self.assertEqual(2, first["added"])
            self.assertEqual(0, second["added"])
            self.assertEqual(2, second["total"])


if __name__ == "__main__":
    unittest.main()
