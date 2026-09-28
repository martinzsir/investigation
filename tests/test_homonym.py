"""同名异人消歧测试。

守护两条红线：
  1. 不把正常通勤误判成两人（误判比漏判更糟——会把单人拆成两人）
  2. 证据不足时不静默决定（既不静默分列，也不静默合并后假装无风险）
"""

import datetime as _dt
import json
import unittest

import duckdb

from core.homonym import (
    detect_homonyms,
    strip_relational,
    to_review_candidates,
)

# 杭州 ≈ (30.27, 120.15)，上海 ≈ (31.23, 121.47)，直线约 170 km
HZ = (30.27, 120.15)
SH = (31.23, 121.47)


def _mk_conn():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE obj_person_identity ("
        " pk VARCHAR, raw_name VARCHAR, id_card VARCHAR)")
    conn.execute(
        "CREATE TABLE obj_trackpoint ("
        " pk VARCHAR, person_raw VARCHAR, location VARCHAR,"
        " date DATE, timestamp TIMESTAMP)")
    conn.execute(
        "CREATE TABLE obj_location ("
        " pk VARCHAR, std_address VARCHAR, lat DOUBLE, lng DOUBLE)")
    conn.execute(
        "CREATE TABLE obj_account ("
        " pk VARCHAR, raw_name VARCHAR, account_id VARCHAR, source_rows VARCHAR)")
    conn.execute(
        "CREATE TABLE obj_person (pk VARCHAR, raw_name VARCHAR)")
    return conn


def _add_loc(conn, pk, addr, lat, lng):
    conn.execute(
        "INSERT INTO obj_location VALUES (?,?,?,?)", [pk, addr, lat, lng])


def _add_tp(conn, pk, person, loc, date, ts=None):
    conn.execute(
        "INSERT INTO obj_trackpoint VALUES (?,?,?,?,?)",
        [pk, person, loc, date, ts])


class StripRelationalTests(unittest.TestCase):
    def test_plain_name(self):
        core, is_rel = strip_relational("张卫国")
        self.assertEqual(core, "张卫国")
        self.assertFalse(is_rel)

    def test_relational_reference(self):
        core, is_rel = strip_relational("张卫国配偶")
        self.assertTrue(is_rel)
        self.assertEqual(core, "张卫国")


class IdCardEvidenceTests(unittest.TestCase):

    def test_two_id_cards_means_distinct(self):
        """同名不同证号 = 铁证 → distinct。"""
        conn = _mk_conn()
        conn.execute("INSERT INTO obj_person_identity VALUES "
                     "('a','张卫国','330100...12345')")
        conn.execute("INSERT INTO obj_person_identity VALUES "
                     "('b','张卫国','330100...45678')")
        r = detect_homonyms(conn, names=["张卫国"])
        g = r["groups"][0]
        self.assertEqual(g["verdict"], "distinct")
        self.assertIn("身份证", g["reason"])

    def test_single_id_card_means_same(self):
        conn = _mk_conn()
        conn.execute("INSERT INTO obj_person_identity VALUES "
                     "('a','李志强','330100...99999')")
        r = detect_homonyms(conn, names=["李志强"])
        self.assertEqual(r["groups"][0]["verdict"], "same")

    def test_empty_identity_table_is_unavailable_not_no_conflict(self):
        """身份表为空 → 证据不可用，不能当成「无冲突」。"""
        conn = _mk_conn()
        r = detect_homonyms(conn, names=["张卫国"])
        self.assertEqual(r["summary"]["no_evidence"], 1)
        kinds = [d["kind"] for d in r["diagnostics"]]
        self.assertIn("no_evidence", kinds)

    def test_blank_id_card_ignored(self):
        conn = _mk_conn()
        conn.execute("INSERT INTO obj_person_identity VALUES ('a','张卫国','')")
        conn.execute("INSERT INTO obj_person_identity VALUES ('b','张卫国',NULL)")
        r = detect_homonyms(conn, names=["张卫国"])
        self.assertEqual(r["summary"]["no_evidence"], 1)


class SpatialEvidenceTests(unittest.TestCase):

    def _setup(self, hours_apart, ts=True):
        conn = _mk_conn()
        _add_loc(conn, "l1", "杭州某地", *HZ)
        _add_loc(conn, "l2", "上海某地", *SH)
        d = _dt.date(2020, 3, 10)
        t1 = _dt.datetime(2020, 3, 10, 14, 0)
        t2 = t1 + _dt.timedelta(hours=hours_apart)
        _add_tp(conn, "t1", "李志强", "杭州某地", d, t1 if ts else None)
        _add_tp(conn, "t2", "李志强", "上海某地", d, t2 if ts else None)
        return conn

    def test_physically_impossible_move_is_distinct(self):
        """30 分钟位移 170 km（需 340 km/h）→ distinct。"""
        conn = self._setup(hours_apart=0.5)
        r = detect_homonyms(conn, names=["李志强"])
        g = r["groups"][0]
        self.assertEqual(g["verdict"], "distinct")
        self.assertIn("空间互斥", g["reason"])

    def test_normal_commute_not_misjudged(self):
        """2 小时位移 170 km（85 km/h，可达）→ 不出证据，绝不误拆。"""
        conn = self._setup(hours_apart=2.0)
        r = detect_homonyms(conn, names=["李志强"])
        # 可达速度内 → 不出空间证据（groups 为空即"未拆分"），绝不误拆
        self.assertNotIn("distinct", [g["verdict"] for g in r["groups"]])

    def test_date_only_is_weak_pending(self):
        """无时刻数据只能按日粒度判 → 弱证据 → pending，不直接分列。"""
        conn = self._setup(hours_apart=0.5, ts=False)
        r = detect_homonyms(conn, names=["李志强"])
        g = r["groups"][0]
        self.assertEqual(g["verdict"], "pending")
        ev = next(e for e in g["evidence"] if e["kind"] == "spatial")
        self.assertEqual(ev["strength"], "weak")

    def test_centroid_overlap_yields_no_false_conclusion(self):
        """坐标重合（区划质心）→ 距离为 0，不构成任何证据。"""
        conn = _mk_conn()
        _add_loc(conn, "l1", "西湖区A", *HZ)
        _add_loc(conn, "l2", "西湖区B", *HZ)   # 同质心
        d = _dt.date(2020, 3, 10)
        _add_tp(conn, "t1", "李志强", "西湖区A", d, _dt.datetime(2020, 3, 10, 9, 0))
        _add_tp(conn, "t2", "李志强", "西湖区B", d, _dt.datetime(2020, 3, 10, 9, 30))
        r = detect_homonyms(conn, names=["李志强"])
        self.assertEqual(r["summary"]["no_evidence"], 1)


class NoSilentDecisionTests(unittest.TestCase):

    def test_pending_does_not_write_any_table(self):
        """弱证据只出 review 候选，不改写语义层（前后行数不变）。"""
        conn = _mk_conn()
        conn.execute("INSERT INTO obj_account VALUES "
                     "('a','王建国','acct1','[\"r1\"]')")
        conn.execute("INSERT INTO obj_account VALUES "
                     "('b','王建国','acct2','[\"r2\"]')")
        before = conn.execute("SELECT count(*) FROM obj_account").fetchone()[0]
        r = detect_homonyms(conn, names=["王建国"])
        self.assertEqual(r["groups"][0]["verdict"], "pending")
        after = conn.execute("SELECT count(*) FROM obj_account").fetchone()[0]
        self.assertEqual(before, after, "检测过程不得写表")

    def test_review_candidates_only_for_pending(self):
        conn = _mk_conn()
        conn.execute("INSERT INTO obj_person_identity VALUES "
                     "('a','张卫国','c1')")
        conn.execute("INSERT INTO obj_person_identity VALUES "
                     "('b','张卫国','c2')")
        r = detect_homonyms(conn, names=["张卫国"])
        self.assertEqual(to_review_candidates(r), [], "distinct 不进 review")

    def test_relational_reference_excluded(self):
        """「张卫国配偶」是关系型指代，不该进消歧组。"""
        conn = _mk_conn()
        r = detect_homonyms(conn, names=["张卫国配偶"])
        self.assertEqual(r["groups"], [])
        self.assertEqual(r["summary"]["excluded_relational"], 1)


if __name__ == "__main__":
    unittest.main()
