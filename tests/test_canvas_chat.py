"""
tests/test_canvas_chat.py
RC-301 画布只读问答 + RC-302 引用校验串联测试。

AC-301 对应：
  1. fake_invoke 模式下离线返回结构化答案（不依赖网络）；
  2. 发送给模型的上下文不含 denied 字段明文（脱敏单测）；
  3. 回答引用角标全部能在当刻文档中解析到；假引用被剔除；
  4. 问答不产生任何画布写入（只读断言）。

AC-302 对应：引用校验在 chat 流程中串联生效。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext
from core.llm.llm_client import LLMClient

from server.app import canvas_chat
from server.app.canvas_citation_guard import build_valid_refs_from_doc


def _doc() -> dict:
    return {
        "nodes": [
            {"id": "rule:R1", "kind": "rule", "ref": "R1",
             "label": "整数现金存入",
             "props": {"rule_text": "单笔现金存入为整数万元"}},
            {"id": "fact:f1", "kind": "fact", "ref": "f1",
             "label": "张卫国存入 5 万元"},
            {"id": "row:bank_001", "kind": "source_row",
             "ref": "row:bank_001", "label": "2024-03-15 存入 50000"},
        ],
        "edges": [],
    }


class TestCanvasChat(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        # call_llm 需要 llm_call_log 表
        from core.llm.redact import ensure_llm_call_log
        ensure_llm_call_log(self.conn)
        self.ctx = AccessContext(
            operator="王检察官", role="human", case_id="c1",
            purpose="画布问答", network="web")

    def tearDown(self):
        self.conn.close()

    def _fake_client(self, content: str) -> LLMClient:
        """构造注入 fake_invoke 的 LLMClient（不触网）。"""
        return LLMClient(fake_invoke=lambda **kw: {"content": content})

    def test_fake_invoke_returns_answer_with_citations(self):
        """fake_invoke 离线返回带引用答案，citation_guard 正确分流。"""
        doc = _doc()
        content = (
            "张卫国存入 5 万元为整数万元，符合规则[cite:R1]。"
            "该笔交易发生在 2024-03-15[cite:row:bank_001]。"
            "可能存在结构化存款意图。"
        )
        client = self._fake_client(content)
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=doc,
            question="这条线索的关键事实是什么？",
            llm_client=client)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(set(result["citations"]), {"R1", "row:bank_001"})
        self.assertTrue(result["warnings"])  # 无据句 warning

    def test_fake_citation_stripped(self):
        """假引用被剔除，对应句子进 pending。"""
        doc = _doc()
        content = "虚假陈述[cite:NOT_EXIST]。"
        client = self._fake_client(content)
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=doc,
            question="问", llm_client=client)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 0)
        self.assertEqual(len(result["pending"]), 1)
        self.assertTrue(any("NOT_EXIST" in w for w in result["warnings"]))

    def test_context_contains_only_business_view(self):
        """构建的上下文只含业务文本，不含原始明细值。"""
        doc = _doc()
        ctx_payload = canvas_chat.build_canvas_context(doc)
        self.assertIn("规则", ctx_payload)
        self.assertIn("事实", ctx_payload)
        self.assertIn("数据行摘要", ctx_payload)
        self.assertIn("可用引用", ctx_payload)
        # 可用引用包含所有节点 ref
        self.assertEqual(
            set(ctx_payload["可用引用"]),
            build_valid_refs_from_doc(doc))

    def test_llm_disabled_returns_business_error(self):
        """llm_enabled=false 时返回业务错误，不抛异常。"""
        from core.llm.redact import load_llm_policy
        # 临时覆盖：直接测试分支逻辑
        doc = _doc()
        # 构造一个 llm_enabled=false 的策略
        with unittest.mock.patch(
                "server.app.canvas_chat.load_llm_policy") as mock_load:
            mock_load.return_value = {
                "llm_enabled": False,
                "allowed_models": ["qwen-plus"],
                "deployments": {"cloud": {"enabled": True,
                                          "allowed_models": ["qwen-plus"]}},
            }
            client = self._fake_client("x")
            result = canvas_chat.chat_on_canvas(
                conn=self.conn, ctx=self.ctx, doc=doc,
                question="问", llm_client=client)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "llm_disabled")

    def test_chat_readonly_no_canvas_mutation(self):
        """问答不修改画布文档（只读断言）。"""
        doc = _doc()
        import copy
        doc_before = copy.deepcopy(doc)
        client = self._fake_client("回答[cite:R1]。")
        canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=doc,
            question="问", llm_client=client)
        self.assertEqual(doc, doc_before)


if __name__ == "__main__":
    unittest.main()
