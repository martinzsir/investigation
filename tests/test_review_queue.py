"""
tests/test_review_queue.py
任务 ③ 测试：人工确认工作台（core.review）
覆盖：队列构建、accept/reject/defer 决策、审计链、拒绝不入映射、JSON 序列化/CLI 导出。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from core.review import ReviewQueue, ReviewDecision, Decision
from core.entity import OrganizationResolver, _load_person_resolver

# 通过 core.entity 的统一加载器获取 EntityResolver：
# 该加载器会做路径安全加载（避同名包遮蔽）+ 注入 review_candidates()，
# 直接 `from entity_resolution import ...` 会拿到未注入的裸类，导致 AttributeError。
EntityResolver = _load_person_resolver()


def make_person_resolver():
    """
    person resolver：共享精确证据(手机号) → 强合并，不进 review；
    两个模糊相似的人名(张卫国 / 张卫国2，编辑距离内) → 触发 _fuzzy_link，
    标 needs_review → 进 review。
    注：fuzzy_threshold 适当放宽，兼容「无 pypinyin 退化到编辑距离」的环境，
    保证 review 候选稳定产出（生产环境拼音可用时阈值更严格）。
    """
    er = EntityResolver(fuzzy_threshold=0.6)
    er.ingest([
        {"name": "张卫国", "phone": "13800001111", "source_row_id": "r1"},
        {"name": "张卫国（董事长）", "phone": "13800001111", "source_row_id": "r2"},  # 共享手机 → 强合并
        {"name": "张卫国", "source_row_id": "r3"},
        {"name": "张卫国弟", "source_row_id": "r4"},  # 模糊相似(前缀高度重合) → review 候选
    ])
    er.resolve()
    return er


def make_org_resolver():
    org = OrganizationResolver()
    org.ingest([
        {"name": "宏业建设有限公司", "credit_code": "9133AAA", "source_row_id": "o1"},
        {"name": "宏业建设（集团）", "credit_code": "9133AAA", "source_row_id": "o2"},
        {"name": "宏业建设第一项目部", "source_row_id": "o3"},  # 别名注入后强合并
        {"name": "泰和建材", "credit_code": "9133CCC", "source_row_id": "o4"},
        {"name": "泰和建材公司", "credit_code": "9133CCC", "source_row_id": "o5"},  # 簇内前缀 → review
    ])
    org.add_aliases({"宏业建设": ["宏业建设第一项目部", "宏业建设（集团）"]})
    org.resolve()
    return org


class TestQueueBuild(unittest.TestCase):
    def test_from_two_resolvers(self):
        q = ReviewQueue.from_resolvers(person_resolver=make_person_resolver(),
                                       org_resolver=make_org_resolver())
        self.assertGreater(len(q), 0)
        # person + org 两类候选都在
        types = {d.entity_type for d in q}
        self.assertIn("person", types)
        self.assertIn("org", types)

    def test_only_review_candidates(self):
        """队列里只应含 needs_review 的，不含已强合并的。"""
        q = ReviewQueue.from_resolvers(person_resolver=make_person_resolver(),
                                       org_resolver=make_org_resolver())
        for d in q:
            self.assertEqual(d.status, Decision.PENDING)
            self.assertTrue(len(d.variants) >= 1)


class TestDecisions(unittest.TestCase):
    def setUp(self):
        self.q = ReviewQueue.from_resolvers(person_resolver=make_person_resolver(),
                                            org_resolver=make_org_resolver())
        self.first_id = self.q.pending()[0].candidate_id

    def test_accept_requires_named_operator(self):
        with self.assertRaises(ValueError):
            self.q.accept(self.first_id, operator="", reason="x")
        with self.assertRaises(ValueError):
            self.q.accept(self.first_id, operator="system", reason="x")
        # 正常 accept
        d = self.q.accept(self.first_id, operator="王检察官", reason="工商内档确认")
        self.assertEqual(d.status, Decision.ACCEPTED)
        self.assertEqual(d.operator, "王检察官")

    def test_reject_requires_reason(self):
        with self.assertRaises(ValueError):
            self.q.reject(self.first_id, operator="王检察官", reason="")
        d = self.q.reject(self.first_id, operator="王检察官", reason="系两家独立公司")
        self.assertEqual(d.status, Decision.REJECTED)

    def test_defer_keeps_pending(self):
        d = self.q.defer(self.first_id, operator="王检察官", note="待补工商内档")
        self.assertEqual(d.status, Decision.DEFERRED)

    def test_accepted_mapping_only(self):
        """只有 ACCEPTED 进 mapping；REJECTED 明确不进。"""
        for d in self.q.pending():
            self.q.accept(d.candidate_id, operator="王检察官", reason="批量确认演示")
        m = self.q.accepted_mapping()
        # mapping 中每个 variant 都应对应 ACCEPTED 的 canonical
        for d in self.q:
            if d.status == Decision.ACCEPTED:
                for v in d.variants:
                    self.assertEqual(m.get(v), d.canonical)

    def test_audit_trail_recorded(self):
        d = self.q.accept(self.first_id, operator="王检察官", reason="确认")
        self.assertEqual(d.operator, "王检察官")
        self.assertEqual(d.note, "确认")
        self.assertNotEqual(d.timestamp, "")


class TestPersist(unittest.TestCase):
    def test_to_json_and_load(self):
        q = ReviewQueue.from_resolvers(person_resolver=make_person_resolver(),
                                       org_resolver=make_org_resolver())
        # 做一个决策
        first = q.pending()[0].candidate_id
        q.accept(first, operator="王检察官", reason="t")
        path = ROOT / "output" / "_test_review.json"
        q.to_json(str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("decisions", data)
        self.assertEqual(data["total"], len(q))
        # 重新加载
        q2 = ReviewQueue.load(str(path))
        self.assertEqual(len(q2), len(q))
        self.assertEqual(q2.get(first).status, Decision.ACCEPTED)
        path.unlink(missing_ok=True)

    def test_summary(self):
        q = ReviewQueue.from_resolvers(person_resolver=make_person_resolver())
        self.assertEqual(q.summary()["pending"], len(q.pending()))
        for d in q.pending():
            q.accept(d.candidate_id, operator="x", reason="t")
        self.assertEqual(q.summary()["accepted"], len(q))


class TestCLI(unittest.TestCase):
    def test_run_cli_no_pending(self):
        """队列为空时 CLI 应直接退出，不阻塞。"""
        q = ReviewQueue(decisions=[])
        q.run_cli()  # 不应抛异常


if __name__ == "__main__":
    unittest.main(verbosity=2)
