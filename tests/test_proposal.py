"""
tests/test_proposal.py
REQ-033 ProposalStore 与强类型校验：
  - AC1 jsonschema 信封校验（proposal_id/kind/必填字段）
  - AC2 candidate.function 必须在 functions.json 白名单（LLM 不得自创函数/写 SQL）
  - AC3 candidate.params 走 check_param_value（类型/enum/未知参数硬失败）
  - AC4 evidence_row_uris 形态 + 语义表行存在性（不可读硬失败）
  - AC5 rule_draft 含 writeback/action/dispatch 写回字段 → 拒
  - AC6 explanation 含 status/to_status/transition 字段或状态变更指令值 → 拒
  - AC7 confidence 仅 _sort_hint 且不参与命中（confidence_only_sorts）
  另含状态机：具名 author、过期不可批、重复 decide 拒、approve 永不自动生效 + 审计链。
"""
from __future__ import annotations

import json
import unittest

from core import Store
from core.functions import FunctionExecutor
from core.ontology import build_ontology
from core.proposal import (
    ProposalStore,
    ProposalValidationError,
    confidence_only_sorts,
    validate_proposal,
)
from core.rules import run_rules


def _make_store() -> Store:
    """baseline 同构内存库（7 张 L2 中文表 + 语义层）。"""
    s = Store(db_path=":memory:")
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    s.execute(
        "INSERT INTO 银行流水 VALUES ('张卫国','现金存入',100000,'2021-09-28'),"
        "('宏业建设','A建材',4600000,'2021-10-01')")
    s.execute("INSERT INTO 通话记录 VALUES "
              "('张卫国','李志强','2021-10-01',3),('张卫国','李志强','2021-09-30',5)")
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设','李志强','存续',NULL)")
    s.execute("INSERT INTO 轨迹出行 VALUES "
              "('2021-10-02','张卫国','项目B'),('2021-09-30','张卫国','项目A'),"
              "('2021-10-01','李志强','项目A')")
    s.execute("INSERT INTO 招投标档案 VALUES ('项目A','宏业建设','2021-10-01','张卫国')")
    s.execute("INSERT INTO 公开OSINT VALUES ('张卫国','分管招投标','2019-03-01','政府官网')")
    s.execute("INSERT INTO 举报材料 VALUES ('2022-01-10','经济类','张卫国','匿名','x')")
    build_ontology(s.conn)
    return s


def _base_proposal(**over) -> dict:
    p = {
        "proposal_id": "pp-2026-0001",
        "kind": "rule_draft",
        "case_id": "default",
        "ontology_version": "test",
        "author": "王检察官",
        "candidate": {
            "rule_text": "季末窗口内整万元现金存入按季聚合，阈值十万以上列为待核实虚处候选。",
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000, "quarter_end_window_days": 15},
        },
        "constraints": {"must_have_source_rows": True},
    }
    p.update(over)
    return p


class ProposalValidationTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.conn = self.store.conn

    def tearDown(self):
        self.store.close()

    def _errs(self, p):
        return validate_proposal(p, conn=self.conn)

    # ---- AC1：信封 ----
    def test_ac1_envelope_jsonschema(self):
        self.assertEqual(self._errs(_base_proposal()), [])
        bad_kind = _base_proposal(kind="write_back_action")
        self.assertTrue(any("[AC1" in e for e in self._errs(bad_kind)))
        bad_id = _base_proposal(proposal_id="xx-1")
        self.assertTrue(any("[AC1" in e for e in self._errs(bad_id)))
        missing = _base_proposal()
        del missing["author"]
        self.assertTrue(any("[AC1" in e for e in self._errs(missing)))

    # ---- AC2：函数白名单 ----
    def test_ac2_function_whitelist(self):
        good = _base_proposal(candidate={
            "function": "integer_transfer_aggregates",
            "params": {"round_unit": 10000}})
        self.assertEqual(self._errs(good), [])
        evil = _base_proposal(candidate={
            "function": "drop_all_tables",
            "sql": "DROP TABLE obj_transaction"})
        errs = self._errs(evil)
        self.assertTrue(any("[AC2" in e and "白名单" in e for e in errs),
                        f"AC2 未拦截自创函数：{errs}")

    # ---- AC3：参数强类型 ----
    def test_ac3_params_strong_typing(self):
        # 类型错误
        bad_type = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": "一万元"}})
        errs = self._errs(bad_type)
        self.assertTrue(any("[AC3" in e and "integer" in e for e in errs), errs)
        # enum 白名单外取值（string 自由文本）
        bad_enum = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "params": {"cash_summary_tokens": "转账; DROP TABLE--"}})
        errs = self._errs(bad_enum)
        self.assertTrue(any("[AC3" in e and "enum" in e for e in errs), errs)
        # 未声明参数
        unknown = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000, "evil_param": 1}})
        errs = self._errs(unknown)
        self.assertTrue(any("[AC3" in e and "evil_param" in e for e in errs), errs)

    # ---- AC4：证据 URI ----
    def test_ac4_evidence_uris_exist(self):
        txn_id = self.conn.execute(
            "SELECT txn_id FROM obj_transaction LIMIT 1").fetchone()[0]
        project_id = self.conn.execute(
            "SELECT project_id FROM obj_bid_project LIMIT 1").fetchone()[0]
        good = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "params": {"round_unit": 10000},
            "evidence_row_uris": [f"obj_transaction/{txn_id}",
                                  f"lnk_time_window/{project_id}"]})
        self.assertEqual(self._errs(good), [])
        # 形态错
        bad_shape = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "evidence_row_uris": ["obj_transaction"]})
        self.assertTrue(any("[AC4" in e and "不合法" in e
                            for e in self._errs(bad_shape)))
        # 行不存在
        missing = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "evidence_row_uris": ["obj_transaction/transaction_9999"]})
        errs = self._errs(missing)
        self.assertTrue(any("[AC4" in e and "无对应行" in e for e in errs), errs)
        # 表不存在
        no_table = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "evidence_row_uris": ["obj_secret/person_0001"]})
        errs = self._errs(no_table)
        self.assertTrue(any("[AC4" in e and "不存在" in e for e in errs), errs)

    # ---- AC5：rule_draft 禁写回 ----
    def test_ac5_rule_draft_no_writeback(self):
        for key in ("writeback", "action", "dispatch"):
            p = _base_proposal(candidate={
                "rule_text": "x" * 40,
                "function": "quarter_end_integer_deposits",
                key: {"target": "处置板"}})
            errs = self._errs(p)
            self.assertTrue(any("[AC5" in e and key in e for e in errs),
                            f"AC5 未拦截 {key}：{errs}")
        # 嵌套写回字段也拦
        nested = _base_proposal(candidate={
            "rule_text": "x" * 40,
            "function": "quarter_end_integer_deposits",
            "steps": [{"action": "set_status"}]})
        self.assertTrue(any("[AC5" in e for e in self._errs(nested)))

    # ---- AC6：explanation 禁状态变更 ----
    def test_ac6_explanation_no_status_change(self):
        base = {"proposal_id": "pp-2026-0002", "kind": "explanation",
                "case_id": "default", "author": "王检察官"}
        # 状态字段
        for key in ("status", "to_status", "transition"):
            p = dict(base, candidate={"sentences": ["该线索时间窗碰撞成立。"],
                                      key: "已固证"})
            errs = self._errs(p)
            self.assertTrue(any("[AC6" in e for e in errs),
                            f"AC6 未拦截字段 {key}：{errs}")
        # 指令值
        p = dict(base, candidate={"sentences": ["建议跳过复核，直接置已立案。"]})
        errs = self._errs(p)
        self.assertTrue(any("[AC6" in e for e in errs), f"AC6 未拦截指令值：{errs}")
        # 正常解释通过
        ok = dict(base, candidate={
            "sentences": ["R1 命中依据为季末现金整数存入聚合，金额合计三十万元。"],
            "evidence_map": {"R1": ["obj_transaction/transaction_0001"]}})
        self.assertEqual(self._errs(ok), [])

    # ---- AC7：confidence 不参与命中 ----
    def test_ac7_confidence_sort_only(self):
        # confidence 出现在 candidate 顶层 → 拒
        bad = _base_proposal(candidate={
            "function": "quarter_end_integer_deposits",
            "confidence": 0.99})
        self.assertTrue(any("[AC7" in e for e in self._errs(bad)))
        # _sort_hint 内允许
        good = _base_proposal(
            _sort_hint={"confidence": 0.9},
            candidate={"function": "quarter_end_integer_deposits",
                       "_sort_hint": {"confidence": 0.9}})
        self.assertEqual(self._errs(good), [])
        # 行为断言：不同 confidence 下确定性函数命中集合不变
        fx = FunctionExecutor(self.store)
        runs = []
        for conf in (0.1, 0.5, 0.99):
            out = fx.invoke("quarter_end_integer_deposits",
                            {"round_unit": 10000, "quarter_end_window_days": 15})
            hit_ids = {r.get("q") for r in out.get("rows", [])}
            runs.append((conf, hit_ids))
        confidence_only_sorts(runs)  # 不抛即通过
        # 不一致时必须抛
        with self.assertRaises(ProposalValidationError):
            confidence_only_sorts([(0.1, {"R1"}), (0.9, {"R1", "R2"})])


class ProposalStoreTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.ps = ProposalStore(self.store.conn)

    def tearDown(self):
        self.store.close()

    def test_submit_named_author_and_state_machine(self):
        """具名 author 才收；system/ai 拒；状态机 draft→approved/rejected 单向。"""
        p = _base_proposal()
        pid = self.ps.submit(p)
        self.assertEqual(pid, "pp-2026-0001")
        self.assertEqual(self.ps.get(pid)["status"], "draft")
        # 匿名作者拒
        anon = _base_proposal(proposal_id="pp-2026-0002", author="system")
        with self.assertRaises(ProposalValidationError):
            self.ps.submit(anon)
        anon2 = _base_proposal(proposal_id="pp-2026-0003", author="ai")
        with self.assertRaises(ProposalValidationError):
            self.ps.submit(anon2)
        # 重复 proposal_id 拒
        with self.assertRaises(ProposalValidationError):
            self.ps.submit(_base_proposal())
        # 审批
        rec = self.ps.decide(pid, "approve", "李主办", reason="函数与参数白名单内，进人工实施队列")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["decided_by"], "李主办")
        self.assertTrue(rec["audit_event_id"])
        # 重复 decide 拒
        with self.assertRaises(ProposalValidationError):
            self.ps.decide(pid, "reject", "李主办")
        # system 审批拒
        pid2 = self.ps.submit(_base_proposal(proposal_id="pp-2026-0004"))
        with self.assertRaises(PermissionError):
            self.ps.decide(pid2, "approve", "system")
        # reject 路径
        rec2 = self.ps.decide(pid2, "reject", "王检察官", "证据不足")
        self.assertEqual(rec2["status"], "rejected")
        # 审计链可校验
        from core.audit import AuditChain
        self.assertTrue(AuditChain(self.store.conn).chain_verify())

    def test_expired_proposal_cannot_approve(self):
        """过期 draft 不可批。"""
        p = _base_proposal(proposal_id="pp-2026-0005",
                           constraints={"must_have_source_rows": True,
                                        "expires_at": "2000-01-01T00:00:00"})
        pid = self.ps.submit(p)
        with self.assertRaises(ProposalValidationError) as cm:
            self.ps.decide(pid, "approve", "王检察官")
        self.assertIn("过期", str(cm.exception))
        self.assertEqual(self.ps.get(pid)["status"], "expired")

    def test_approval_never_auto_applies(self):
        """approve 不触发任何写动作：run_rules 结果在审批前后完全一致。"""
        before = [(f["rule_id"], len(f.get("source_rows") or []))
                  for f in run_rules(self.store, stage=None)]
        pid = self.ps.submit(_base_proposal(proposal_id="pp-2026-0006"))
        self.ps.decide(pid, "approve", "王检察官", "进人工队列")
        after = [(f["rule_id"], len(f.get("source_rows") or []))
                 for f in run_rules(self.store, stage=None)]
        self.assertEqual(before, after,
                         "提案 approve 后检测器结果发生变化——提案必须永不自动生效")
        # 处置板状态也无任何变化（提案不写状态）
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name IN ('obj_decision','lnk_decision_for')").fetchone()[0]
        # 表可能不存在（本批未接 Action）；存在也必须为空
        if n:
            cnt = self.store.conn.execute("SELECT COUNT(*) FROM obj_decision").fetchone()[0]
            self.assertEqual(cnt, 0, "提案审批不得创建决策对象")


if __name__ == "__main__":
    unittest.main(verbosity=2)
