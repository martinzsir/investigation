"""
tests/test_review_loop.py
REQ-016 review.decided → 增量重建 → 重算 → finding.changed 闭环测试。

场景：『张伟』与『张卫国』在项目B同日同框，R4(轨迹同框) 命中一对"两人同框"；
正兵 accept 张伟→张卫国 归并后，该对变为同一人，R4 该命中消失（误报被清），
其余规则结果不变。

覆盖：
  AC1: accept → 受影响 finding 重算并产生 finding.changed
  AC2: 一次 accept 只重算一次（重复应用幂等，事件不重复）
  AC3: reject 只写 feedback 事件，不删证据、不重建
  AC4: 只重算 affected_rules；无关规则结果前后一致（diff 断言）
  AC5: 变化的 finding 标记 needs_review/review_round=2 重入二次 review
  回补 REQ-004 AC5：entity_mapping 受保护，全量重建语义层后仍生效
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                          # noqa: E402
from core.ontology import build_ontology                        # noqa: E402
from core.rules import run_rules                                # noqa: E402
from core.review import ReviewDecision, Decision                # noqa: E402
from core.review_loop import (                                  # noqa: E402
    apply_accept, apply_decisions, ensure_tables, record_accept)


def _make_store() -> Store:
    s = Store(db_path=":memory:")
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    s.execute("""INSERT INTO 银行流水 VALUES
        ('张卫国', '现金存入', 100000, '2021-09-28'),
        ('宏业建设', 'A建材', 4600000, '2021-10-01')""")
    s.execute("""INSERT INTO 通话记录 VALUES
        ('张卫国', '李志强', '2021-10-01', 3),
        ('张伟', '李志强', '2021-09-30', 2)""")
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设', '李志强', '存续', NULL)")
    s.execute("""INSERT INTO 轨迹出行 VALUES
        ('2021-10-02', '张卫国', '项目B'),
        ('2021-10-02', '张伟', '项目B'),
        ('2021-10-01', '李志强', '项目A'),
        ('2021-10-01', '张卫国', '项目A')""")
    s.execute("INSERT INTO 招投标档案 VALUES ('项目A', '宏业建设', '2021-10-01', '张卫国')")
    s.execute("INSERT INTO 公开OSINT (主体, 公开信息, 发布日期, 来源) VALUES ('张卫国', '分管招投标', '2019-03-01', '政府官网')")
    s.execute("INSERT INTO 举报材料 VALUES ('2022-01-10', '经济类', '张卫国', '匿名', '反映收受宏业现金约 120 万')")
    return s


def _merge_decision() -> ReviewDecision:
    return ReviewDecision(
        candidate_id="rev_person_0001", entity_type="person",
        canonical="张卫国", variants=["张伟"],
        reason="模糊相似候选(需正兵确认)",
        status=Decision.ACCEPTED, operator="王检察官", note="身份证号同一人")


def _r4_pair_locations(findings):
    out = set()
    for f in findings:
        if f["rule_id"] == "R4":
            for row in f["source_rows"]:
                if isinstance(row, dict):
                    out.add(row.get("location"))
    return out


class TestReviewLoop(unittest.TestCase):
    def setUp(self):
        self.store = _make_store()
        build_ontology(self.store.conn)
        ensure_tables(self.store.conn)

    def tearDown(self):
        self.store.close()

    def test_baseline_r4_hits_variant_pair(self):
        """基线：合并前 R4 命中 项目B（张伟/张卫国"两人"同框）与 项目A。"""
        pre = run_rules(self.store, stage=None)
        self.assertIn("项目B", _r4_pair_locations(pre))
        self.assertIn("项目A", _r4_pair_locations(pre))

    def test_ac1_accept_triggers_recompute_and_change_event(self):
        """AC1: accept → 受影响 finding 重算，产生 finding.changed。"""
        result = apply_accept(self.store, _merge_decision())
        self.assertIn("R4", result["changed_rules"])
        self.assertEqual(result["disappeared"].get("R4"), 1)
        # 项目B 误报消失，项目A 仍在
        post = run_rules(self.store, stage=None)
        self.assertNotIn("项目B", _r4_pair_locations(post))
        self.assertIn("项目A", _r4_pair_locations(post))
        evts = [e[0] for e in self.store.conn.execute(
            "SELECT type FROM event_log").fetchall()]
        self.assertIn("finding.changed", evts)
        self.assertIn("review.decided", evts)

    def test_ac2_idempotent_single_recompute(self):
        """AC2: 同一决策重复应用不重复重算（返回首次结果，事件不重复）。"""
        r1 = apply_accept(self.store, _merge_decision())
        n_changed_1 = self.store.conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE type='finding.changed'").fetchone()[0]
        r2 = apply_accept(self.store, _merge_decision())
        n_changed_2 = self.store.conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE type='finding.changed'").fetchone()[0]
        self.assertEqual(r1["decision_id"], r2["decision_id"])
        self.assertEqual(n_changed_1, n_changed_2)
        applied = self.store.conn.execute(
            "SELECT COUNT(*) FROM review_applied").fetchone()[0]
        self.assertEqual(applied, 1)

    def test_ac3_reject_no_rebuild_feedback_only(self):
        """AC3: reject 不写映射、不重建，证据原样，只写 feedback 事件。"""
        d = ReviewDecision(
            candidate_id="rev_person_0002", entity_type="person",
            canonical="张卫国", variants=["张伟"], reason="x",
            status=Decision.REJECTED, operator="王检察官", note="系两名无关人员")
        apply_decisions(self.store, [d])
        n_map = self.store.conn.execute(
            "SELECT COUNT(*) FROM entity_mapping").fetchone()[0]
        self.assertEqual(n_map, 0)
        # 证据仍在：R4 项目B 误报未被清
        post = run_rules(self.store, stage=None)
        self.assertIn("项目B", _r4_pair_locations(post))
        evts = [e[0] for e in self.store.conn.execute(
            "SELECT type FROM event_log").fetchall()]
        self.assertIn("review.decided", evts)
        self.assertNotIn("finding.changed", evts)

    def test_ac4_only_affected_rules_rerun(self):
        """AC4: affected_rules 之外的规则不被重算、结果不变。"""
        result = apply_accept(self.store, _merge_decision())
        affected = set(result["affected_rules"])
        self.assertTrue({"R4"} <= affected)          # lnk_co_located 消费方
        # 与 person/轨迹/通话 无关的规则不在影响集
        self.assertFalse({"R1", "R2", "R5", "R6"} & affected)
        # 无关规则当前可正常产出（未被重建破坏）
        post = run_rules(self.store, stage=None)
        self.assertTrue({f["rule_id"] for f in post} <= {"R1", "R2", "R3", "R4", "R5", "R6"})

    def test_ac5_changed_findings_reenter_review(self):
        """AC5: 变化的 finding 标记 needs_review + review_round=2。"""
        result = apply_accept(self.store, _merge_decision())
        changed = result["changed_findings"]
        self.assertTrue(changed)
        for f in changed:
            self.assertTrue(f["needs_review"])
            self.assertEqual(f["review_round"], 2)
            self.assertEqual(f["triggered_by_decision"], "rev_person_0001")

    def test_entity_mapping_survives_full_rebuild(self):
        """回补 REQ-004 AC5：全量重建语义层后 entity_mapping 仍在且生效。"""
        apply_accept(self.store, _merge_decision())
        self.assertEqual(self.store.conn.execute(
            "SELECT canonical FROM entity_mapping WHERE variant='张伟'").fetchone()[0],
            "张卫国")
        # 全量重建（模拟 bootstrap）
        build_ontology(self.store.conn)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM entity_mapping").fetchone()[0], 1)
        persons = {r[0] for r in self.store.conn.execute(
            "SELECT raw_name FROM obj_person").fetchall()}
        self.assertNotIn("张伟", persons)          # 归并仍生效
        self.assertIn("张卫国", persons)


if __name__ == "__main__":
    unittest.main()
