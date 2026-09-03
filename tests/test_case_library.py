"""
tests/test_case_library.py
REQ-031 案例库（case_fragment）四质量门：
  - AC1 已固证/已排除终态线索可沉淀（verified/excluded）
  - AC2 待查/查证中线索拒绝沉淀（未核验不得入库）；状态与 outcome 不一致拒绝
  - AC3 含真实姓名/身份证号 → 脱敏门拒绝（必须用 当事人#token）
  - AC4 按 rule_id / 关键词 / outcome 检索命中
  - AC5 legal_basis + rule_version + ontology_version + redaction_hash 齐全可溯源，审计链可校验
"""
from __future__ import annotations

import unittest

from core import Store
from core.audit import AuditChain
from core.case_library import (
    CaseLibraryError,
    search,
    settle_fragment,
)
from core.lineage import ensure_status_table
from core.ontology import build_ontology

_PATTERN_R1 = ("R1 规则适用条件：季末窗口内整万元现金存入按季聚合，"
               "当事人#a1b2c3 多笔整数存入达阈值，主体为个人非对公单位。")
_LEGAL = "《中华人民共和国刑事诉讼法》第一百一十五条（测试援引）"
_EVIDENCE = {"evidence_uris": ["obj_transaction/txn_0001"],
             "note": "当事人#a1b2c3 指代原始主体，真名不出库"}


def _make_store() -> Store:
    s = Store(db_path=":memory:")
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    build_ontology(s.conn)
    ensure_status_table(s.conn)
    for cid, st in [("clue-001", "已固证"), ("clue-002", "已排除"),
                    ("clue-003", "待查"), ("clue-004", "查证中")]:
        s.conn.execute(
            "INSERT INTO clue_disposal_status (clue_id, status, note, operator) "
            "VALUES (?, ?, '', '王检察官')", [cid, st])
    return s


class CaseLibraryTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.conn = self.store.conn

    def tearDown(self):
        self.store.close()

    def _settle(self, clue_id="clue-001", outcome="verified", **kw):
        kwargs = dict(clue_id=clue_id, rule_id="R1", outcome=outcome,
                      legal_basis=_LEGAL, operator="王检察官",
                      pattern=_PATTERN_R1, evidence=_EVIDENCE,
                      confidence=0.8)
        kwargs.update(kw)
        return settle_fragment(self.conn, **kwargs)

    # ---- AC1：终态可沉淀 ----
    def test_ac1_terminal_clues_settle(self):
        fid1 = self._settle(clue_id="clue-001", outcome="verified")
        self.assertTrue(fid1.startswith("cf-"))
        fid2 = self._settle(clue_id="clue-002", outcome="excluded",
                            confidence=0.4)
        self.assertTrue(fid2.startswith("cf-"))
        rows = search(self.conn, rule_id="R1")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["outcome"] for r in rows}, {"verified", "excluded"})

    # ---- AC2：非终态拒绝 ----
    def test_ac2_non_terminal_rejected(self):
        for cid in ("clue-003", "clue-004"):
            with self.assertRaises(CaseLibraryError) as cm:
                self._settle(clue_id=cid)
            self.assertIn("终态门", str(cm.exception))
        # 状态与 outcome 不一致
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(clue_id="clue-001", outcome="excluded")
        self.assertIn("不一致", str(cm.exception))
        # 不存在的线索
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(clue_id="clue-999")
        self.assertIn("终态门", str(cm.exception))

    # ---- AC3：脱敏门 ----
    def test_ac3_redaction_gate(self):
        # 真实姓名
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(pattern="R1 规则：张卫国在季末窗口多笔整数现金存入达阈值。")
        self.assertIn("脱敏门", str(cm.exception))
        self.assertIn("张卫国", str(cm.exception))
        # 身份证形态
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(pattern="R1 规则：当事人证件 310101199001011234 对应账户整数存入。")
        self.assertIn("脱敏门", str(cm.exception))
        # evidence 里藏真名同样拒
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(evidence={"note": "李志强账户收款"})
        self.assertIn("脱敏门", str(cm.exception))
        # 适用条件门：pattern 不含 rule_id / 过短
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(pattern="整数存入")
        self.assertIn("适用条件门", str(cm.exception))
        # legal_basis 空
        with self.assertRaises(CaseLibraryError) as cm:
            self._settle(legal_basis="  ")
        self.assertIn("legal_basis", str(cm.exception))
        # confidence 越界
        with self.assertRaises(CaseLibraryError):
            self._settle(confidence=1.5)

    # ---- AC4：检索 ----
    def test_ac4_search(self):
        self._settle()
        self._settle(clue_id="clue-002", outcome="excluded", confidence=0.3,
                     pattern=_PATTERN_R1.replace("达阈值", "未达阈值，排除"))
        # rule_id 过滤
        self.assertEqual(len(search(self.conn, rule_id="R1")), 2)
        self.assertEqual(len(search(self.conn, rule_id="R99")), 0)
        # outcome 过滤
        self.assertEqual(len(search(self.conn, rule_id="R1", outcome="verified")), 1)
        # 关键词
        self.assertEqual(
            len(search(self.conn, rule_id="R1", keyword="季末窗口")), 2)
        self.assertEqual(
            len(search(self.conn, rule_id="R1", keyword="排除")), 1)

    # ---- AC5：溯源字段 + 审计链 ----
    def test_ac5_provenance_and_audit(self):
        fid = self._settle()
        rows = search(self.conn, rule_id="R1")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["fragment_id"], fid)
        self.assertTrue(r["legal_basis"])
        self.assertTrue(r["rule_version"])
        self.assertEqual(len(r["rule_version"]), 16)
        self.assertTrue(r["ontology_version"])
        self.assertTrue(r["redaction_hash"])
        self.assertEqual(len(r["redaction_hash"]), 64)
        self.assertEqual(r["created_by"], "王检察官")
        self.assertEqual(r["clue_id"], "clue-001")
        self.assertEqual(r["confidence"], 0.8)
        self.assertTrue(r["audit_event_id"])
        # 审计链可校验
        chain = AuditChain(self.conn)
        self.assertTrue(chain.chain_verify())
        ev = self.conn.execute(
            "SELECT after_state FROM audit_chain WHERE event_id = ?",
            [r["audit_event_id"]]).fetchone()
        import json
        after = json.loads(ev[0])
        self.assertEqual(after["action"], "case_fragment_settled")
        self.assertEqual(after["outcome"], "verified")
        self.assertEqual(after["rule_version"], r["rule_version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
