"""
tests/test_evidence_builder.py
B2 证据三栏产出器测试。

覆盖：
  - 事实栏按行聚合（每条 source_row → 一条 fact）
  - 推断栏（依据文本 + source_rows）
  - 推断无 source_rows → 前端丢弃（partitionEvidence 模拟）
  - 待核实栏（降级标记 + 假设匹配 + rule_text 留痕）
  - 三栏物理分隔（fact 不含推断内容）
  - SourceRef 含伪 row_uri
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.evidence_builder import build_evidence          # noqa: E402


class TestEvidenceBuilder(unittest.TestCase):
    """证据三栏产出器测试。"""

    # ---- 事实栏 ----
    def test_facts_one_per_source_row(self):
        """每条 source_row → 一条事实。"""
        raw = {
            "clue_id": "clue_test1",
            "source_rows": [
                {"raw_name": "宏业建设", "legal_rep": "李志强"},
                {"raw_name": "A建材", "legal_rep": "李志强妻弟"},
            ],
            "detail": {"rule_id": "R5", "依据": "法人/关联人重叠"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        facts = [i for i in items if i["kind"] == "fact"]
        self.assertEqual(len(facts), 2)
        self.assertIn("宏业建设", facts[0]["text"])
        self.assertIn("A建材", facts[1]["text"])

    def test_fact_text_skips_internal_fields(self):
        """事实文本跳过内部字段。"""
        raw = {
            "clue_id": "c1",
            "source_rows": [
                {"raw_name": "张三", "matched_person": ["李四"],
                 "knowledge_sources": ["测试"]},
            ],
            "detail": {"依据": "测试"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        fact = next(i for i in items if i["kind"] == "fact")
        self.assertNotIn("matched_person", fact["text"])
        self.assertNotIn("knowledge_sources", fact["text"])
        self.assertIn("张三", fact["text"])

    # ---- 推断栏 ----
    def test_inference_uses_basis_text(self):
        """推断栏文本取 detail.依据。"""
        raw = {
            "clue_id": "c2",
            "source_rows": [{"from_raw": "张三", "to_raw": "李四", "amount": 100000}],
            "detail": {"rule_id": "R1", "依据": "整数现金存入异常"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        inf = next(i for i in items if i["kind"] == "inference")
        self.assertEqual(inf["text"], "整数现金存入异常")

    def test_inference_has_source_rows(self):
        """推断栏挂全部 source_rows。"""
        raw = {
            "clue_id": "c3",
            "source_rows": [
                {"from_raw": "张三", "to_raw": "李四", "amount": 100000},
                {"from_raw": "张三", "to_raw": "王五", "amount": 50000},
            ],
            "detail": {"依据": "异常存入"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        inf = next(i for i in items if i["kind"] == "inference")
        self.assertEqual(len(inf["source_rows"]), 2)

    def test_inference_without_source_rows_dropped(self):
        """推断无 source_rows → 前端 partitionEvidence 丢弃（模拟）。"""
        raw = {
            "clue_id": "c4",
            "source_rows": [],
            "detail": {"依据": "无数据的推断"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        inf = next(i for i in items if i["kind"] == "inference")
        # source_rows 为空或 None
        self.assertFalse(inf.get("source_rows"))
        # 模拟前端 partitionEvidence 的丢弃逻辑
        dropped = 0
        if not inf.get("source_rows"):
            dropped += 1
        self.assertEqual(dropped, 1)

    # ---- 待核实栏 ----
    def test_degraded_produces_pending(self):
        """is_degraded → 待核实栏降级标记。"""
        raw = {
            "clue_id": "c5",
            "source_rows": [{"q": "2019-01-01", "cnt": 1}],
            "is_degraded": True,
            "detail": {"依据": "测试", "degrade_reason": "阈值不足"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        pending = [i for i in items if i["kind"] == "pending"]
        degraded = [p for p in pending if "降级" in p["text"]]
        self.assertTrue(len(degraded) >= 1)
        self.assertIn("阈值不足", degraded[0]["text"])

    def test_hypothesis_matched_by_rule_id(self):
        """rule_id R1 → 假设 H1 匹配。"""
        raw = {
            "clue_id": "c6",
            "source_rows": [{"from_raw": "张三", "amount": 100000}],
            "detail": {"rule_id": "R1", "依据": "整数现金存入"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        pending = [i for i in items if i["kind"] == "pending"]
        hyp = [p for p in pending if "假设" in p["text"]]
        self.assertTrue(len(hyp) >= 1)
        self.assertIn("H1", hyp[0]["text"])

    def test_rule_text_kept_in_pending(self):
        """rule_text 留痕到待核实栏。"""
        raw = {
            "clue_id": "c7",
            "source_rows": [{"caller_raw": "张三"}],
            "detail": {"rule_id": "R3", "依据": "频次突增",
                        "rule_text": "在公示期通话频次突增"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        pending = [i for i in items if i["kind"] == "pending"]
        rt = [p for p in pending if "规则判据" in p["text"]]
        self.assertTrue(len(rt) >= 1)

    # ---- 三栏物理分隔 ----
    def test_facts_do_not_contain_inference(self):
        """事实栏不含推断内容（FE-T-005 红线）。"""
        raw = {
            "clue_id": "c8",
            "source_rows": [{"raw_name": "宏业建设"}],
            "detail": {"依据": "法人关联重叠"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        facts = [i for i in items if i["kind"] == "fact"]
        inferences = [i for i in items if i["kind"] == "inference"]
        # 事实文本不含推断关键词
        for f in facts:
            self.assertNotIn("法人关联重叠", f["text"])
        # 推断文本不含纯数据字段值
        for inf in inferences:
            self.assertNotIn("宏业建设", inf["text"])

    # ---- SourceRef ----
    def test_source_ref_has_row_uri(self):
        """SourceRef 含伪 row_uri（内容哈希）。"""
        raw = {
            "clue_id": "c9",
            "source_rows": [{"raw_name": "张三"}],
            "detail": {"依据": "测试"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        fact = next(i for i in items if i["kind"] == "fact")
        ref = fact["source_rows"][0]
        self.assertIn("row_uri", ref)
        self.assertIn("@", ref["row_uri"])
        self.assertIn("source", ref)

    def test_source_ref_stable_uri(self):
        """相同内容生成相同 row_uri（稳定性）。"""
        sr = {"raw_name": "张三", "legal_rep": "李四"}
        raw = {
            "clue_id": "c10",
            "source_rows": [sr, sr],
            "detail": {"依据": "测试"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        facts = [i for i in items if i["kind"] == "fact"]
        self.assertEqual(facts[0]["source_rows"][0]["row_uri"],
                         facts[1]["source_rows"][0]["row_uri"])

    # ---- 无 source_rows 的线索 ----
    def test_no_source_rows_no_facts(self):
        """无 source_rows → 无事实栏条目。"""
        raw = {
            "clue_id": "c11",
            "source_rows": [],
            "detail": {"依据": "无数据推断"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        facts = [i for i in items if i["kind"] == "fact"]
        self.assertEqual(len(facts), 0)

    # ---- 无依据的线索 ----
    def test_no_basis_no_inference(self):
        """无依据 → 无推断栏条目。"""
        raw = {
            "clue_id": "c12",
            "source_rows": [{"raw_name": "张三"}],
            "detail": {"rule_id": "R5"},
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        inferences = [i for i in items if i["kind"] == "inference"]
        self.assertEqual(len(inferences), 0)

    # ---- 真实线索结构 ----
    def test_real_finding_r5(self):
        """R5 利益关联线索的三栏产出。"""
        raw = {
            "clue_id": "clue_c134de1f",
            "title": "工商登记利益关联",
            "source_rows": [
                {"raw_name": "宏业建设", "legal_rep": "李志强",
                 "relation": "", "matched_person": ["李志强"]},
                {"raw_name": "A建材", "legal_rep": "李志强妻弟",
                 "relation": "", "matched_person": ["李志强", "李志强妻弟"]},
            ],
            "detail": {
                "rule_id": "R5",
                "依据": "法人/关联人与案件对象人员存在重叠",
                "rule_text": "涉案单位的法定代表人...",
            },
            "jian_types": ["因间"],
        }
        items = build_evidence(raw_clue=raw, pack_id="default")
        facts = [i for i in items if i["kind"] == "fact"]
        inferences = [i for i in items if i["kind"] == "inference"]
        pending = [i for i in items if i["kind"] == "pending"]
        self.assertEqual(len(facts), 2)
        self.assertEqual(len(inferences), 1)
        self.assertTrue(len(pending) >= 1)  # 假设 + rule_text


if __name__ == "__main__":
    unittest.main()
