"""
tests/test_relation_functions.py
P4 关系研判 Function 测试（relation_neighborhood / relation_common_neighbors /
relation_paths，语义层统一图，研判能力插件化 v3 §4-P4）。

覆盖：
  ① 跨边类型 BFS 圈层（资金/通话/持有混合，跳数正确）；
  ② 共同邻居交集（两侧关联边可溯源）；
  ③ 路径枚举（跨类型 3 跳链、深度不足零命中、环剪枝）；
  ④ 主体不存在 → hit=False 降级不崩；
  ⑤ 缺边表结构降级（gaps 记录，其余边照常）；
  ⑥ 同名歧义 ValueError、标识符白名单、参数范围校验；
  ⑦ edge_kinds 收窄。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store
from core.functions import FunctionExecutor


def make_graph_store() -> Store:
    """内存库 + 手造语义表（列名与真实 build_sql 输出一致）。

    图样例：
      资金：宏业建设(account_hy) → A建材(account_ajc) → 张卫国配偶(account_zwp)
      持有：account_hy ← 持有 ← 张三(person_zhang)
      通话：张三 → 王五(person_wang) (c1)；李四(person_li) → 王五 (c2)
      同框：李四 / 孙七(person_sun) 同地点 (tk1)（不与资金/持有圈层相交，
            保证宏业建设 2 跳内不含李四）
      组织：宏业建设集团(org_hy) 中标 项目P1(project_p1)；
            重名组织(org_dup) 亦参与 P1（有边，供 target_type 消歧后 hit）
    """
    s = Store(db_path=":memory:")
    c = s.conn
    c.execute("CREATE TABLE obj_person (person_id VARCHAR, raw_name VARCHAR)")
    c.executemany("INSERT INTO obj_person VALUES (?, ?)", [
        ("person_zhang", "张三"), ("person_li", "李四"),
        ("person_wang", "王五"), ("person_dup", "重名"),
        ("person_sun", "孙七"),
    ])
    c.execute("CREATE TABLE obj_account (account_id VARCHAR, raw_name VARCHAR)")
    c.executemany("INSERT INTO obj_account VALUES (?, ?)", [
        ("account_hy", "宏业建设"), ("account_ajc", "A建材"),
        ("account_zwp", "张卫国配偶"),
    ])
    c.execute("CREATE TABLE obj_org (org_id VARCHAR, raw_name VARCHAR)")
    c.executemany("INSERT INTO obj_org VALUES (?, ?)", [
        ("org_hy", "宏业建设集团"), ("org_dup", "重名"),
    ])
    c.execute("CREATE TABLE obj_bid_project (project_id VARCHAR, title VARCHAR)")
    c.execute("INSERT INTO obj_bid_project VALUES ('project_p1', '市政道路工程')")
    c.execute("""CREATE TABLE lnk_transfers (
        txn_id VARCHAR, from_account_id VARCHAR, to_account_id VARCHAR,
        amount DOUBLE, date VARCHAR)""")
    c.executemany("INSERT INTO lnk_transfers VALUES (?,?,?,?,?)", [
        ("txn_001", "account_hy", "account_ajc", 4600000, "2021-10-01"),
        ("txn_002", "account_ajc", "account_zwp", 1700000, "2021-11-15"),
    ])
    c.execute("CREATE TABLE lnk_calls_to "
              "(call_id VARCHAR, from_person VARCHAR, to_person VARCHAR)")
    c.executemany("INSERT INTO lnk_calls_to VALUES (?,?,?)", [
        ("call_001", "person_zhang", "person_wang"),
        ("call_002", "person_li", "person_wang"),
    ])
    c.execute("CREATE TABLE lnk_owns "
              "(account_id VARCHAR, owner_raw VARCHAR, owner_person VARCHAR)")
    c.execute("INSERT INTO lnk_owns VALUES "
              "('account_hy', '张三', 'person_zhang')")
    c.execute("CREATE TABLE lnk_involved_in "
              "(org_id VARCHAR, project_id VARCHAR, winner_raw VARCHAR)")
    c.executemany("INSERT INTO lnk_involved_in VALUES (?,?,?)", [
        ("org_hy", "project_p1", "宏业建设集团"),
        ("org_dup", "project_p1", "重名"),
    ])
    c.execute("""CREATE TABLE lnk_co_located (
        track_id_1 VARCHAR, track_id_2 VARCHAR, person_1 VARCHAR,
        person_2 VARCHAR, location VARCHAR, date VARCHAR)""")
    c.execute("INSERT INTO lnk_co_located VALUES "
              "('track_001', 'track_002', 'person_li', 'person_sun', "
              "'江滨路', '2021-10-02')")
    return s


class RelationFunctionTests(unittest.TestCase):

    def setUp(self):
        self.store = make_graph_store()
        self.exec = FunctionExecutor(self.store)

    def tearDown(self):
        self.store.close()

    def _invoke(self, name, params):
        return self.exec.invoke(name, params)["result"]

    # ---- ① 圈层 BFS ----

    def test_neighborhood_cross_type_hops(self):
        r = self._invoke("relation_neighborhood",
                         {"target": "宏业建设", "depth": 2})
        self.assertTrue(r["hit"])
        self.assertEqual(r["subject"]["type"], "account")
        hops = {n["pk"]: n["hops"] for n in r["nodes"]}
        # 一跳：转账对手 + 账户持有人
        self.assertEqual(hops["account_ajc"], 1)
        self.assertEqual(hops["person_zhang"], 1)
        # 二跳：A建材下游 + 张三的通话对端
        self.assertEqual(hops["account_zwp"], 2)
        self.assertEqual(hops["person_wang"], 2)
        self.assertNotIn("person_li", hops)  # 李四与圈层无连边

    def test_neighborhood_tree_edges_traceable(self):
        r = self._invoke("relation_neighborhood",
                         {"target": "宏业建设", "depth": 2})
        sigs = {(e["edge"], e["edge_pk"]) for e in r["edges"]}
        self.assertIn(("transfers", "txn_001"), sigs)
        self.assertIn(("owns", "account_hy"), sigs)
        for e in r["edges"]:
            self.assertIn("key_column", e["ref"])

    # ---- ② 共同邻居 ----

    def test_common_neighbors_via_calls(self):
        r = self._invoke("relation_common_neighbors",
                         {"subject_a": "张三", "subject_b": "李四"})
        self.assertTrue(r["hit"])
        self.assertEqual(r["count"], 1)
        item = r["common"][0]
        self.assertEqual(item["pk"], "person_wang")
        self.assertEqual(item["via_a"]["edge"], "calls_to")
        self.assertEqual(item["via_b"]["edge"], "calls_to")

    def test_common_neighbors_none(self):
        r = self._invoke("relation_common_neighbors",
                         {"subject_a": "张三", "subject_b": "王五"})
        self.assertFalse(r["hit"])
        self.assertEqual(r["count"], 0)

    def test_common_neighbors_same_subject_rejected(self):
        with self.assertRaises(ValueError):
            self._invoke("relation_common_neighbors",
                         {"subject_a": "张三", "subject_b": "张三"})

    # ---- ③ 路径枚举 ----

    def test_paths_fund_two_hop(self):
        r = self._invoke("relation_paths",
                         {"subject_a": "宏业建设", "subject_b": "张卫国配偶",
                          "depth": 2})
        self.assertTrue(r["hit"])
        self.assertEqual(r["count"], 1)
        path = r["paths"][0]
        self.assertEqual(path["length"], 2)
        names = [n["name"] for n in path["nodes"]]
        self.assertEqual(names, ["宏业建设", "A建材", "张卫国配偶"])
        self.assertEqual(path["edges"][0]["ref"]["key_column"], "txn_id")

    def test_paths_cross_type_three_hop(self):
        r = self._invoke("relation_paths",
                         {"subject_a": "张三", "subject_b": "张卫国配偶",
                          "depth": 3})
        self.assertTrue(r["hit"])
        lengths = {p["length"] for p in r["paths"]}
        self.assertIn(3, lengths)  # 张三—持有—宏业建设—转账—A建材—转账—配偶

    def test_paths_depth_too_shallow(self):
        r = self._invoke("relation_paths",
                         {"subject_a": "宏业建设", "subject_b": "张卫国配偶",
                          "depth": 1})
        self.assertFalse(r["hit"])

    def test_paths_prune_cycles(self):
        """环剪枝：路径节点不重复（简单路径）。"""
        r = self._invoke("relation_paths",
                         {"subject_a": "宏业建设", "subject_b": "张卫国配偶",
                          "depth": 3})
        for p in r["paths"]:
            pks = [n["pk"] for n in p["nodes"]]
            self.assertEqual(len(pks), len(set(pks)))

    def test_paths_same_subject_rejected(self):
        with self.assertRaises(ValueError):
            self._invoke("relation_paths",
                         {"subject_a": "张三", "subject_b": "张三"})

    # ---- ④ 主体不存在 ----

    def test_missing_subject_degrades(self):
        r = self._invoke("relation_neighborhood", {"target": "查无此人"})
        self.assertFalse(r["hit"])
        self.assertTrue(r["degraded"])
        self.assertIn("查无此人", r["degraded_reason"])

    def test_paths_missing_subject(self):
        r = self._invoke("relation_paths",
                         {"subject_a": "张三", "subject_b": "查无此人"})
        self.assertFalse(r["hit"])
        self.assertIsNone(r["subject_b"])

    # ---- ⑤ 结构降级（缺边表）----

    def test_missing_edge_table_gap_not_crash(self):
        self.store.conn.execute("DROP TABLE lnk_co_located")
        r = self._invoke("relation_neighborhood",
                         {"target": "张三", "depth": 2})
        self.assertTrue(r["hit"])  # 其余边照常
        gap_links = {g["link"] for g in r["diagnostics"]["gaps"]}
        self.assertEqual(gap_links, {"co_located"})
        self.assertTrue(r["degraded"])

    def test_all_edges_missing_still_no_crash(self):
        for t in ("lnk_transfers", "lnk_calls_to", "lnk_owns",
                  "lnk_involved_in", "lnk_co_located"):
            self.store.conn.execute(f"DROP TABLE {t}")
        r = self._invoke("relation_neighborhood", {"target": "张三"})
        self.assertFalse(r["hit"])
        self.assertEqual(len(r["diagnostics"]["gaps"]), 5)

    # ---- ⑥ 歧义 / 白名单 / 参数校验 ----

    def test_ambiguous_name_requires_disambiguation(self):
        with self.assertRaises(ValueError):
            self._invoke("relation_neighborhood", {"target": "重名"})

    def test_target_type_disambiguates(self):
        r = self._invoke("relation_neighborhood",
                         {"target": "重名", "target_type": "org"})
        self.assertTrue(r["hit"])
        self.assertEqual(r["subject"]["type"], "org")

    def test_subject_whitelist_rejects_injection(self):
        with self.assertRaises(ValueError):
            self._invoke("relation_neighborhood",
                         {"target": "x'; DROP TABLE obj_person; --"})

    def test_param_range_validation(self):
        with self.assertRaises(ValueError):
            self._invoke("relation_neighborhood",
                         {"target": "张三", "depth": 4})
        with self.assertRaises(ValueError):
            self._invoke("relation_neighborhood",
                         {"target": "张三", "edge_kinds": "magic"})
        with self.assertRaises(ValueError):
            self._invoke("relation_paths",
                         {"subject_a": "张三", "subject_b": "李四",
                          "max_paths": 0})

    # ---- ⑦ edge_kinds 收窄 ----

    def test_edge_kinds_fund_filter(self):
        r = self._invoke("relation_neighborhood",
                         {"target": "宏业建设", "depth": 2, "edge_kinds": "fund"})
        hops = {n["pk"] for n in r["nodes"]}
        self.assertIn("account_ajc", hops)
        self.assertIn("account_zwp", hops)
        self.assertNotIn("person_zhang", hops)  # owns 属 org 类被收窄
        self.assertEqual(r["diagnostics"]["loaded_links"], ["transfers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
