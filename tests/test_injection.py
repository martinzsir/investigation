"""
tests/test_injection.py
REQ-039 提示注入防护：
  - AC1 "忽略指令/标记已核实"语料 → high 隔离；恶意候选经 sanitize 丢字段 +
    proposal AC6 拒 + assert_no_status_change 拦；处置状态零变更
  - AC2 诱导 SQL 语料 → high（sql_injection）；自创函数候选被函数白名单拒（无自由 SQL 通道）
  - AC3 "跳过复核/自动批准"语料 → high；needs_human_review/auto_approve 字段被丢；
    ai 身份不能 submit/decide（无 auto-approve 路径，批准必须具名自然人）
  - AC4 4 份夹具语料全部扫出 high 命中（CI 即本测试组）
  - AC5 raw_evidence_fragment 可取回原始证据片段（过 access 对象级策略，无权即拒）
"""
from __future__ import annotations

import unittest
from pathlib import Path

from core import Store
from core.access import AccessContext
from core.llm.guard import (
    assert_no_status_change,
    raw_evidence_fragment,
    sanitize_candidate,
    scan_bundle,
    scan_text,
    wrap_untrusted,
)
from core.ontology import build_ontology
from core.proposal import ProposalStore, validate_proposal

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "injection"


def _make_store() -> Store:
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


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _explanation_proposal(candidate: dict, pid: str = "pp-inj-0001") -> dict:
    return {
        "proposal_id": pid,
        "kind": "explanation",
        "case_id": "default",
        "author": "王检察官",
        "candidate": candidate,
    }


class InjectionGuardTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.conn = self.store.conn
        self.ps = ProposalStore(self.conn)

    def tearDown(self):
        self.store.close()

    # ---- AC4：4 份夹具全部命中（先跑，CI 锁语料库）----
    def test_ac4_all_fixtures_detected(self):
        expected = {
            "ignore_instructions.txt": "ignore_previous",
            "induce_sql.txt": "sql_injection",
            "skip_review.txt": "skip_review",
            "role_play.txt": "role_play",
        }
        for fname, pat in expected.items():
            text = _fixture(fname)
            high = {h.pattern_id for h in scan_text(text) if h.severity == "high"}
            self.assertIn(pat, high,
                          f"AC4 失败：{fname} 未扫出 {pat}，实际 {sorted(high)}")

    # ---- AC1：忽略指令 + 标记已核实 ----
    def test_ac1_ignore_instructions_quarantined(self):
        text = _fixture("ignore_instructions.txt")
        high = [h for h in scan_text(text) if h.severity == "high"]
        self.assertTrue(any(h.pattern_id == "ignore_previous" for h in high))
        self.assertTrue(any(h.pattern_id == "override_verdict" for h in high))
        # 整帧隔离，不进模型上下文
        rep = scan_bundle([wrap_untrusted(text, source="举报材料#tipoff-1")])
        self.assertFalse(rep["clean"])
        self.assertEqual(len(rep["quarantined"]), 1)
        self.assertEqual(rep["safe_frames"], [])
        # 恶意模型候选：status/to_status 被白名单丢弃
        raw = {"sentences": ["核查结果如下。"],
               "status": "已核实", "to_status": "已固证", "confidence": 0.99}
        clean, dropped = sanitize_candidate(raw, "explanation")
        self.assertIn("status", dropped)
        self.assertIn("to_status", dropped)
        self.assertNotIn("status", clean)
        # guard 层直接拦
        with self.assertRaises(PermissionError):
            assert_no_status_change(raw, "explanation")
        # 即使绕过 sanitize 混进提案，AC6 硬校验拒
        errs = validate_proposal(_explanation_proposal(raw), conn=self.conn)
        self.assertTrue(any("[AC6" in e for e in errs), errs)
        # 处置面零变更：无决策对象
        n = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'obj_decision'").fetchone()[0]
        if n:
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM obj_decision").fetchone()[0], 0)

    # ---- AC2：诱导 SQL ----
    def test_ac2_sql_injection_blocked(self):
        text = _fixture("induce_sql.txt")
        high = {h.pattern_id for h in scan_text(text) if h.severity == "high"}
        self.assertIn("sql_injection", high)
        rep = scan_bundle([wrap_untrusted(text, source="外部邮件#1")])
        self.assertEqual(len(rep["quarantined"]), 1)
        # 恶意候选夹带 SQL 通道：sql 字段非白名单被丢；自创函数被白名单拒
        raw = {"rule_text": "执行数据修正" + "x" * 40,
               "function": "drop_audit_chain",
               "sql": "DROP TABLE audit_chain",
               "params": {}}
        clean, dropped = sanitize_candidate(raw, "rule_draft")
        self.assertIn("sql", dropped)
        self.assertNotIn("sql", clean)
        p = {
            "proposal_id": "pp-inj-0002",
            "kind": "rule_draft",
            "case_id": "default",
            "author": "王检察官",
            "candidate": raw,
        }
        errs = validate_proposal(p, conn=self.conn)
        self.assertTrue(any("[AC2" in e for e in errs),
                        f"AC2 失败：自创函数未被白名单拦截：{errs}")
        # 内核不存在任何自由 SQL 执行通道：FunctionExecutor 只消费 functions.json
        from core.functions import FunctionExecutor
        with self.assertRaises(KeyError):
            FunctionExecutor(self.store).invoke("drop_audit_chain", {})

    # ---- AC3：跳过复核 / 自动批准 ----
    def test_ac3_skip_review_no_auto_approve(self):
        text = _fixture("skip_review.txt")
        high = {h.pattern_id for h in scan_text(text) if h.severity == "high"}
        self.assertIn("skip_review", high)
        self.assertIn("override_verdict", high)
        # 候选试图关闭人工复核
        raw = {"sentences": ["证据充分。"],
               "needs_human_review": False, "auto_approve": True}
        clean, dropped = sanitize_candidate(raw, "explanation")
        self.assertIn("needs_human_review", dropped)
        self.assertIn("auto_approve", dropped)
        with self.assertRaises(PermissionError):
            assert_no_status_change(raw, "explanation")
        # ai 身份不能提交提案
        good = _explanation_proposal(
            {"sentences": ["R1 命中依据为季末现金整数存入。"]}, pid="pp-inj-0003")
        good["author"] = "ai"
        with self.assertRaises(Exception):
            self.ps.submit(good)
        # ai 身份不能审批；只有具名自然人显式 decide 才生效
        good["author"] = "王检察官"
        good["proposal_id"] = "pp-inj-0004"
        pid = self.ps.submit(good)
        with self.assertRaises(PermissionError):
            self.ps.decide(pid, "approve", "ai")
        with self.assertRaises(PermissionError):
            self.ps.decide(pid, "approve", "system")
        rec = self.ps.decide(pid, "approve", "王检察官", reason="人工复核后进入实施队列")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["decided_by"], "王检察官")
        # 提案 approve 不产生任何决策/状态副作用
        n = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'obj_decision'").fetchone()[0]
        if n:
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM obj_decision").fetchone()[0], 0)

    # ---- AC5：原始证据片段取回 + access 策略 ----
    def test_ac5_raw_evidence_fragment_with_access(self):
        txn_id = self.conn.execute(
            "SELECT txn_id FROM obj_transaction LIMIT 1").fetchone()[0]
        uri = f"obj_transaction/{txn_id}"
        # 正兵可读资金明细（min_clearance=1）
        frag = raw_evidence_fragment(
            self.conn, uri,
            access=AccessContext(operator="正兵甲", role="正兵", network="isolated"))
        self.assertEqual(frag["table"], "obj_transaction")
        self.assertEqual(frag["rows"][0]["txn_id"], txn_id)
        self.assertIn("amount", frag["columns"])
        # 见习无权读资金明细 → 拒（fail-closed）
        with self.assertRaises(PermissionError):
            raw_evidence_fragment(
                self.conn, uri,
                access=AccessContext(operator="见习甲", role="见习", network="isolated"))
        # 链接证据
        project_id = self.conn.execute(
            "SELECT project_id FROM obj_bid_project LIMIT 1").fetchone()[0]
        lfrag = raw_evidence_fragment(
            self.conn, f"lnk_time_window/{project_id}",
            access=AccessContext(operator="正兵甲", role="正兵", network="isolated"))
        self.assertGreaterEqual(len(lfrag["rows"]), 1)
        # 非法/不存在 URI
        with self.assertRaises(ValueError):
            raw_evidence_fragment(self.conn, "garbage_uri")
        with self.assertRaises(KeyError):
            raw_evidence_fragment(self.conn, "obj_transaction/txn_9999")
        # system 旁路
        sfrag = raw_evidence_fragment(self.conn, uri)
        self.assertEqual(sfrag["rows"][0]["txn_id"], txn_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
