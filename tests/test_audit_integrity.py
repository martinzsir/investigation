"""
tests/test_audit_integrity.py
REQ-G-018 审计链完整性自检（在既有 chain_verify 上扩展）：
  - 既有回归：chain_verify() 仍返回 bool，健康链 True / 篡改 False
  - 新增：chain_integrity() 返回结构化结论
      {chain_ok, expected_count, actual_count, broken_links, missing_fields}
  - 记录数核对：删除中间一条 → expected(max seq) != actual，且断链
  - 字段完备性：operator 空 / after_state 空 / ontology_version=unknown 均入 missing_fields
  - 结论进健康度：断链 critical、字段缺失 warning，落 run_diagnostic(audit_integrity_gap)
"""
from __future__ import annotations

import unittest

from core import Store
from core.audit import AuditChain
from core.run_health import RunHealth


def _append(chain, operator="张三", after=None, version="v2026.1", rows=("r1",)):
    return chain.append(
        operator=operator, before=None,
        after={"s": 1} if after is None else after,
        source_row_ids=list(rows), ontology_version=version)


class AuditIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.conn = self.store.conn
        self.health = RunHealth(self.conn)
        self.chain = AuditChain(self.conn, health=self.health)

    def tearDown(self):
        self.store.close()

    def _diag_kinds(self):
        return [(d["kind"], d["severity"]) for d in self.health.rows()]

    def test_healthy_chain_ok(self):
        for i in range(3):
            _append(self.chain, rows=(f"r{i}",))
        # 既有回归：chain_verify bool 仍为 True
        self.assertTrue(self.chain.chain_verify())
        integ = self.chain.chain_integrity()
        self.assertTrue(integ["chain_ok"])
        self.assertEqual(integ["expected_count"], 3)
        self.assertEqual(integ["actual_count"], 3)
        self.assertEqual(integ["broken_links"], [])
        self.assertEqual(integ["missing_fields"], [])

    def test_unknown_version_flagged_missing(self):
        # G-007 联动：ontology_version=unknown → missing_fields 记 ontology_version
        _append(self.chain, version="unknown")
        integ = self.chain.chain_integrity()
        self.assertFalse(integ["chain_ok"])
        fields = integ["missing_fields"][0]["fields"]
        self.assertIn("ontology_version", fields)
        # 链本身仍自洽（签名基于标注后的 after），不断链
        self.assertEqual(integ["broken_links"], [])

    def test_empty_operator_flagged(self):
        _append(self.chain, operator="")
        integ = self.chain.chain_integrity()
        self.assertFalse(integ["chain_ok"])
        self.assertIn("operator", integ["missing_fields"][0]["fields"])

    def test_tamper_breaks_and_critical_diag(self):
        _append(self.chain, rows=("a",))
        _append(self.chain, rows=("b",))
        # 篡改第一条 after_state
        self.conn.execute(
            "UPDATE audit_chain SET after_state='{\"s\": 999}' "
            "WHERE seq = 1")
        self.assertFalse(self.chain.chain_verify())  # 既有回归
        integ = self.chain.chain_integrity()
        self.assertFalse(integ["chain_ok"])
        self.assertEqual(len(integ["broken_links"]), 1)
        # 断链 → critical 诊断
        self.assertIn(("audit_integrity_gap", "critical"), self._diag_kinds())

    def test_delete_middle_count_gap(self):
        _append(self.chain, rows=("a",))
        _append(self.chain, rows=("b",))
        _append(self.chain, rows=("c",))
        self.conn.execute(
            "DELETE FROM audit_chain WHERE seq = 2")
        integ = self.chain.chain_integrity()
        self.assertFalse(integ["chain_ok"])
        self.assertEqual(integ["expected_count"], 3)
        self.assertEqual(integ["actual_count"], 2)
        self.assertTrue(integ["broken_links"])  # 删除导致 prev_hash 断链

    def test_missing_field_warning_diag(self):
        _append(self.chain, operator="")  # operator 空 → warning
        self.chain.chain_integrity()
        self.assertIn(("audit_integrity_gap", "warning"), self._diag_kinds())

    def test_empty_chain_is_ok(self):
        # 空链：无记录即无缺口（不制造噪声告警）
        integ = self.chain.chain_integrity()
        self.assertTrue(integ["chain_ok"])
        self.assertEqual(integ["expected_count"], 0)
        self.assertEqual(integ["actual_count"], 0)
        self.assertEqual(integ["disposal_events"], 0)

    def test_disposal_unwired_flags_warning(self):
        # REQ-G-025 AC5：处置动作有持久痕迹但链上零处置事件（接线缺失）
        # → chain_ok=False 且落 warning；纯查询空链不报警（见上一用例）
        from core.registry import LineageClue
        from core.lineage import save_statuses
        from core.action_executor import ActionExecutor
        # 证据源 1：处置结果已落 clue_disposal_status（状态变更只走内存，模拟脱链）
        clue = LineageClue(title="脱链线索")
        clue.set_status("查证中", operator="王检察官", note="已核查")
        save_statuses(self.conn, [clue])
        # 证据源 2：两阶段 action_request 已到 confirmed（_apply 理应已落链）
        ActionExecutor(self.store)  # 仅为建 action_request 表
        self.conn.execute(
            "INSERT INTO action_request (action_id, idempotency_key, action_name, "
            "clue_id, target_status, params_json, status, submitted_by, submitted_at, "
            "attempts) VALUES ('act_t1', 'k', 'verify', 'clue_x', '查证中', '{}', "
            "'confirmed', '王检察官', '2026-09-05 00:00:00', 1)")
        integ = self.chain.chain_integrity()
        self.assertFalse(integ["chain_ok"])
        self.assertEqual(integ["disposal_events"], 0)
        self.assertEqual(integ["disposal_activity"]["persisted_non_pending"], 1)
        self.assertEqual(integ["disposal_activity"]["actions_applied"], 1)
        self.assertIn(("audit_integrity_gap", "warning"), self._diag_kinds())

    def test_wired_disposal_no_unwired_alarm(self):
        # REQ-G-025：处置经 ActionExecutor 落链后（版本锚点有效），不报脱链、chain_ok=True
        from core.registry import LineageClue
        from core.action_executor import ActionExecutor
        from core.ontology_version import record_version, OntologyVersion
        record_version(self.conn, OntologyVersion(
            pack="default", schema_version=2, ontology_version="2.9.9",
            build_id="test-anchor", built_at="2026-09-05T00:00:00",
            source_watermark="", ontology_watermark=""))
        ex = ActionExecutor(self.store, health=self.health)
        clue = LineageClue(title="落链线索")
        ex.execute("verify", clue, "王检察官", {"note": "已核查"})
        integ = self.chain.chain_integrity()
        self.assertEqual(integ["disposal_events"], 1)
        self.assertTrue(integ["chain_ok"])
        self.assertNotIn(("audit_integrity_gap", "warning"), self._diag_kinds())
        self.assertNotIn(("audit_integrity_gap", "critical"), self._diag_kinds())


if __name__ == "__main__":
    unittest.main(verbosity=2)
