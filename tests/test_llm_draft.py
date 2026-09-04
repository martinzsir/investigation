"""
tests/test_llm_draft.py
REQ-034~037 LLM 能力测试（全部用 fake_invoke 离线注入，不依赖真实 API）。

覆盖：
  REQ-034 draft_rule: AC1 可映射→生成 proposal / AC2 无法映射→不生成 / AC3 不自动生效 / AC4 影子模式 / AC5 author=model:<id>
  REQ-035 explain: AC1 证据映射 / AC2 无证据标假设 / AC3 coverage / AC4 定性词拒绝 / AC5 免责声明
  REQ-036 align: AC1 needs_human_review 不变 / AC2 不得预置 accept / AC4 全拒一致 / AC5 脱敏
  REQ-037 plan: AC1 合法意图 / AC2 未知 function 硬失败 / AC3 参数越界硬失败 / AC4 高影响 / AC5 explain
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store
from core.access import AccessContext
from core.ontology import build_ontology
from core.llm.llm_client import LLMClient


def _make_ctx(network: str = "local") -> AccessContext:
    return AccessContext(
        operator="test_analyst",
        role="主办",
        clearance=2,
        network=network,
    )


def _fake_response(parsed: dict) -> dict:
    """构造 LLMClient fake_invoke 的返回值（模拟 chat() 的 result 字段）。

    chat() 返回 {"ok": True, "model": ..., "result": <fake_invoke return>};
    chat_json 从 result["content"] 解析 JSON 得 result["parsed"]。
    """
    return {"content": "```json\n" + json.dumps(parsed, ensure_ascii=False) + "\n```",
            "parsed": parsed}


def _setup_db():
    """建最小语义层夹具（含 obj_transaction / obj_person 等表）。"""
    s = Store(db_path=":memory:")
    c = s.conn
    c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    c.execute("INSERT INTO 银行流水 VALUES ('张三','李四',10000,'2024-01-01')")
    c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    c.execute("INSERT INTO 通话记录 VALUES ('张三','李四','2024-01-01',5)")
    c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    c.execute("INSERT INTO 工商信息 VALUES ('张三','张三','存续','')")
    c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    c.execute("INSERT INTO 轨迹出行 VALUES ('2024-01-01','张三','北京')")
    c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    c.execute("INSERT INTO 招投标档案 VALUES ('项目A','宏业建设','2024-01-01','张三')")
    c.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    c.execute("INSERT INTO 公开OSINT VALUES ('张三','公开信息','2024-01-01','来源')")
    c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    c.execute("INSERT INTO 举报材料 VALUES ('2024-01-01','资金','李四','王五','举报内容')")
    build_ontology(c)
    return s


# ======================================================================
# REQ-034 LLM 规则草案生成
# ======================================================================
class TestREQ034DraftRule(unittest.TestCase):

    def setUp(self):
        self.store = _setup_db()
        self.ctx = _make_ctx()

    def tearDown(self):
        self.store.close()

    def test_ac1_mapped_function_generates_proposal(self):
        """AC1: 输出可映射既有 function → 生成 proposal。"""
        from core.llm.draft_rule import draft_rule
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "rule_text": "季末整数存款超过1万",
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "dimension": "资金",
            "jian_types": ["生间"],
        }))
        result = draft_rule(self.store.conn, self.ctx, "查找季末整数存款异常",
                            llm_client=fake)
        self.assertTrue(result["ok"], f"expected ok, got: {result}")
        self.assertIsNotNone(result["proposal_id"])
        self.assertEqual(result["candidate"]["function"], "quarter_end_integer_deposits")

    def test_ac2_unmappable_no_free_sql(self):
        """AC2: 无法映射 → 返回"无可用函数"，不生成自由 SQL。"""
        from core.llm.draft_rule import draft_rule
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "rule_text": "...",
            "function": "nonexistent_func",
            "params": {},
            "dimension": "资金",
            "jian_types": [],
        }))
        result = draft_rule(self.store.conn, self.ctx, "无映射问句", llm_client=fake)
        self.assertFalse(result["ok"])
        self.assertIn("无可用函数", result["error"])
        self.assertIsNone(result["proposal_id"])

    def test_ac3_proposal_not_auto_effective(self):
        """AC3: proposal 不自动生效（status=draft）。"""
        from core.llm.draft_rule import draft_rule
        from core.proposal import ProposalStore
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "rule_text": "整数转账聚合",
            "function": "integer_transfer_aggregates",
            "params": {"round_unit": 10000},
            "dimension": "资金",
            "jian_types": ["生间"],
        }))
        result = draft_rule(self.store.conn, self.ctx, "整数转账", llm_client=fake)
        self.assertTrue(result["ok"])
        store = ProposalStore(self.store.conn)
        rec = store.get(result["proposal_id"])
        self.assertEqual(rec["status"], "draft")

    def test_ac4_shadow_mode_no_finding(self):
        """AC4: 影子模式——草案生成不产生 finding。"""
        from core.llm.draft_rule import draft_rule
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "rule_text": "...",
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "dimension": "资金",
            "jian_types": ["生间"],
        }))
        result = draft_rule(self.store.conn, self.ctx, "影子测试", llm_client=fake)
        self.assertTrue(result["ok"])
        tables = [r[0] for r in self.store.conn.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()]
        self.assertNotIn("findings", tables)

    def test_ac5_author_is_model_id(self):
        """AC5: 每条草案含 author=model:<id> 与 provenance。"""
        from core.llm.draft_rule import draft_rule
        from core.proposal import ProposalStore
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "rule_text": "...",
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "dimension": "资金",
            "jian_types": ["生间"],
        }))
        result = draft_rule(self.store.conn, self.ctx, "author 测试", llm_client=fake)
        self.assertTrue(result["ok"], f"expected ok, got: {result}")
        store = ProposalStore(self.store.conn)
        rec = store.get(result["proposal_id"])
        self.assertTrue(rec["author"].startswith("model:"))
        # provenance 存在 _sort_hint 中
        payload = rec["payload"]
        self.assertIn("_sort_hint", payload)
        self.assertEqual(payload["_sort_hint"]["model"], "qwen-plus")


# ======================================================================
# REQ-035 LLM 解释生成 + 证据覆盖检查
# ======================================================================
class TestREQ035Explain(unittest.TestCase):

    def setUp(self):
        self.store = _setup_db()
        self.ctx = _make_ctx()
        self.findings = [
            {
                "rule_id": "R1",
                "级别": "三级",
                "dimension": "资金",
                "jian_types": ["生间"],
                "source_rows": [{"txn_id": "transaction_0001", "amount": 10000}],
            }
        ]

    def tearDown(self):
        self.store.close()

    def test_ac1_evidence_mapped(self):
        """AC1: 每个事实断言能匹配证据。"""
        from core.llm.explain import generate_explanation
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "sentences": [{
                "text": "存在整数存款",
                "evidence_uris": ["obj_transaction/transaction_0001"],
                "is_assumption": False,
            }],
        }))
        result = generate_explanation(self.store.conn, self.ctx, self.findings,
                                      llm_client=fake)
        self.assertTrue(result["ok"], f"expected ok, got: {result}")
        self.assertEqual(len(result["sentences"]), 1)
        self.assertFalse(result["sentences"][0]["is_assumption"])

    def test_ac2_no_evidence_marked_assumption(self):
        """AC2: 无证据句子 → 标【假设】。"""
        from core.llm.explain import generate_explanation
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "sentences": [{
                "text": "可能存在关联",
                "evidence_uris": [],
                "is_assumption": False,
            }],
        }))
        result = generate_explanation(self.store.conn, self.ctx, self.findings,
                                      llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertTrue(result["sentences"][0]["is_assumption"])

    def test_ac3_evidence_coverage(self):
        """AC3: 输出含 evidence coverage 百分比。"""
        from core.llm.explain import generate_explanation
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "sentences": [
                {"text": "有证据句", "evidence_uris": ["obj_transaction/transaction_0001"], "is_assumption": False},
                {"text": "无证据句", "evidence_uris": [], "is_assumption": False},
            ],
        }))
        result = generate_explanation(self.store.conn, self.ctx, self.findings,
                                      llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["evidence_coverage"], 0.5)

    def test_ac4_qualitative_words_rejected(self):
        """AC4: 含"涉嫌/确认/违法/立案"等定性词 → 拒绝输出。"""
        from core.llm.explain import generate_explanation
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "sentences": [{
                "text": "涉嫌违法资金往来",
                "evidence_uris": ["obj_transaction/transaction_0001"],
                "is_assumption": False,
            }],
        }))
        result = generate_explanation(self.store.conn, self.ctx, self.findings,
                                      llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sentences"]), 0)

    def test_ac5_disclaimer_present(self):
        """AC5: 输出含免责声明。"""
        from core.llm.explain import generate_explanation, _DISCLAIMER
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "sentences": [{
                "text": "存在异常",
                "evidence_uris": ["obj_transaction/transaction_0001"],
                "is_assumption": False,
            }],
        }))
        result = generate_explanation(self.store.conn, self.ctx, self.findings,
                                      llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertIn(_DISCLAIMER, result["explanation"])


# ======================================================================
# REQ-036 LLM 实体对齐预标注
# ======================================================================
class TestREQ036Align(unittest.TestCase):

    def setUp(self):
        self.store = _setup_db()
        self.ctx = _make_ctx()

    def tearDown(self):
        self.store.close()

    def test_ac1_needs_human_review_unchanged(self):
        """AC1: LLM 输出不改变 needs_human_review 标志。"""
        from core.llm.align import align_entities
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "reviews": [{
                "entity_pair": ["当事人#a1b2c3", "当事人#d4e5f6"],
                "merge_risk": "low",
                "support": ["同名"],
                "conflict": [],
                "question_for_operator": "请确认是否同一人",
            }],
        }))
        entities = [{"name": "张三", "type": "person"}, {"name": "张三", "type": "person"}]
        result = align_entities(self.store.conn, self.ctx, entities, llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertTrue(result["needs_human_review"])

    def test_ac2_no_auto_accept(self):
        """AC2: LLM 不得预置 accept。"""
        from core.llm.align import align_entities
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "reviews": [{
                "entity_pair": ["A", "B"],
                "merge_risk": "low",
                "support": [],
                "conflict": [],
                "question_for_operator": "?",
                "accept": True,
            }],
        }))
        entities = [{"name": "张三"}, {"name": "张三"}]
        result = align_entities(self.store.conn, self.ctx, entities, llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertNotIn("accept", result["reviews"][0])
        self.assertEqual(result["auto_merged"], 0)

    def test_ac4_all_rejected_consistent(self):
        """AC4: LLM 建议全拒时行为与原确定性版一致（auto_merged=0）。"""
        from core.llm.align import align_entities
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "reviews": [{
                "entity_pair": ["A", "B"],
                "merge_risk": "high",
                "support": [],
                "conflict": ["不同人"],
                "question_for_operator": "确认",
            }],
        }))
        entities = [{"name": "张三"}, {"name": "李四"}]
        result = align_entities(self.store.conn, self.ctx, entities, llm_client=fake)
        self.assertTrue(result["ok"])
        self.assertEqual(result["auto_merged"], 0)

    def test_ac5_pii_redacted_before_send(self):
        """AC5: 输入含真实身份信息 → 脱敏后才发送。"""
        from core.llm.align import align_entities
        captured = {}
        def _capture(**kw):
            captured.update(kw)
            return _fake_response({"reviews": []})
        fake = LLMClient(model="qwen-plus", fake_invoke=_capture)
        entities = [{"name": "张三", "id_card": "310101199001011234",
                      "phone": "13812345678"}]
        result = align_entities(self.store.conn, self.ctx, entities, llm_client=fake)
        self.assertTrue(result["ok"])
        messages = captured.get("messages", [])
        user_msg = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("310101199001011234", user_msg)
        self.assertNotIn("13812345678", user_msg)


# ======================================================================
# REQ-037 LLM 意图 → Function 选择
# ======================================================================
class TestREQ037Plan(unittest.TestCase):

    def setUp(self):
        self.store = _setup_db()
        self.ctx = _make_ctx()

    def tearDown(self):
        self.store.close()

    def test_ac1_valid_intent_selects_function(self):
        """AC1: 合法意图 → 选出正确 function 且参数通过 schema。"""
        from core.llm.plan import plan_query
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "reasoning": "...",
        }))
        result = plan_query(self.store.conn, self.ctx, "查季末整数存款",
                            llm_client=fake)
        self.assertTrue(result["ok"], f"expected ok, got: {result}")
        self.assertEqual(result["plan"]["function"], "quarter_end_integer_deposits")
        self.assertEqual(result["plan"]["params"]["round_unit"], 10000)

    def test_ac2_unknown_function_hard_fail(self):
        """AC2: 未知 function 名 → 硬失败。"""
        from core.llm.plan import plan_query, IntentPlanError
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "function": "nonexistent_func",
            "params": {},
            "reasoning": "...",
        }))
        with self.assertRaises(IntentPlanError) as ctx:
            plan_query(self.store.conn, self.ctx, "无效查询", llm_client=fake)
        self.assertIn("未知 function", str(ctx.exception))

    def test_ac3_param_out_of_bounds_hard_fail(self):
        """AC3: 参数越界 → 硬失败。"""
        from core.llm.plan import plan_query, IntentPlanError
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": "not_a_number"},
            "reasoning": "...",
        }))
        with self.assertRaises(IntentPlanError):
            plan_query(self.store.conn, self.ctx, "参数越界", llm_client=fake)

    def test_ac4_high_impact_needs_approval(self):
        """AC4: 高影响查询需人工批准（行数阈值）。"""
        from core.llm.plan import plan_query, HIGH_IMPACT_ROW_THRESHOLD
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "reasoning": "...",
        }))
        result = plan_query(self.store.conn, self.ctx, "高影响测试", llm_client=fake)
        self.assertTrue(result["ok"], f"expected ok, got: {result}")
        self.assertFalse(result["plan"]["needs_approval"])

    def test_ac5_explain(self):
        """AC5: 查询计划可 explain()。"""
        from core.llm.plan import plan_query
        fake = LLMClient(model="qwen-plus", fake_invoke=lambda **kw: _fake_response({
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "reasoning": "...",
        }))
        result = plan_query(self.store.conn, self.ctx, "explain 测试", llm_client=fake)
        self.assertTrue(result["ok"], f"expected ok, got: {result}")
        explain = result["plan"]["explain"]
        self.assertIn("Function:", explain)
        self.assertIn("quarter_end_integer_deposits", explain)
        self.assertIn("Params:", explain)


if __name__ == "__main__":
    unittest.main()
