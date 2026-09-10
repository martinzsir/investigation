"""
tests/test_scoring_decl.py
R7: scoring.json 声明化（计分权重外置）

验证用例：
  SC-TC-01: 装载 scoring.json 权重和 ≠ 1.0 → 硬失败
  SC-TC-03: 修改权重 → 排序结果随之变化
  SC-TC-08: 数据行数极多 → data_strength 封顶 1.0
  SC-TC-10: 现有线索排序与改造前一致
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.ontology_loader import load_scoring, invalidate_pack_cache
from core.lineage import prioritize_clues, LineageClue


def _write_scoring(path: Path, dims, ac) -> None:
    path.write_text(json.dumps({
        "schema_version": 2, "dimensions": dims,
        "assumption_confidence": ac,
    }, ensure_ascii=False), encoding="utf-8")


class ScoringDeclTests(unittest.TestCase):
    """R7: scoring.json 声明校验。"""

    def test_default_pack_loads(self):
        """default 包 scoring.json 装载成功，权重和=1.0。"""
        scoring = load_scoring("default")
        total = sum(d["weight"] for d in scoring["dimensions"])
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_sc_tc_01_weight_sum_not_one_fails(self):
        """SC-TC-01: 权重和 ≠ 1.0 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_scoring(pack_dir / "scoring.json",
                           [{"name": "confidence", "weight": 0.5},
                            {"name": "jian_coverage", "weight": 0.5}],
                           {"_default": 0.7})
            # 0.5+0.5=1.0 应该通过；故意改成 0.6+0.5=1.1
            _write_scoring(pack_dir / "scoring.json",
                           [{"name": "confidence", "weight": 0.6},
                            {"name": "jian_coverage", "weight": 0.5}],
                           {"_default": 0.7})
            with self.assertRaises(ValueError):
                load_scoring("testpack", base_dir=Path(td))

    def test_sc_tc_01_missing_default_fails(self):
        """assumption_confidence 缺 _default → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_scoring(pack_dir / "scoring.json",
                           [{"name": "confidence", "weight": 1.0}],
                           {"H1": 0.9})
            with self.assertRaises(ValueError):
                load_scoring("testpack", base_dir=Path(td))

    def test_sc_tc_08_data_strength_capped(self):
        """SC-TC-08: 数据行数极多 → data_strength 封顶 1.0。"""
        # 构造一个有很多 source_rows 的线索
        clue = LineageClue(
            clue_id="t1", title="test",
            jian_types=["反间"],
            assumption_chain=["H1"],
            source_rows=[{"r": i} for i in range(100)],  # 100 行
        )
        result = prioritize_clues([clue], pack="default")
        score = result[0].detail["priority_score"]
        # data_str 封顶 1.0，confidence=0.9(H1), jian_cov=2/5=0.4
        # score = 0.9*0.4 + 0.4*0.35 + 1.0*0.25 = 0.36 + 0.14 + 0.25 = 0.75
        self.assertAlmostEqual(score, 0.75, places=2)

    def test_sc_tc_10_default_weights_match_hardcoded(self):
        """SC-TC-10: default 包权重与原硬编码值一致。"""
        scoring = load_scoring("default")
        dims = {d["name"]: d["weight"] for d in scoring["dimensions"]}
        self.assertAlmostEqual(dims["confidence"], 0.4)
        self.assertAlmostEqual(dims["jian_coverage"], 0.35)
        self.assertAlmostEqual(dims["data_strength"], 0.25)


if __name__ == "__main__":
    unittest.main()
