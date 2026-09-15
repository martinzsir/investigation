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
import unittest.mock
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


def _react_policy(canvas_chat_mode="direct"):
    """构造启用 ReAct 的策略（cloud 档可用）。"""
    return {
        "llm_enabled": True,
        "allowed_models": ["qwen-plus"],
        "canvas_chat_mode": canvas_chat_mode,
        "deployments": {
            "cloud": {
                "enabled": True,
                "allowed_models": ["qwen-plus"],
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "redaction": "strict",
            },
        },
        "pii_redaction": {
            "id_card": "redact", "phone": "redact", "bank_card": "redact",
            "precise_track": "drop", "call_content": "drop",
            "name": "tokenize",
        },
    }


def _mock_react_runner(*, user_prompt, dep, model_name, ctx):
    """模拟 ReAct agent 返回（带引用，与 direct 模式 fake_invoke 同构）。"""
    return (
        "张卫国存入 5 万元为整数万元，符合规则[cite:R1]。"
        "该笔交易发生在 2024-03-15[cite:row:bank_001]。"
        "可能存在结构化存款意图。"
    )


class TestCanvasChatReactMode(unittest.TestCase):
    """ReAct 模式测试：双模式开关 / AgentScope 缺失降级 / 隔离网络拒绝 /
    mock happy path / 只读断言。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        from core.llm.redact import ensure_llm_call_log
        ensure_llm_call_log(self.conn)
        self.ctx = AccessContext(
            operator="王检察官", role="human", case_id="c1",
            purpose="画布问答", network="web")

    def tearDown(self):
        self.conn.close()

    # ---- 模式开关 ----

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_react_mode_with_mock_runner_happy_path(self, mock_load):
        """ReAct 模式 + mock runner → citation_guard 正确分流（与 direct 同构）。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="direct")
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="关键事实是什么？",
            chat_mode="react", react_runner=_mock_react_runner)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(set(result["citations"]), {"R1", "row:bank_001"})

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_mode_switch_policy_default_react(self, mock_load):
        """策略 canvas_chat_mode=react + 请求不指定 → 走 ReAct 路径。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="react")
        # 用 mock runner 验证走了 react 路径（若走 direct 会因无 fake_client 报错）
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="问", react_runner=_mock_react_runner)
        self.assertTrue(result["ok"])
        self.assertIn("R1", result["citations"])

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_mode_switch_request_override_to_react(self, mock_load):
        """策略=direct + 请求 chat_mode=react → 走 ReAct 路径。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="direct")
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="问", chat_mode="react",
            react_runner=_mock_react_runner)
        self.assertTrue(result["ok"])
        self.assertIn("R1", result["citations"])

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_invalid_mode_falls_back_to_direct(self, mock_load):
        """chat_mode='invalid' → 兜底 direct 模式。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="direct")
        client = LLMClient(fake_invoke=lambda **kw: {"content": "回答[cite:R1]。"})
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="问", chat_mode="invalid", llm_client=client)
        self.assertTrue(result["ok"])
        self.assertIn("R1", result["citations"])

    # ---- AgentScope 缺失降级 ----

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_react_mode_agent_scope_missing(self, mock_load):
        """ReAct 模式 + AgentScope 未安装 → react_unavailable 业务错误。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="react")
        # 不传 react_runner → 走默认 _run_react_agent_async → ImportError
        with unittest.mock.patch.object(
                canvas_chat, "_run_react_agent_async") as mock_run:
            mock_run.side_effect = ImportError(
                "No module named 'agentscope'")
            result = canvas_chat.chat_on_canvas(
                conn=self.conn, ctx=self.ctx, doc=_doc(),
                question="问")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "react_unavailable")
        self.assertTrue(any("AgentScope" in w for w in result["warnings"]))

    # ---- 隔离网络拒绝 ----

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_react_mode_isolated_network(self, mock_load):
        """network=isolated → deployment_off（ReAct 模式不可达）。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="react")
        ctx = AccessContext(
            operator="测试员", role="正兵", case_id="c1",
            purpose="画布问答", network="isolated")
        result = canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=ctx, doc=_doc(),
            question="问", chat_mode="react",
            react_runner=_mock_react_runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "deployment_off")

    # ---- 只读断言 ----

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_react_mode_readonly_no_mutation(self, mock_load):
        """ReAct 模式不修改画布文档（只读断言）。"""
        mock_load.return_value = _react_policy(canvas_chat_mode="react")
        doc = _doc()
        import copy
        doc_before = copy.deepcopy(doc)
        canvas_chat.chat_on_canvas(
            conn=self.conn, ctx=self.ctx, doc=doc,
            question="问", chat_mode="react",
            react_runner=_mock_react_runner)
        self.assertEqual(doc, doc_before)


# ----------------------------------------------------------------------
# 流式问答测试
# ----------------------------------------------------------------------

def _stream_policy(canvas_chat_mode="direct"):
    """启用流式的策略（cloud 档可用）。"""
    return {
        "llm_enabled": True,
        "allowed_models": ["qwen-plus"],
        "canvas_chat_mode": canvas_chat_mode,
        "deployments": {
            "cloud": {
                "enabled": True,
                "allowed_models": ["qwen-plus"],
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "redaction": "strict",
            },
        },
        "pii_redaction": {
            "id_card": "redact", "phone": "redact", "bank_card": "redact",
            "precise_track": "drop", "call_content": "drop",
            "name": "tokenize",
        },
    }


class _FakeStreamClient(LLMClient):
    """注入 chat_stream 的假客户端——逐 token 产出预设文本。"""

    def __init__(self, chunks: list[str]):
        super().__init__(fake_invoke=lambda **kw: {"content": ""})
        self._chunks = chunks

    def chat_stream(self, messages, temperature=0.3, max_tokens=2048, **kwargs):
        for ch in self._chunks:
            yield {"delta": ch}


class TestCanvasChatStream(unittest.TestCase):
    """流式问答测试：direct 逐 token / react 一次性 done / 引用校验 / 只读断言。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        from core.llm.redact import ensure_llm_call_log
        ensure_llm_call_log(self.conn)
        self.ctx = AccessContext(
            operator="王检察官", role="human", case_id="c1",
            purpose="画布问答", network="web")

    def tearDown(self):
        self.conn.close()

    def _collect(self, gen) -> list[dict]:
        """消费生成器，返回事件列表。"""
        return list(gen)

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_stream_direct_delta_and_done(self, mock_load):
        """direct 模式逐 token 流式：start → delta* → done（含引用校验）。"""
        mock_load.return_value = _stream_policy("direct")
        chunks = ["张卫国存入 5 万元", "为整数万元，", "符合规则[cite:R1]。"]
        client = _FakeStreamClient(chunks)
        events = self._collect(canvas_chat.chat_on_canvas_stream(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="关键事实？", llm_client=client))

        # 事件序列：start → delta×3 → done
        self.assertEqual(events[0]["event"], "start")
        deltas = [e for e in events if e["event"] == "delta"]
        self.assertEqual(len(deltas), 3)
        self.assertEqual(deltas[0]["data"]["text"], "张卫国存入 5 万元")
        done = [e for e in events if e["event"] == "done"]
        self.assertEqual(len(done), 1)
        # done 含完整文本 + 引用校验结果
        self.assertIn("R1", done[0]["data"]["citations"])
        self.assertEqual(len(done[0]["data"]["facts"]), 1)
        # 完整文本 = 各 delta 拼接
        full = "".join(d["data"]["text"] for d in deltas)
        self.assertEqual(done[0]["data"]["answer"], full)

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_stream_llm_disabled_error_event(self, mock_load):
        """llm_enabled=false → error 事件（非抛异常）。"""
        mock_load.return_value = {"llm_enabled": False,
                                  "allowed_models": ["qwen-plus"]}
        events = self._collect(canvas_chat.chat_on_canvas_stream(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="问"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "error")
        self.assertEqual(events[0]["data"]["error"], "llm_disabled")

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_stream_isolated_network_error_event(self, mock_load):
        """network=isolated → error 事件（deployment_off）。"""
        mock_load.return_value = _stream_policy("direct")
        ctx = AccessContext(
            operator="测试员", role="正兵", case_id="c1",
            purpose="画布问答", network="isolated")
        events = self._collect(canvas_chat.chat_on_canvas_stream(
            conn=self.conn, ctx=ctx, doc=_doc(),
            question="问"))
        self.assertEqual(events[0]["event"], "error")
        self.assertEqual(events[0]["data"]["error"], "deployment_off")

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_stream_react_mode_single_done(self, mock_load):
        """react 模式：不逐 token，一次性发 start → done。"""
        mock_load.return_value = _stream_policy("direct")
        events = self._collect(canvas_chat.chat_on_canvas_stream(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="问", chat_mode="react",
            react_runner=_mock_react_runner))
        # start → done（无 delta）
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[1]["event"], "done")
        deltas = [e for e in events if e["event"] == "delta"]
        self.assertEqual(len(deltas), 0)
        self.assertIn("R1", events[1]["data"]["citations"])

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_stream_readonly_no_mutation(self, mock_load):
        """流式问答不修改画布文档（只读断言）。"""
        mock_load.return_value = _stream_policy("direct")
        doc = _doc()
        import copy
        doc_before = copy.deepcopy(doc)
        chunks = ["回答[cite:R1]。"]
        client = _FakeStreamClient(chunks)
        self._collect(canvas_chat.chat_on_canvas_stream(
            conn=self.conn, ctx=self.ctx, doc=doc,
            question="问", llm_client=client))
        self.assertEqual(doc, doc_before)

    @unittest.mock.patch("server.app.canvas_chat.load_llm_policy")
    def test_stream_llm_error_chunk(self, mock_load):
        """LLM 流式返回 error 块 → error 事件（非崩溃）。"""
        mock_load.return_value = _stream_policy("direct")

        class _ErrClient(LLMClient):
            def __init__(self):
                super().__init__(fake_invoke=lambda **kw: {"content": ""})

            def chat_stream(self, messages, **kwargs):
                yield {"error": "connection reset"}

        client = _ErrClient()
        events = self._collect(canvas_chat.chat_on_canvas_stream(
            conn=self.conn, ctx=self.ctx, doc=_doc(),
            question="问", llm_client=client))
        # start → error
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("connection reset", events[-1]["data"]["message"])


if __name__ == "__main__":
    unittest.main()
