"""
tests/test_rebuild_planner.py
REQ-018 受影响范围计算 测试。

覆盖 AC1-AC5：
  AC1: 改一个 person → 一跳邻域含其账户/通话/同地点，不含无关主体（org/project）
  AC2: 影响集超阈值 → mode="batch"
  AC3: 10 万行边图规划耗时 < 1s
  AC4: 环状/双向 link 不无限扩散（严格一跳，每条链接处理一次）
  AC5: 空影响集 → mode="skip"，不重建
另含 plan_from_partition 的声明级受影响对象接线（include_objects）。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology import build_ontology                         # noqa: E402
from core.rebuild_planner import (                               # noqa: E402
    plan_from_seeds, plan_from_partition, RebuildPlan,
)
from core.ingest_validate import IngestPartition                 # noqa: E402
from tests.test_ontology_version import make_store               # noqa: E402


def make_store_with_two_hop():
    """夹具：在标准夹具上加一条 李志强→王五 通话（王五是张卫国的两跳邻居）。"""
    s = make_store()
    s.execute("INSERT INTO 通话记录 VALUES ('李志强', '王五', '2021-10-03', 2)")
    return s


class TestPlanFromSeeds(unittest.TestCase):
    def setUp(self):
        self.store = make_store_with_two_hop()
        self.conn = self.store.conn
        # 夹具仅造源表数据；编译语义层（含新增的王五通话）
        build_ontology(self.conn)

    def _pid(self, name: str) -> str:
        return self.conn.execute(
            "SELECT person_id FROM obj_person WHERE raw_name=?", [name]
        ).fetchone()[0]

    def test_ac1_one_hop_neighborhood(self):
        """AC1: 改一个 person → 一跳含账户/通话/邻人，不含无关主体。"""
        zhang = self._pid("张卫国")
        li = self._pid("李志强")
        wang = self._pid("王五")
        plan = plan_from_seeds(self.conn, {"person": {zhang}})

        # 种子本人在影响集
        self.assertIn(zhang, plan.affected_pks["person"])
        # 一跳邻居：李志强（通话 + 同地点）
        self.assertIn(li, plan.affected_pks["person"])
        # 两跳邻居王五不得被回收（严格一跳）
        self.assertNotIn(wang, plan.affected_pks.get("person", set()))

        # 一跳账户：张卫国持有账户（owns）
        zhang_acct = self.conn.execute(
            "SELECT account_id FROM obj_account WHERE raw_name='张卫国'"
        ).fetchone()[0]
        self.assertIn(zhang_acct, plan.affected_pks.get("account", set()))

        # 一跳通话：张卫国相关通话事件外键被回收
        self.assertTrue(plan.affected_pks.get("call"))
        for cid in plan.affected_pks["call"]:
            self.assertTrue(cid.startswith("call_"))

        # 无关主体不在影响集：org / bid_project 及其链接
        self.assertNotIn("org", plan.affected_objects)
        self.assertNotIn("bid_project", plan.affected_objects)
        self.assertNotIn("involved_in", plan.affected_links)
        # 相关链接在
        self.assertIn("calls_to", plan.affected_links)
        self.assertIn("owns", plan.affected_links)
        self.assertIn("co_located", plan.affected_links)

    def test_ac2_batch_mode_over_threshold(self):
        """AC2: 预估行数超阈值 → batch。"""
        zhang = self._pid("张卫国")
        plan = plan_from_seeds(self.conn, {"person": {zhang}}, batch_threshold=1)
        self.assertEqual(plan.mode, "batch")
        self.assertGreater(plan.estimated_rows, 1)

    def test_ac3_100k_edges_under_1s(self):
        """AC3: 10 万行边图，影响范围计算 < 1s。"""
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TABLE obj_person "
                        "(person_id VARCHAR, raw_name VARCHAR, source_rows VARCHAR)")
            con.execute("INSERT INTO obj_person VALUES "
                        "('p1', '张三', 'x'), ('p2', '李四', 'x')")
            con.execute("""
                CREATE TABLE lnk_calls_to AS
                SELECT 'c' || i AS call_id,
                       'p1' AS from_person, 'p2' AS to_person
                FROM range(100000) t(i)
            """)
            t0 = time.perf_counter()
            plan = plan_from_seeds(con, {"person": {"p1"}})
            elapsed = (time.perf_counter() - t0) * 1000
            self.assertLess(elapsed, 1000, f"规划耗时 {elapsed:.0f}ms 超过 1s")
            self.assertEqual(len(plan.affected_pks.get("call", set())), 100000)
        finally:
            con.close()

    def test_ac4_no_infinite_spread_on_cycles(self):
        """AC4: 双向/环状 link 只处理一次，不无限扩散。"""
        zhang = self._pid("张卫国")
        plan = plan_from_seeds(self.conn, {"person": {zhang}})
        # 链接清单无重复（每条链接只处理一次）
        self.assertEqual(len(plan.affected_links), len(set(plan.affected_links)))
        # 两跳外的王五不可达（环 张↔李 不再向外扩）
        wang = self._pid("王五")
        self.assertNotIn(wang, plan.affected_pks.get("person", set()))

    def test_ac5_empty_seeds_skip(self):
        """AC5: 空影响集 → skip，不重建。"""
        plan = plan_from_seeds(self.conn, {"person": set()})
        self.assertEqual(plan.mode, "skip")
        self.assertTrue(plan.is_empty())

        plan2 = plan_from_seeds(self.conn, {"不存在的对象": {"x"}})
        self.assertEqual(plan2.mode, "skip")
        self.assertTrue(plan2.is_empty())

    def test_plan_dataclass_contract(self):
        plan = plan_from_seeds(self.conn, {"person": set()})
        self.assertIsInstance(plan, RebuildPlan)
        d = plan.to_dict()
        for k in ("reason", "mode", "estimated_rows", "elapsed_ms",
                  "affected_objects", "affected_links", "affected_rules"):
            self.assertIn(k, d)


class TestPlanFromPartition(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.conn = self.store.conn
        build_ontology(self.conn)

    def test_direct_objects_and_declared_links_included(self):
        """分区到达：直接受影响对象 + 其声明依赖链接必被标记（含空种子的全新对象）。"""
        # 新分区视图：既有主体 + 全新对手方 + 新日期交易
        self.conn.execute("""
            CREATE VIEW 银行流水_2025Q1 AS
            SELECT '张卫国' AS 主体, '全新对手方' AS 对方,
                   5000.0 AS 金额, '2025-01-15' AS 日期
        """)
        part = IngestPartition(partition_id="银行流水_2025Q1", dataset="银行流水")
        plan = plan_from_partition(self.conn, part)

        self.assertEqual(plan.reason, "partition")
        # binding 直接消费 银行流水 的对象：person(source_sql)/account/transaction
        self.assertIn("transaction", plan.affected_objects)
        self.assertIn("account", plan.affected_objects)
        self.assertIn("person", plan.affected_objects)
        # 声明依赖链接（transfers 端点 account/build_sql 读 obj_transaction；
        # time_window 端点 transaction）——全新对象也不漏标
        self.assertIn("transfers", plan.affected_links)
        self.assertIn("time_window", plan.affected_links)
        self.assertIn("owns", plan.affected_links)
        self.assertNotEqual(plan.mode, "skip")

    def test_unknown_dataset_skips(self):
        """没有任何 binding 消费的数据集 → 空影响集 skip。"""
        self.conn.execute("""
            CREATE VIEW 未知数据_2025Q1 AS
            SELECT 'x' AS a
        """)
        part = IngestPartition(partition_id="未知数据_2025Q1", dataset="未知数据")
        plan = plan_from_partition(self.conn, part)
        self.assertEqual(plan.mode, "skip")
        self.assertTrue(plan.is_empty())


if __name__ == "__main__":
    unittest.main()
