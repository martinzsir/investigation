"""
tests/test_parameters.py
REQ-032 参数治理：
  - AC1 变更恒新版本：draft 两次 → version_seq 递增、旧值保留不覆盖
  - AC2 未审批不生效：pending 提案/shadow 版本不进 effective_values（只读 production）
  - AC3 shadow_compare：两组 values 各跑白名单 Function，差异可量化（added/removed）
  - AC4 rollback：production 回退到旧版本，effective 与函数结果复原
  - AC5 provenance 落盘（metrics_run_id/sample_size/basis 随版本可读）
  - AC6 样本量 <20 拒入审批
"""
from __future__ import annotations

import json
import unittest

from core import Store
from core.audit import AuditChain
from core.functions import FunctionExecutor
from core.ontology import build_ontology
from core.ontology_loader import load_pack
from core.parameters import (
    MIN_SAMPLE_SIZE,
    ParameterGovernanceError,
    approve,
    draft_set,
    effective_set_id,
    effective_values,
    get_set,
    list_sets,
    propose,
    reject,
    rollback,
    shadow_compare,
)


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


class ParameterGovernanceTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.conn = self.store.conn
        self.rule = load_pack("default").rules["R1"]

    def tearDown(self):
        self.store.close()

    def _draft(self, values, provenance=None, operator="王检察官"):
        return draft_set(self.conn, scope="R1", values=values,
                         provenance=provenance or {"metrics_run_id": "run-1",
                                                   "sample_size": 120,
                                                   "basis": "precision>=0.9"},
                         operator=operator)

    # ---- AC1：版本恒新 ----
    def test_ac1_version_never_overwritten(self):
        v1 = self._draft({"round_unit": 10000})
        v2 = self._draft({"round_unit": 300000})
        self.assertEqual((v1["version_seq"], v2["version_seq"]), (1, 2))
        self.assertEqual(v1["set_id"], "ps-R1-v1")
        self.assertEqual(v2["set_id"], "ps-R1-v2")
        # 旧值保留
        self.assertEqual(get_set(self.conn, v1["set_id"])["values"],
                         {"round_unit": 10000})
        self.assertEqual(get_set(self.conn, v2["set_id"])["values"],
                         {"round_unit": 300000})
        self.assertEqual([s["version_seq"] for s in list_sets(self.conn, "R1")],
                         [1, 2])

    # ---- AC2：未审批不生效 ----
    def test_ac2_unapproved_not_effective(self):
        self.assertEqual(effective_values(self.conn, "R1"), {})
        v1 = self._draft({"round_unit": 10000})
        v2 = self._draft({"round_unit": 300000})
        # pending 提案不影响生效值
        pid = propose(self.conn, set_id=v2["set_id"], evidence={"finding_diff": []},
                      risk="中", operator="王检察官", sample_size=120)
        self.assertEqual(effective_values(self.conn, "R1"), {})
        self.assertIsNone(effective_set_id(self.conn, "R1"))
        # shadow 审批也不生效
        approve(self.conn, pid, "王检察官", mode="shadow")
        self.assertEqual(effective_values(self.conn, "R1"), {})
        # production 审批才生效
        v3 = self._draft({"round_unit": 50000})
        pid3 = propose(self.conn, set_id=v3["set_id"], evidence={}, risk="低",
                       operator="王检察官", sample_size=200)
        approve(self.conn, pid3, "王检察官", mode="production")
        self.assertEqual(effective_values(self.conn, "R1"), {"round_unit": 50000})
        self.assertEqual(effective_set_id(self.conn, "R1"), v3["set_id"])
        # 被 reject 的提案不生效
        v4 = self._draft({"round_unit": 999})
        pid4 = propose(self.conn, set_id=v4["set_id"], evidence={}, risk="高",
                       operator="王检察官", sample_size=200)
        reject(self.conn, pid4, "王检察官", "证据不成立")
        self.assertEqual(effective_values(self.conn, "R1"), {"round_unit": 50000})

    # ---- AC3：影子比对 ----
    def test_ac3_shadow_compare(self):
        v1 = self._draft({"round_unit": 10000})
        v2 = self._draft({"round_unit": 300000})
        rep = shadow_compare(self.conn, v1["set_id"], v2["set_id"],
                             self.store, "R1")
        self.assertEqual(rep["function"], self.rule.function)
        self.assertEqual(rep["old_count"], 1)   # 10 万整数现金存入命中
        self.assertEqual(rep["new_count"], 0)   # 30 万为单位 → 10 万不命中
        self.assertEqual(len(rep["removed"]), 1)
        self.assertEqual(rep["added"], [])
        self.assertFalse(rep["stable"])
        # 同参数比对 → stable
        v3 = self._draft({"round_unit": 10000})
        rep2 = shadow_compare(self.conn, v1["set_id"], v3["set_id"],
                              self.store, "R1")
        self.assertTrue(rep2["stable"])

    # ---- AC4：回滚 ----
    def test_ac4_rollback_restores(self):
        v1 = self._draft({"round_unit": 10000})
        v2 = self._draft({"round_unit": 300000})
        pid = propose(self.conn, set_id=v2["set_id"], evidence={}, risk="中",
                      operator="王检察官", sample_size=120)
        approve(self.conn, pid, "王检察官", mode="production")
        self.assertEqual(effective_values(self.conn, "R1"),
                         {"round_unit": 300000})
        # 新参数下 R1 函数零命中
        fx = FunctionExecutor(self.store)
        merged = dict(self.rule.params); merged.update({"round_unit": 300000})
        self.assertEqual(len(fx.invoke(self.rule.function, merged)["rows"]), 0)
        # 回滚到 v1
        rb = rollback(self.conn, scope="R1", rollback_version=1,
                      operator="王检察官", reason="误报率升高")
        self.assertEqual(effective_values(self.conn, "R1"),
                         {"round_unit": 10000})
        self.assertEqual(effective_set_id(self.conn, "R1"), v1["set_id"])
        self.assertEqual(get_set(self.conn, v2["set_id"])["status"], "retired")
        # 函数结果复原
        merged = dict(self.rule.params); merged.update(effective_values(self.conn, "R1"))
        self.assertEqual(len(fx.invoke(self.rule.function, merged)["rows"]), 1)
        self.assertTrue(rb["audit_event_id"])
        # 审计链可校验
        self.assertTrue(AuditChain(self.conn).chain_verify())

    # ---- AC5：provenance 落盘 ----
    def test_ac5_provenance_persisted(self):
        prov = {"metrics_run_id": "run-20260904-01", "sample_size": 340,
                "basis": "后验精确率 0.93 / 命中率 0.12，阈值策略建议"}
        v = self._draft({"round_unit": 10000}, provenance=prov)
        got = get_set(self.conn, v["set_id"])
        self.assertEqual(got["provenance"]["metrics_run_id"],
                         "run-20260904-01")
        self.assertEqual(got["provenance"]["sample_size"], 340)
        self.assertIn("精确率", got["provenance"]["basis"])
        # 审批记录也带审计
        pid = propose(self.conn, set_id=v["set_id"],
                      evidence={"precision": 0.93}, risk="低",
                      operator="王检察官", sample_size=340)
        rec = approve(self.conn, pid, "王检察官", mode="production")
        self.assertTrue(rec["audit_event_id"])
        row = self.conn.execute(
            "SELECT provenance FROM parameter_set WHERE set_id = ?",
            [v["set_id"]]).fetchone()
        self.assertEqual(json.loads(row[0])["metrics_run_id"],
                         "run-20260904-01")

    # ---- AC6：样本量门 ----
    def test_ac6_small_sample_rejected(self):
        v = self._draft({"round_unit": 10000})
        with self.assertRaises(ParameterGovernanceError) as cm:
            propose(self.conn, set_id=v["set_id"], evidence={}, risk="中",
                    operator="王检察官", sample_size=MIN_SAMPLE_SIZE - 1)
        self.assertIn(str(MIN_SAMPLE_SIZE), str(cm.exception))
        # 缺样本量同样拒（fail-closed）
        with self.assertRaises(ParameterGovernanceError):
            propose(self.conn, set_id=v["set_id"], evidence={}, risk="中",
                    operator="王检察官", sample_size=None)
        # 边界值 20 可入审批
        pid = propose(self.conn, set_id=v["set_id"], evidence={}, risk="中",
                      operator="王检察官", sample_size=20)
        self.assertTrue(pid.startswith("pp-"))
        # 重复审批拒
        approve(self.conn, pid, "王检察官", mode="shadow")
        with self.assertRaises(ParameterGovernanceError):
            approve(self.conn, pid, "王检察官", mode="production")


if __name__ == "__main__":
    unittest.main(verbosity=2)
