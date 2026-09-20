"""
tests/test_timeline_functions.py
P5 时间研判 Function 测试（timeline_event_sequence / timeline_rhythm /
timeline_cross_collision，投标/资金/通话/轨迹统一时间轴，
研判能力插件化 v3 §4-P5）。

覆盖：
  ① 跨类型事件序列邻接（排序、gap_days、跨度、类型计数）；
  ② 周期节奏（间隔中位数、burst_days 聚集簇、事件不足降级）；
  ③ 跨类型时间窗碰撞（每主体类型数、min_event_types、窗口收窄）；
  ④ 项目/主体不存在 → hit=False 降级不崩；
  ⑤ 缺事件表结构降级（gaps 记录，其余事件照常）；
  ⑥ 参数范围校验；
  ⑦ 事件缺主键/缺日期被跳过（不悬空）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store
from core.functions import FunctionExecutor


def make_timeline_store() -> Store:
    """内存库 + 手造语义表（列名与真实 build_sql 输出一致）。

    锚点：市政道路工程 project_p1，公示日 2021-10-15。
      资金：开标前宏业建设分别向张三/李四（10-13）、王五（10-14）转 9.8 万；
            9-01 张三→赵六 5 万（窗口外）
      通话：10-12 张三→李四；10-15 张三→赵六；7-01 赵六→钱七
      轨迹：10-13 张三/李四 在开标地酒店；10-14 王五 在开标地酒店；
            8-01 赵六 在外地
    """
    s = Store(db_path=":memory:")
    c = s.conn
    c.execute("CREATE TABLE obj_person (person_id VARCHAR, raw_name VARCHAR)")
    c.executemany("INSERT INTO obj_person VALUES (?, ?)", [
        ("person_zhang", "张三"), ("person_li", "李四"),
        ("person_wang", "王五"), ("person_zhao", "赵六"),
        ("person_qian", "钱七"),
    ])
    c.execute("CREATE TABLE obj_bid_project "
              "(project_id VARCHAR, title VARCHAR, pub_date VARCHAR)")
    c.execute("INSERT INTO obj_bid_project VALUES "
              "('project_p1', '市政道路工程', '2021-10-15')")
    c.execute("""CREATE TABLE obj_transaction (
        txn_id VARCHAR, from_raw VARCHAR, to_raw VARCHAR,
        amount DOUBLE, date VARCHAR)""")
    c.executemany("INSERT INTO obj_transaction VALUES (?,?,?,?,?)", [
        ("txn_001", "宏业建设", "张三", 98000, "2021-10-13"),
        ("txn_002", "宏业建设", "李四", 98000, "2021-10-13"),
        ("txn_003", "宏业建设", "王五", 98000, "2021-10-14"),
        ("txn_004", "张三", "赵六", 50000, "2021-09-01"),
    ])
    c.execute("""CREATE TABLE obj_call (
        call_id VARCHAR, caller_raw VARCHAR, callee_raw VARCHAR,
        date VARCHAR)""")
    c.executemany("INSERT INTO obj_call VALUES (?,?,?,?)", [
        ("call_001", "张三", "李四", "2021-10-12"),
        ("call_002", "张三", "赵六", "2021-10-15"),
        ("call_003", "赵六", "钱七", "2021-07-01"),
    ])
    c.execute("""CREATE TABLE obj_trackpoint (
        track_id VARCHAR, person_raw VARCHAR, location VARCHAR,
        date VARCHAR)""")
    c.executemany("INSERT INTO obj_trackpoint VALUES (?,?,?,?)", [
        ("track_001", "张三", "开标地酒店", "2021-10-13"),
        ("track_002", "李四", "开标地酒店", "2021-10-13"),
        ("track_003", "王五", "开标地酒店", "2021-10-14"),
        ("track_004", "赵六", "外地", "2021-08-01"),
    ])
    c.execute("""CREATE TABLE lnk_time_window (
        project_id VARCHAR, txn_id VARCHAR, title VARCHAR,
        owner_raw VARCHAR, amount DOUBLE, offset_days INTEGER)""")
    c.executemany("INSERT INTO lnk_time_window VALUES (?,?,?,?,?,?)", [
        ("project_p1", "txn_001", "市政道路工程", "张三", 98000, -2),
        ("project_p1", "txn_002", "市政道路工程", "李四", 98000, -2),
        ("project_p1", "txn_003", "市政道路工程", "王五", 98000, -1),
    ])
    return s


class TimelineFunctionTests(unittest.TestCase):

    def setUp(self):
        self.store = make_timeline_store()
        self.exec = FunctionExecutor(self.store)

    def tearDown(self):
        self.store.close()

    def _invoke(self, name, params):
        return self.exec.invoke(name, params)["result"]

    # ---- ① 事件序列邻接 ----

    def test_sequence_three_types_ordered(self):
        r = self._invoke("timeline_event_sequence", {"target": "张三"})
        self.assertTrue(r["hit"])
        self.assertEqual(r["event_count"], 5)
        self.assertEqual(r["span_days"], 44)
        self.assertEqual(r["type_counts"],
                         {"transaction": 2, "call": 2, "trackpoint": 1})
        dates = [e["date"] for e in r["timeline"]]
        self.assertEqual(dates, sorted(dates))
        # 首事件无 gap；9-01 → 10-12 间隔 41 天
        self.assertIsNone(r["timeline"][0]["gap_days"])
        self.assertEqual(r["timeline"][1]["gap_days"], 41)

    def test_sequence_roles_and_brief(self):
        r = self._invoke("timeline_event_sequence", {"target": "张三"})
        by_pk = {e["event_pk"]: e for e in r["timeline"]}
        self.assertEqual(by_pk["txn_004"]["role"], "转出")
        self.assertEqual(by_pk["txn_001"]["role"], "转入")
        self.assertEqual(by_pk["call_001"]["role"], "主叫")
        self.assertTrue(all(e["brief"] for e in r["timeline"]))

    def test_sequence_single_event_not_hit(self):
        r = self._invoke("timeline_event_sequence", {"target": "钱七"})
        self.assertFalse(r["hit"])
        self.assertEqual(r["event_count"], 1)

    # ---- ② 周期节奏 ----

    def test_rhythm_burst_cluster(self):
        r = self._invoke("timeline_rhythm",
                         {"target": "张三", "burst_days": 3})
        self.assertTrue(r["hit"])
        self.assertEqual(r["burst_count"], 1)
        burst = r["bursts"][0]
        self.assertEqual(burst["start"], "2021-10-12")
        self.assertEqual(burst["end"], "2021-10-15")
        self.assertEqual(burst["event_count"], 4)
        self.assertEqual(set(burst["types"]),
                         {"transaction", "call", "trackpoint"})
        self.assertEqual(r["median_gap_days"], 1.5)

    def test_rhythm_no_burst_when_spread_out(self):
        # 赵六事件 7-01/8-01/9-01 间隔均 31 天：burst_days=14 无 ≥2 起连续簇
        r = self._invoke("timeline_rhythm",
                         {"target": "赵六", "burst_days": 14})
        self.assertFalse(r["hit"])
        self.assertEqual(r["bursts"], [])

    def test_rhythm_few_events_degraded(self):
        r = self._invoke("timeline_rhythm", {"target": "钱七"})
        self.assertFalse(r["hit"])
        self.assertTrue(r["degraded"])
        self.assertIn("事件不足", r["degraded_reason"])

    def test_rhythm_burst_days_validation(self):
        for bad in (0, 15):
            with self.assertRaises(ValueError):
                self._invoke("timeline_rhythm",
                             {"target": "张三", "burst_days": bad})

    # ---- ③ 跨类型时间窗碰撞 ----

    def test_cross_collision_three_subjects(self):
        r = self._invoke("timeline_cross_collision",
                         {"project": "市政道路工程", "window_days": 7})
        self.assertTrue(r["hit"])
        self.assertEqual(r["count"], 3)
        subjects = {row["subject_raw"]: row for row in r["rows"]}
        self.assertEqual(set(subjects), {"张三", "李四", "王五"})
        self.assertEqual(subjects["张三"]["type_count"], 3)
        self.assertEqual(set(subjects["王五"]["event_types"]),
                         {"transaction", "trackpoint"})
        # 单类型主体不进碰撞
        all_names = subjects
        self.assertNotIn("赵六", all_names)
        self.assertNotIn("宏业建设", all_names)

    def test_cross_collision_min_event_types_three(self):
        r = self._invoke("timeline_cross_collision",
                         {"project": "市政道路工程", "min_event_types": 3})
        self.assertEqual(r["count"], 2)
        self.assertEqual({row["subject_raw"] for row in r["rows"]},
                         {"张三", "李四"})

    def test_cross_collision_narrow_window(self):
        r = self._invoke("timeline_cross_collision",
                         {"project": "市政道路工程", "window_days": 1})
        # 10-14..10-16：王五（资金 10-14 + 轨迹 10-14）
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["rows"][0]["subject_raw"], "王五")

    def test_cross_collision_window_excludes_far_events(self):
        r = self._invoke("timeline_cross_collision",
                         {"project": "市政道路工程", "window_days": 3})
        # call_002 在 10-15（窗内），赵六仅通话单一类型仍不碰撞
        subjects = {row["subject_raw"] for row in r["rows"]}
        self.assertNotIn("赵六", subjects)

    # ---- ④ 不存在 ----

    def test_sequence_missing_subject_degrades(self):
        r = self._invoke("timeline_event_sequence", {"target": "查无此人"})
        self.assertFalse(r["hit"])
        self.assertTrue(r["degraded"])

    def test_cross_collision_missing_project(self):
        r = self._invoke("timeline_cross_collision",
                         {"project": "查无此项目"})
        self.assertFalse(r["hit"])
        self.assertTrue(r["degraded"])

    def test_cross_collision_scans_all_projects_without_param(self):
        """规则引擎路径（R7）：不给 project → 扫全部带公示日项目，逐锚点打标。"""
        self.store.conn.execute(
            "INSERT INTO obj_bid_project VALUES "
            "('project_p2', '远郊绿化工程', '2022-03-01')")
        r = self._invoke("timeline_cross_collision", {})
        self.assertTrue(r["hit"])
        self.assertIsNone(r["project"])
        self.assertIsNone(r["anchor_date"])
        self.assertEqual(r["scanned_projects"], 2)
        # 碰撞全部归属 project_p1；p2 窗口内无事件不出行；每行带锚点标签
        self.assertEqual({row["project"]["pk"] for row in r["rows"]},
                         {"project_p1"})
        self.assertTrue(all(row["anchor_date"] == "2021-10-15"
                            for row in r["rows"]))

    def test_cross_collision_multi_anchor_rows_tagged_per_project(self):
        """第二项目窗口内也有跨类型事件 → 两个锚点的碰撞行各自打标。"""
        c = self.store.conn
        c.execute("INSERT INTO obj_bid_project VALUES "
                  "('project_p2', '远郊绿化工程', '2022-03-01')")
        c.execute("INSERT INTO obj_transaction VALUES "
                  "('txn_101', '宏业建设', '钱七', 66000, '2022-03-02')")
        c.execute("INSERT INTO obj_trackpoint VALUES "
                  "('track_101', '钱七', '远郊工地', '2022-03-02')")
        r = self._invoke("timeline_cross_collision", {})
        by_proj = {}
        for row in r["rows"]:
            by_proj.setdefault(row["project"]["pk"], []).append(
                row["subject_raw"])
        self.assertIn("project_p2", by_proj)
        self.assertIn("钱七", by_proj["project_p2"])

    # ---- ⑤ 结构降级 ----

    def test_sequence_missing_transaction_table_gap(self):
        self.store.conn.execute("DROP TABLE obj_transaction")
        r = self._invoke("timeline_event_sequence", {"target": "张三"})
        self.assertTrue(r["hit"])  # 通话+轨迹照常
        self.assertEqual(r["event_count"], 3)
        gap_objs = {g["object"] for g in r["diagnostics"]["gaps"]}
        self.assertEqual(gap_objs, {"transaction"})
        self.assertTrue(r["degraded"])

    def test_cross_collision_missing_transaction(self):
        self.store.conn.execute("DROP TABLE obj_transaction")
        r = self._invoke("timeline_cross_collision",
                         {"project": "市政道路工程"})
        # 张三/李四：通话+轨迹；王五：仅轨迹
        self.assertEqual(r["count"], 2)
        self.assertTrue(r["degraded"])

    def test_cross_collision_all_event_tables_missing(self):
        for t in ("obj_transaction", "obj_call", "obj_trackpoint"):
            self.store.conn.execute(f"DROP TABLE {t}")
        r = self._invoke("timeline_cross_collision",
                         {"project": "市政道路工程"})
        self.assertFalse(r["hit"])
        self.assertEqual(len(r["diagnostics"]["gaps"]), 3)

    # ---- ⑥ 参数校验 ----

    def test_param_range_validation(self):
        for bad in (0, 61):
            with self.assertRaises(ValueError):
                self._invoke("timeline_cross_collision",
                             {"project": "市政道路工程", "window_days": bad})
        for bad in (1, 4):
            with self.assertRaises(ValueError):
                self._invoke("timeline_cross_collision",
                             {"project": "市政道路工程",
                              "min_event_types": bad})

    # ---- ⑦ 脏事件跳过 ----

    def test_events_missing_pk_or_date_skipped(self):
        self.store.conn.execute(
            "INSERT INTO obj_call VALUES ('call_bad1', '张三', '李四', NULL),"
            "(NULL, '张三', '李四', '2021-10-13')")
        r = self._invoke("timeline_event_sequence", {"target": "张三"})
        pks = {e["event_pk"] for e in r["timeline"]}
        self.assertNotIn("call_bad1", pks)
        self.assertEqual(r["event_count"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
