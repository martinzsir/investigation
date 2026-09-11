"""
tests/test_scoring_v3.py
P1-3 + P0-1 + P1-0c + P2-5 修复回归护栏（score_source=scoring.json@v3）。

覆盖：
  - P1-3：data_strength.curve=log，0 行不再压过多行；
  - P0-1：等级作主序（声明推导），候选级恒在观察级之前；
  - P1-0c：无间类线索 cross_level 不再误标"观察"，退回让读面用级别字段；
  - P2-5：权重复核（B vs G 偏好）—— 50 行通讯记录优先于 1 条内间举报；
  - 红线：分数不参与等级判定（等级仍由独立源数决定）；
  - version 标识：score_source=scoring.json@v3。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.lineage import (
    _cross_level_name_single,
    _SCORE_SOURCE,
    prioritize_clues,
)
from core.registry import LineageClue

from server.app import ontology_meta


def _clue(*, cid: str, jians: list[str], rows: int = 0,
          assumptions: list[str] | None = None) -> LineageClue:
    return LineageClue(
        clue_id=cid,
        skill_id="t",
        title=cid,
        jian_types=list(jians),
        assumption_chain=list(assumptions or []),
        source_rows=[{"r": i} for i in range(rows)],
    )


class DataStrengthLogTests(unittest.TestCase):
    """P1-3：对数曲线下 0 行线索不再压过多行线索。"""

    def test_zero_rows_uses_log_and_gives_zero(self):
        c = _clue(cid="c0", jians=["反间"], rows=0, assumptions=["H1"])
        out = prioritize_clues([c], pack="default")[0]
        basis = out.detail["score_basis"]["data_strength"]
        # log1p(0)=0，归一化后 = 0
        self.assertAlmostEqual(basis["raw"], 0.0, places=6)

    def test_log_curve_orders_more_rows_higher_than_zero(self):
        """核心修复：21 行 ≥ 0 行。"""
        c0 = _clue(cid="c0", jians=["反间"], rows=0, assumptions=["H1"])
        c1 = _clue(cid="c1", jians=["反间"], rows=21, assumptions=["H1"])
        out = prioritize_clues([c0, c1], pack="default")
        s0 = next(c.detail["priority_score"] for c in out if c.clue_id == "c0")
        s1 = next(c.detail["priority_score"] for c in out if c.clue_id == "c1")
        self.assertGreater(s1, s0,
            f"21 行 ({s1}) 必须高于 0 行 ({s0})——否则 data_strength 维度失灵")

    def test_log_saturates_at_normalize_rows(self):
        """normalize=50 行 → data_strength≈1.0。"""
        c = _clue(cid="c50", jians=["反间"], rows=50, assumptions=["H1"])
        out = prioritize_clues([c], pack="default")[0]
        basis = out.detail["score_basis"]["data_strength"]
        self.assertAlmostEqual(basis["raw"], 1.0, delta=1e-3)

    def test_cap_still_enforced_under_log(self):
        """SC-TC-08：行数极多 data_strength 封顶 1.0。"""
        c = _clue(cid="c100", jians=["反间"], rows=100, assumptions=["H1"])
        out = prioritize_clues([c], pack="default")[0]
        basis = out.detail["score_basis"]["data_strength"]
        self.assertEqual(basis["raw"], 1.0)

    def test_score_source_bumped_to_v3(self):
        c = _clue(cid="c", jians=["反间"], rows=1, assumptions=["H1"])
        out = prioritize_clues([c], pack="default")[0]
        self.assertEqual(out.detail["score_source"], _SCORE_SOURCE)
        self.assertEqual(_SCORE_SOURCE, "scoring.json@v3")


class LevelPrimarySortTests(unittest.TestCase):
    """P0-1：等级主序由 jians.json 声明推导，候选级恒在观察级之前。"""

    def test_candidate_ranks_before_observation_with_lower_score(self):
        """构造候选级(0.45) vs 观察级(0.90)：候选级必须排前。"""
        # 候选级：三源 (生间4+反间2+因间3=9)，jian_cov=min(1,9/5)=1.0
        # 观察级：单源内间=5/5=1.0；同等分场景：让候选级 data_strength=0，
        # 观察级 data_strength=1.0（50 行）；conf 都 H1=0.9
        cand = _clue(cid="cand", jians=["生间", "反间", "因间"], rows=0,
                     assumptions=["H1"])
        obs = _clue(cid="obs", jians=["内间"], rows=50, assumptions=["H1"])
        # 先跑出原始分（用于 sanity：观察级分数 > 候选级）
        scored = prioritize_clues([cand, obs], pack="default")
        cand_score = next(c.detail["priority_score"] for c in scored
                          if c.clue_id == "cand")
        obs_score = next(c.detail["priority_score"] for c in scored
                         if c.clue_id == "obs")
        self.assertGreater(obs_score, cand_score,
            f"前置：观察级(内间+50行) 分数应高于 候选级(三源低权+0行)，"
            f"实际 obs={obs_score} cand={cand_score}")

        # 应用等级主序：候选级必须排在前
        ranks = ontology_meta.cross_level_rank("default")
        items = [{"level": c.detail.get("cross_level"),
                  "priority_score": c.detail.get("priority_score"),
                  "clue_id": c.clue_id} for c in scored]
        items.sort(key=lambda x: (
            ontology_meta.level_sort_rank(x["level"], ranks),
            -(x["priority_score"]
              if isinstance(x["priority_score"], (int, float)) else -1),
            x["clue_id"] or ""))
        self.assertEqual(items[0]["clue_id"], "cand",
            f"等级主序未生效：首位={items[0]}; 排序={items}")

    def test_unknown_level_sinks_to_bottom(self):
        """未知等级（旧产物无 cross_level、异常通道"待核实"）垫底。"""
        ranks = ontology_meta.cross_level_rank("default")
        self.assertEqual(
            ontology_meta.level_sort_rank("待核实", ranks),
            ontology_meta.UNKNOWN_LEVEL_RANK)
        self.assertEqual(
            ontology_meta.level_sort_rank(None, ranks),
            ontology_meta.UNKNOWN_LEVEL_RANK)
        self.assertEqual(
            ontology_meta.level_sort_rank("", ranks),
            ontology_meta.UNKNOWN_LEVEL_RANK)

    def test_cross_level_rank_derived_from_declaration(self):
        """等级序从 jians.json cross_levels 推导（不硬编码）。"""
        ranks = ontology_meta.cross_level_rank("default")
        # 最高等级（min_independent_sources 最大）= rank 0
        # 内置包为 ["观察", "线索", "可立案依据候选"]
        self.assertEqual(ranks["可立案依据候选"], 0)
        self.assertEqual(ranks["线索"], 1)
        self.assertEqual(ranks["观察"], 2)


class ZeroJianSafetyTests(unittest.TestCase):
    """P1-0c：无间类线索 cross_level 返回 None（不误标"观察"）。"""

    def test_zero_jian_cross_level_returns_none(self):
        self.assertIsNone(_cross_level_name_single(0, "default"))

    def test_zero_jian_clue_detail_cross_level_is_none(self):
        c = _clue(cid="c0", jians=[], rows=0)
        out = prioritize_clues([c], pack="default")[0]
        # 0 间类 → cross_level=None，让读面回落 detail.级别（异常通道"待核实"）
        self.assertIsNone(out.detail.get("cross_level"))


class RedLineRegressionTests(unittest.TestCase):
    """红线：等级仍由独立源数决定，分数不能反向升格。"""

    def test_score_does_not_influence_level(self):
        """高分单源（=观察）不会变候选级。"""
        # 单源生间 100 行（理论上高分）+ 单源内间 0 行（低分）
        hi = _clue(cid="hi", jians=["生间"], rows=100, assumptions=["H1"])
        lo = _clue(cid="lo", jians=["内间"], rows=0, assumptions=["H1"])
        out = prioritize_clues([hi, lo], pack="default")
        # 两条都应是"观察"
        self.assertEqual(out[0].detail["cross_level"] if out[0].clue_id == "hi"
                         else out[1].detail["cross_level"],
                         "观察")

    def test_three_independent_jians_still_candidate(self):
        """三独立源（生间+反间+因间）→ 候选级，与分数无关。"""
        c = _clue(cid="t", jians=["生间", "反间", "因间"], rows=0,
                  assumptions=["H1"])
        out = prioritize_clues([c], pack="default")[0]
        self.assertEqual(out.detail["cross_level"], "可立案依据候选")


class ScoreBasisConsistencyTests(unittest.TestCase):
    """contrib 之和 ≈ priority_score（可解释性约束）。"""

    def test_score_basis_contrib_sum_equals_score(self):
        c = _clue(cid="t", jians=["内间", "死间", "因间"], rows=5,
                  assumptions=["H1"])
        out = prioritize_clues([c], pack="default")[0]
        basis = out.detail["score_basis"]
        s = out.detail["priority_score"]
        contrib_sum = sum(basis[k]["contrib"] for k in basis)
        self.assertAlmostEqual(contrib_sum, s, places=3)


class JianWeightPreferenceTests(unittest.TestCase):
    """P2-5：权重复核（B vs G 偏好）—— 50 行通讯记录 优先于 1 条内间举报。"""

    def test_B_shengjian_50rows_beats_G_neijian_10rows(self):
        """B（单源生间 50 行）分数必须高于 G（单源内间 10 行）。

        偏好依据：数据量（50 行通讯记录）压过单条内间举报的间类权重优势。
        由 jians.json 生间权重=4 实现：B jian_cov=4/5=0.8，G jian_cov=5/5=1.0，
        但 B 的 data_strength=1.0（饱和）足以拉开差值。
        """
        b = _clue(cid="B", jians=["生间"], rows=50, assumptions=["H1"])
        g = _clue(cid="G", jians=["内间"], rows=10, assumptions=["H1"])
        out = prioritize_clues([b, g], pack="default")
        b_score = next(c.detail["priority_score"] for c in out
                       if c.clue_id == "B")
        g_score = next(c.detail["priority_score"] for c in out
                       if c.clue_id == "G")
        self.assertGreater(b_score, g_score,
            f"P2-5 偏好未生效：B(生间50行)={b_score} 应高于 G(内间10行)={g_score}")
        # 排序首位必须是 B
        self.assertEqual(out[0].clue_id, "B")


if __name__ == "__main__":
    unittest.main()