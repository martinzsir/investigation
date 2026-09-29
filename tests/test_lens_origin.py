"""
tests/test_lens_origin.py
定向镜头发起来源归一化（案件级画布回挂的入口契约）。

覆盖（对应 core/lens_origin 的 R-1 / R-2 / R-3）：
  ① 线索级：clue_id 有效 → 原样保留已知键；
  ② 案件级：只有 node_id → **不得丢弃**（R-1），记 canvas=case；
  ③ 无任何上下文 → None（唯一合法的丢弃情形）；
  ④ 未知键丢弃、非 dict 入参安全（R-3）；
  ⑤ 指纹键：clue_id 优先、回落 node_id（R-2）；
  ⑥ 真跑验证：同镜头 + 同靶心名 + 不同发起节点 → 观察 id **必须不同**，
     否则定向观察 upsert 时后者覆盖前者，正兵换个人再跑一次，上一个人
     在图上的结论就消失了；
  ⑦ 反向验证：改回"只取 clue_id"后 ⑥ 必须失败（证明断言不是摆设）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.lens_origin import (  # noqa: E402
    CASE_CANVAS,
    normalize_origin,
    origin_context_key,
)
from core.observation import observation_from_clue  # noqa: E402


class _FakeClue:
    """最小 LineageClue 替身：只需 to_dict 与 observation_from_clue 用得到的键。"""

    def __init__(self, *, skill_id: str, subject: str, param_source: str = ""):
        self._d = {
            "skill_id": skill_id,
            "title": f"{skill_id} 的产出",
            "detail": {
                "subject": {"name": subject, "pk": f"person_{subject}"},
                "param_source": param_source,
                "basis": "判据",
                "falsification": "证伪条件",
            },
            "evidence_refs": [],
        }

    def to_dict(self) -> dict:
        return self._d


# ----------------------------------------------------------------------
# ①~④ 归一化
# ----------------------------------------------------------------------
class TestNormalizeOrigin(unittest.TestCase):
    def test_clue_level_origin_kept(self):
        got = normalize_origin({
            "clue_id": "clue_1", "node_id": "n1",
            "subject": "张卫国", "surface": "canvas",
        })
        self.assertEqual(got, {"clue_id": "clue_1", "node_id": "n1",
                               "subject": "张卫国", "surface": "canvas"})

    def test_case_canvas_origin_not_dropped(self):
        """R-1：案件级画布没有 clue_id，绝不许把来源整个丢弃。"""
        got = normalize_origin({
            "node_id": "case#C1:subject:person_aaa", "subject": "张卫国",
        })
        self.assertIsNotNone(got, "案件级发起来源被丢弃 → 结果挂不回图上")
        self.assertEqual(got["node_id"], "case#C1:subject:person_aaa")
        self.assertEqual(got["canvas"], CASE_CANVAS)
        self.assertEqual(got["subject"], "张卫国")

    def test_no_context_returns_none(self):
        self.assertIsNone(normalize_origin({}))
        self.assertIsNone(normalize_origin({"subject": "张卫国"}))
        self.assertIsNone(normalize_origin(None))
        self.assertIsNone(normalize_origin("not a dict"))

    def test_unknown_keys_dropped(self):
        """R-3：不落任意结构。"""
        got = normalize_origin({
            "node_id": "case#C1:subject:person_aaa", "__evil__": {"x": 1},
        })
        self.assertIn("node_id", got)
        self.assertNotIn("__evil__", got)

    def test_blank_values_treated_as_missing(self):
        self.assertIsNone(normalize_origin({"clue_id": "  ", "node_id": ""}))


# ----------------------------------------------------------------------
# ⑤ 指纹键
# ----------------------------------------------------------------------
class TestOriginContextKey(unittest.TestCase):
    def test_clue_id_priority(self):
        self.assertEqual(
            origin_context_key({"clue_id": "clue_1",
                                "node_id": "case#C1:subject:person_aaa"}),
            "clue_1")

    def test_fallback_to_node_id(self):
        self.assertEqual(
            origin_context_key({"node_id": "case#C1:subject:person_aaa"}),
            "case#C1:subject:person_aaa")

    def test_empty_when_no_context(self):
        self.assertEqual(origin_context_key({}), "")
        self.assertEqual(origin_context_key(None), "")


# ----------------------------------------------------------------------
# ⑥ 真跑验证：不同发起节点 → 不同观察 id
# ----------------------------------------------------------------------
class TestDirectedObservationId(unittest.TestCase):
    def test_different_origin_node_yields_different_id(self):
        """同镜头 + 同靶心名 + 不同发起节点 → id 必须不同（upsert 不互相覆盖）。

        真实场景：案件级画布上同一姓名有两个节点（已裁决 person_pk 与
        未锚定未裁决节点）。正兵分别在两个节点上跑同一个镜头，若 id 相同，
        后一次会覆盖前一次——图上结论凭空消失。
        """
        lens = "geo_anomaly_track"
        subject = "张卫国"
        a = observation_from_clue(
            _FakeClue(skill_id=lens, subject=subject),
            source="directed",
            origin={"canvas": "case", "node_id": "case#C1:subject:person_aaa"})
        b = observation_from_clue(
            _FakeClue(skill_id=lens, subject=subject),
            source="directed",
            origin={"canvas": "case",
                    "node_id": "case#C1:subject:ecb52c3719fc"})
        self.assertNotEqual(
            a.observation_id, b.observation_id,
            "不同发起节点算出同一观察 id：定向观察会互相覆盖")

    def test_same_origin_is_stable(self):
        """同一发起节点重复跑 → id 稳定（upsert 更新而非堆第二条）。"""
        lens = "geo_anomaly_track"
        origin = {"canvas": "case", "node_id": "case#C1:subject:person_aaa"}
        a = observation_from_clue(_FakeClue(skill_id=lens, subject="张卫国"),
                                  source="directed", origin=dict(origin))
        b = observation_from_clue(_FakeClue(skill_id=lens, subject="张卫国"),
                                  source="directed", origin=dict(origin))
        self.assertEqual(a.observation_id, b.observation_id)

    def test_batch_source_unaffected(self):
        """批量扫描无发起上下文，不加指纹（与既有行为一致）。"""
        lens = "geo_anomaly_track"
        a = observation_from_clue(_FakeClue(skill_id=lens, subject="张卫国"),
                                  source="batch")
        b = observation_from_clue(_FakeClue(skill_id=lens, subject="张卫国"),
                                  source="batch",
                                  origin={"node_id": "case#C1:subject:x"})
        self.assertEqual(a.observation_id, b.observation_id)

    def test_clue_id_still_discriminates(self):
        """既有线索级口径不受影响。"""
        lens = "geo_anomaly_track"
        a = observation_from_clue(_FakeClue(skill_id=lens, subject="张卫国"),
                                  source="directed",
                                  origin={"clue_id": "clue_1"})
        b = observation_from_clue(_FakeClue(skill_id=lens, subject="张卫国"),
                                  source="directed",
                                  origin={"clue_id": "clue_2"})
        self.assertNotEqual(a.observation_id, b.observation_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
