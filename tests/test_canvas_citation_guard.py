"""
tests/test_canvas_citation_guard.py
RC-302 引用强制校验纯函数测试（server/app/canvas_citation_guard.py）。

AC-302 对应：
  1. 有据句保留在事实段；无据句进待核实时段；假引用被剔除并记 warning；
  2. 事实段引用覆盖率 100%（零裸句断言）；
  3. warning 列表随响应返回；
  4. 纯函数无 LLM 依赖，全部离线测试。

覆盖 5 种 fixture：有句/无句/假引用/空输出/全待核实。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.canvas_citation_guard import (
    build_valid_refs_from_doc,
    validate_citations,
)


class TestCitationGuard(unittest.TestCase):
    """RC-302 引用校验五 fixture。"""

    def test_mixed_fixture(self):
        """3 有据 + 2 无据 + 1 假引用：有据入 facts，无据入 pending，假引用剔除。"""
        text = (
            "张卫国向海州建材转账 5 万元，为整数万元存入[cite:R1]。"
            "该笔交易发生在 2024-03-15[cite:row:bank_001]。"
            "海州建材为壳公司，无实际经营[cite:FAKE_REF]。"
            "资金可能流向境外。"
            "建议核查海州建材的工商登记。"
        )
        valid = {"R1", "row:bank_001"}
        result = validate_citations(text, valid)

        # 3 有据句（R1、row:bank_001、假引用那句因有效引用数为 0 → pending）
        # 实际：第 1 句 cites=[R1] valid；第 2 句 cites=[row:bank_001] valid；
        # 第 3 句 cites=[FAKE_REF] 无效 → pending；第 4、5 句无引用 → pending
        self.assertEqual(result["fact_count"], 2)
        self.assertEqual(result["pending_count"], 3)
        self.assertEqual(result["citation_count"], 2)
        self.assertFalse(result["all_facts_cited"])  # 有 pending

        # 假引用被剔除并记 warning
        self.assertTrue(any("FAKE_REF" in w for w in result["warnings"]))

        # 有据句文本不含引用标记
        for f in result["facts"]:
            self.assertNotIn("[cite:", f["sentence"])
            self.assertTrue(f["citations"])

    def test_all_cited(self):
        """全部有据：all_facts_cited=True，pending 为空。"""
        text = "事实一[cite:R1]。事实二[cite:row:001]。"
        result = validate_citations(text, {"R1", "row:001"})
        self.assertEqual(result["fact_count"], 2)
        self.assertEqual(result["pending_count"], 0)
        self.assertTrue(result["all_facts_cited"])
        self.assertEqual(result["warnings"], [])

    def test_all_pending(self):
        """全部无据：全部进 pending，facts 为空。"""
        text = "这是推测一。这是推测二。"
        result = validate_citations(text, {"R1"})
        self.assertEqual(result["fact_count"], 0)
        self.assertEqual(result["pending_count"], 2)
        self.assertFalse(result["all_facts_cited"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_empty_output(self):
        """空输出：facts/pending/warnings 全空，不抛异常。"""
        result = validate_citations("", {"R1"})
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["fact_count"], 0)
        self.assertFalse(result["all_facts_cited"])

    def test_fake_citation_only(self):
        """仅假引用：句子进 pending，假引用记 warning，不进 facts。"""
        text = "虚假陈述[cite:NOT_EXIST]。"
        result = validate_citations(text, {"R1"})
        self.assertEqual(result["fact_count"], 0)
        self.assertEqual(result["pending_count"], 1)
        self.assertTrue(any("NOT_EXIST" in w for w in result["warnings"]))

    def test_citation_count_dedup_by_sentence(self):
        """同一句多引用：citation_count 累加有效引用数。"""
        text = "双引用句[cite:R1][cite:R2]。"
        result = validate_citations(text, {"R1", "R2"})
        self.assertEqual(result["citation_count"], 2)
        self.assertEqual(result["facts"][0]["citations"], ["R1", "R2"])

    def test_build_valid_refs_from_doc(self):
        """从画布文档提取所有节点 ref。"""
        doc = {
            "nodes": [
                {"id": "rule:R1", "kind": "rule", "ref": "R1"},
                {"id": "row:bank_001", "kind": "source_row", "ref": "row:bank_001"},
                {"id": "cn_h1", "kind": "hypothesis", "ref": "hyp_1"},
            ],
            "edges": [],
        }
        refs = build_valid_refs_from_doc(doc)
        self.assertEqual(refs, {
            "R1", "row:bank_001", "@local#row/bank_001", "hyp_1",
        })


if __name__ == "__main__":
    unittest.main()
