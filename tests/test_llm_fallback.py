"""
tests/test_llm_fallback.py
REQ-040 LLM 降级开关与影子模式。

  AC1 LLM 关闭（llm_enabled=false）→ 四类草案能力全部 degraded 回落，
      零提案、零模型调用；确定性功能不受影响
  AC2 影子模式（mode=shadow）：草案运行只允许 proposal/llm_call_log/audit_chain
      三表变化（shadow_diff 快照断言），语义层 obj_*/lnk_* 零写入
  AC3 一键开关：llm_state 对 llm_enabled 显式判定（未声明=交下游闸门）
  AC4 降级发生时 llm_call_log + AuditChain 留痕，哈希链校验通过
  AC5 关闭 LLM 后确定性只读 Function 结果与未启用时一致

全部用 LLMClient(fake_invoke=...) 离线注入，不依赖真实 API。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store  # noqa: E402
from core.access import AccessContext  # noqa: E402
from core.audit import AuditChain  # noqa: E402
from core.functions import invoke_function  # noqa: E402
from core.llm.fallback import llm_state, shadow_diff, table_snapshot  # noqa: E402
from core.llm.llm_client import LLMClient  # noqa: E402
from core.llm.redact import load_llm_policy  # noqa: E402
from core.ontology import build_ontology  # noqa: E402

MODEL = "qwen-plus"


def _make_ctx(network: str = "local") -> AccessContext:
    return AccessContext(operator="test_analyst", role="主办",
                         clearance=2, network=network)


def _fake_response(parsed: dict) -> dict:
    return {"content": "```json\n" + json.dumps(parsed, ensure_ascii=False) + "\n```",
            "parsed": parsed}


def _fake(parsed: dict) -> LLMClient:
    return LLMClient(model=MODEL, fake_invoke=lambda **kw: _fake_response(parsed))


def _setup_db() -> Store:
    """最小语义层夹具（与 test_llm_draft 同构的七张中文源表）。"""
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


def _proposal_count(conn) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM proposal").fetchone()[0]
    except Exception:
        return 0


class FallbackTests(unittest.TestCase):

    def setUp(self):
        self.store = _setup_db()
        self.conn = self.store.conn
        self.ctx = _make_ctx()
        base = load_llm_policy("default")
        self.off = dict(base)
        self.off["llm_enabled"] = False
        self.on = dict(base)  # default 策略 llm_enabled=true / mode=shadow
        self.findings = [{
            "rule_id": "R1", "级别": "A", "dimension": "资金",
            "jian_types": ["生间"],
            "source_rows": [{"txn_id": "transaction_0001", "amount": 10000}],
        }]
        self.entities = [{"name": "张三", "type": "person"},
                         {"name": "张三", "type": "person"}]

    def tearDown(self):
        self.store.close()

    # ---- AC3：开关判定 ----
    def test_ac3_kill_switch_state(self):
        self.assertFalse(llm_state({"llm_enabled": False})["enabled"])
        self.assertTrue(llm_state({"llm_enabled": True})["enabled"])
        # 未声明 = 能力门放行，由 network/模型白名单闸门 fail-closed
        self.assertTrue(llm_state({})["enabled"])
        self.assertIn("llm_enabled", llm_state({"llm_enabled": False})["reason"])

    # ---- AC1：关闭后四类能力全部 degraded ----
    def test_ac1_all_capabilities_degrade_when_off(self):
        from core.llm.draft_rule import draft_rule
        from core.llm.explain import generate_explanation
        from core.llm.align import align_entities
        from core.llm.plan import plan_query

        r1 = draft_rule(self.conn, self.ctx, "通话异常？",
                        llm_client=_fake({
                            "rule_text": "同一主体短期通话次数超阈值列为异常",
                            "function": "call_frequency_spike",
                            "params": {"absolute_threshold": 20},
                            "dimension": "通讯", "jian_types": ["生间"]}),
                        policy=self.off)
        r2 = generate_explanation(self.conn, self.ctx, self.findings,
                                  llm_client=_fake({"sentences": [{
                                      "text": "存在整数存款",
                                      "evidence_uris": ["obj_transaction/transaction_0001"],
                                      "is_assumption": False}]}),
                                  policy=self.off)
        r3 = align_entities(self.conn, self.ctx, self.entities,
                            llm_client=_fake({"reviews": [{
                                "entity_pair": ["A", "B"], "merge_risk": "low",
                                "support": ["同名"], "conflict": [],
                                "question_for_operator": "请确认是否同一人"}]}),
                            policy=self.off)
        r4 = plan_query(self.conn, self.ctx, "查通话异常",
                        llm_client=_fake({
                            "function": "call_frequency_spike",
                            "params": {"absolute_threshold": 20},
                            "reasoning": "..."}),
                        policy=self.off)
        for i, r in enumerate((r1, r2, r3, r4), 1):
            self.assertFalse(r["ok"], f"能力 {i} 应降级：{r}")
            self.assertTrue(r.get("degraded"), f"能力 {i} 缺 degraded 标记：{r}")
        # 零提案写入
        self.assertEqual(_proposal_count(self.conn), 0)

    # ---- AC2：影子模式隔离 ----
    def test_ac2_shadow_mode_isolates_drafts(self):
        from core.llm.draft_rule import draft_rule
        from core.llm.explain import generate_explanation
        from core.llm.align import align_entities

        before = table_snapshot(self.conn)
        r1 = draft_rule(self.conn, self.ctx, "整数转账异常？",
                        llm_client=_fake({
                            "rule_text": "整数转账聚合异常",
                            "function": "integer_transfer_aggregates",
                            "params": {"round_unit": 10000},
                            "dimension": "资金", "jian_types": ["生间"]}),
                        policy=self.on)
        r2 = generate_explanation(self.conn, self.ctx, self.findings,
                                  llm_client=_fake({"sentences": [{
                                      "text": "存在整数存款",
                                      "evidence_uris": ["obj_transaction/transaction_0001"],
                                      "is_assumption": False}]}),
                                  policy=self.on)
        r3 = align_entities(self.conn, self.ctx, self.entities,
                            llm_client=_fake({"reviews": [{
                                "entity_pair": ["当事人#a", "当事人#b"],
                                "merge_risk": "low",
                                "support": ["同名"], "conflict": [],
                                "question_for_operator": "请确认是否同一人"}]}),
                            policy=self.on)
        after = table_snapshot(self.conn)
        self.assertTrue(r1["ok"] and r2["ok"] and r3["ok"], (r1, r2, r3))
        # 只允许 proposal / llm_call_log / audit_chain 变化；
        # 语义层 obj_*/lnk_*、线索表、发件箱等任何增量都算越界
        diff = shadow_diff(before, after)
        self.assertEqual(diff, {}, f"影子模式越界写入：{diff}")
        # 仅 draft_rule 落 draft 提案（explain/align 只回结果不建提案）
        self.assertEqual(_proposal_count(self.conn), 1)

    # ---- AC4：降级留痕 + 链校验 ----
    def test_ac4_degradation_audited(self):
        from core.llm.draft_rule import draft_rule
        draft_rule(self.conn, self.ctx, "通话异常？",
                   llm_client=_fake({
                       "rule_text": "x", "function": "call_frequency_spike",
                       "params": {}, "dimension": "通讯", "jian_types": []}),
                   policy=self.off)
        logs = self.conn.execute(
            "SELECT allowed, blocked_reason FROM llm_call_log").fetchall()
        self.assertTrue(any(a is False and "llm_enabled" in (b or "")
                            for a, b in logs), logs)
        self.assertTrue(AuditChain(self.conn).chain_verify())

    # ---- AC5：确定性功能等价 ----
    def test_ac5_deterministic_functions_unaffected(self):
        from core.llm.draft_rule import draft_rule
        from core.llm.plan import plan_query

        before = invoke_function(self.store, "call_frequency_spike",
                                 {"absolute_threshold": 30})
        # LLM 降级尝试不影响确定性结果
        draft_rule(self.conn, self.ctx, "通话异常？",
                   llm_client=_fake({
                       "rule_text": "x", "function": "call_frequency_spike",
                       "params": {}, "dimension": "通讯", "jian_types": []}),
                   policy=self.off)
        plan_query(self.conn, self.ctx, "查通话",
                   llm_client=_fake({
                       "function": "call_frequency_spike",
                       "params": {"absolute_threshold": 30},
                       "reasoning": "..."}),
                   policy=self.off)
        after = invoke_function(self.store, "call_frequency_spike",
                                {"absolute_threshold": 30})
        self.assertEqual(json.dumps(before, sort_keys=True, default=str),
                         json.dumps(after, sort_keys=True, default=str))
        self.assertEqual(_proposal_count(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
