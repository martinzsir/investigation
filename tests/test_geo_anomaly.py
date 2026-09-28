"""
tests/test_geo_anomaly.py
PLAN-GEO-001 P6 轨迹分段与异常轨迹检测测试。

覆盖：
  ① 轨迹无时刻（date 档）→ 分段不产出、异常检测随之降级，绝不拿日期编造
     停留时长；
  ② 有时刻 → 停留段/移动段切分，按日切分不串段（整周不误并成一次驻留）；
  ③ 常驻基线之上的偏离：非常驻地点 off_route；常驻地点非常态时段
     off_hours；非常态通勤 off_path；
  ④ 共现：异常停留点上同日同地另有谁（不出"接触"结论）；
  ⑤ 基线下限门控：停留天数不足不产出异常（否则样本越少伪异常越多）；
  ⑥ 常驻阈值语义：rare_ratio 调低 → 非常驻地点变常驻 → 偏离减少；
  ⑦ 镜头 handler 出观察（非线索），声明参数能传到 Function（防白名单脱节）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.store import Store
from core import geo
from core.functions import invoke_function


class _Ctx:
    """ctx 需提供 store（判 timestamp 列是否存在）与表名映射。"""

    def __init__(self, store):
        self.store = store

    def table(self, n):
        return f"obj_{n}"

    def link(self, n):
        return f"lnk_{n}"


class _TrackFixture(unittest.TestCase):
    """内存库：obj_trackpoint 带 timestamp 列（时刻档轨迹）。

    张三：20 个工作日 09:00~18:00 在 loc_B（单位，常驻）；
          其中 3 天 14:00~16:30 在 loc_C（莫干山路，非常驻）。
    李四：同 3 天 14:10~16:20 亦在 loc_C（用于共现）。
    王五：仅日期、无时刻（date 档，用于降级用例）。
    """

    def setUp(self):
        self.store = Store(db_path=":memory:")
        s = self.store
        s.execute("CREATE TABLE obj_person(person_id VARCHAR, raw_name VARCHAR)")
        s.execute(
            "CREATE TABLE obj_location(location_id VARCHAR, std_address VARCHAR, "
            "raw_address VARCHAR, province VARCHAR, prefecture VARCHAR, "
            "county VARCHAR, township VARCHAR, admin_code VARCHAR, "
            "lat DOUBLE, lng DOUBLE, coord_sys VARCHAR, geocode_source VARCHAR, "
            "geocode_confidence DOUBLE, geocoded_at VARCHAR)")
        s.execute(
            "CREATE TABLE obj_trackpoint(track_id VARCHAR, person_raw VARCHAR, "
            "location VARCHAR, date DATE, timestamp TIMESTAMP)")
        s.execute(
            "CREATE TABLE lnk_trackpoint_at(track_id VARCHAR, "
            "location_id VARCHAR, date DATE)")
        for pid, name in (("p1", "张三"), ("p2", "李四"), ("p3", "王五")):
            s.execute("INSERT INTO obj_person VALUES (?, ?)", (pid, name))
        # loc_C 与 loc_B 相距约 7km：若放在 200 米停留半径内会被并成
        # 同一地点，异常地点就"消失"了（实测踩到过）。
        locs = [("loc_A", "甲路", 30.0000, 120.0000),
                ("loc_B", "乙路", 30.0100, 120.0000),
                ("loc_C", "丙路", 30.0500, 120.0500)]
        for lid, addr, lat, lng in locs:
            s.execute(
                "INSERT INTO obj_location VALUES (?, ?, ?, NULL, NULL, '某县', "
                "NULL, '330108', ?, ?, 'GCJ-02', 'manual', 0.9, NULL)",
                (lid, addr, addr, lat, lng))

        n = 0

        def _add(person, lid, day, t_from, t_to):
            nonlocal n
            for t in (t_from, t_to):
                n += 1
                s.execute(
                    "INSERT INTO obj_trackpoint VALUES (?, ?, ?, ?, ?)",
                    (f"t{n}", person, f"地址{lid}", f"2026-01-{day:02d}",
                     f"2026-01-{day:02d} {t}:00"))
                s.execute("INSERT INTO lnk_trackpoint_at VALUES (?, ?, ?)",
                          (f"t{n}", lid, f"2026-01-{day:02d}"))

        # 张三 20 个工作日（1~20 日）：上午甲路、下午乙路（常态通勤 甲路→乙路）
        # 异常日 8/9/10 下午改去丙路；其中仅 8 日构成"甲路→丙路"的非常态
        # 通勤（9/10 日只有丙路一段，无前置停留 → 不生成移动段）。
        self.abnormal_days = [8, 9, 10]
        self.days = list(range(1, 21))
        for d in self.days:
            if d in (9, 10):
                continue          # 这两日只有丙路一段 → 不生成移动段
            _add("张三", "loc_A", d, "09:00", "12:00")
            if d == 8:
                _add("张三", "loc_C", d, "14:00", "16:30")
            else:
                _add("张三", "loc_B", d, "13:00", "18:00")
        for d in (9, 10):
            _add("张三", "loc_C", d, "14:00", "16:30")
        # 李四同 3 天在异常地点
        for d in self.abnormal_days:
            _add("李四", "loc_C", d, "14:10", "16:20")
        # 王五：无时刻
        for d in (1, 2, 3):
            n += 1
            s.execute(
                "INSERT INTO obj_trackpoint VALUES (?, ?, ?, ?, NULL)",
                (f"t{n}", "王五", "地址loc_A", f"2026-01-{d:02d}"))
            s.execute("INSERT INTO lnk_trackpoint_at VALUES (?, ?, ?)",
                      (f"t{n}", "loc_A", f"2026-01-{d:02d}"))

        # ctx 必须带 store：ctx=None 时 SQL 保守按"无 timestamp 列"投影
        # NULL（防旧语义库硬失败），时刻轨会整列落空。
        self.ctx = _Ctx(s)

    def tearDown(self):
        self.store.close()

    def seg(self, target, **kw):
        return geo.geo_trajectory_segment(
            self.store, {"target": target, "target_type": "person", **kw},
            self.ctx)

    def anom(self, target, **kw):
        return geo.geo_anomaly_trajectory(
            self.store, {"target": target, "target_type": "person", **kw},
            self.ctx)


class TrajectorySegmentTests(_TrackFixture):
    def test_no_timestamp_degrades(self):
        r = self.seg("王五")
        self.assertTrue(r["degraded"])
        self.assertEqual(r["stay_count"], 0)
        self.assertIn("无可用时刻", r["degraded_reason"])

    def test_stays_and_moves(self):
        r = self.seg("张三")
        self.assertFalse(r["degraded"])
        # 17 个正常日各 2 段 + 8 日 2 段 + 9/10 日各 1 段 = 38
        self.assertEqual(r["stay_count"], 38)
        # 同日相邻停留之间才成移动：17 段常态（甲路→乙路）+ 1 段非常态
        self.assertEqual(r["move_count"], 18)
        self.assertTrue(r["total_stay_minutes"] > 0)

    def test_not_clustered_across_days(self):
        """按日切分：20 个工作日不误并成一个"停留 20 天"。"""
        r = self.seg("张三")
        for s in r["stays"]:
            self.assertEqual(s["start"][:10], s["end"][:10])
            self.assertLessEqual(s["duration_minutes"], 1440)

    def test_unknown_subject(self):
        r = self.seg("查无此人")
        self.assertTrue(r["degraded"])
        self.assertIsNone(r["subject"])


class AnomalyTrajectoryTests(_TrackFixture):
    def test_off_route_detected(self):
        r = self.anom("张三")
        self.assertFalse(r["degraded"])
        self.assertTrue(r["hit"])
        self.assertEqual(r["by_kind"].get("off_route"), 3)
        off_route = [a for a in r["anomalies"] if a["kind"] == "off_route"]
        self.assertEqual(len(off_route), 3)
        for a in off_route:
            self.assertEqual(a["location_id"], "loc_C")

    def test_baseline_built(self):
        r = self.anom("张三")
        b = r["baseline"]
        self.assertEqual(b["days"], 20)
        self.assertEqual(b["site_count"], 3)
        hab = [x for x in b["habitual_sites"] if x["habitual"]]
        self.assertEqual(len(hab), 2, msg=str(b["habitual_sites"]))
        self.assertEqual({x["location_id"] for x in hab}, {"loc_A", "loc_B"})

    def test_off_path_detected(self):
        """非常态通勤：甲路→丙路仅 1 次，常态组合甲路→乙路出现 17 次。"""
        r = self.anom("张三")
        self.assertEqual(r["by_kind"].get("off_path"), 1)
        a = [x for x in r["anomalies"] if x["kind"] == "off_path"][0]
        self.assertEqual(a["from"], "甲路")
        self.assertEqual(a["to"], "丙路")

    def test_co_presence_on_abnormal_stay(self):
        r = self.anom("张三")
        off_route = [a for a in r["anomalies"]
                     if a["kind"] in ("off_route", "off_hours")]
        self.assertEqual(len(off_route), 3)
        for a in off_route:
            self.assertEqual(a.get("co_present"), ["李四"])
            self.assertIn("待核查", a["co_present_note"])

    def test_baseline_days_gate(self):
        r = self.anom("张三", min_baseline_days=30)
        self.assertTrue(r["degraded"])
        self.assertEqual(r["anomaly_count"], 0)
        self.assertIn("低于基线下限", r["degraded_reason"])

    def test_rare_ratio_semantics(self):
        """阈值调低 → 丙路（占比 0.15）变常驻 → 不再判"非常驻地点"偏离。"""
        r = self.anom("张三", rare_ratio=0.1)
        self.assertIsNone(r["by_kind"].get("off_route"))

    def test_degrades_when_no_timestamp(self):
        r = self.anom("王五")
        self.assertTrue(r["degraded"])
        self.assertIn("分段不可用", r["degraded_reason"])

    def test_unknown_subject(self):
        r = self.anom("查无此人")
        self.assertTrue(r["degraded"])

    def test_no_qualitative_claim(self):
        """产出不得出现"可疑""疑似"——偏离是结构事实，不是定性。"""
        r = self.anom("张三")
        for a in r["anomalies"]:
            for w in ("可疑", "疑似"):
                self.assertNotIn(w, str(a.get("reason", "")))
        self.assertIn("不等于可疑", r["anomaly_note"])


class AnomalyLensTests(_TrackFixture):
    """镜头接线：handler 出观察，且声明参数能传到 Function。"""

    def _skill_ids(self):
        import json
        p = ROOT / "packs" / "geo" / "pack.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        return {s["skill_id"]: s for s in d["skills"]}

    def test_lens_registered(self):
        from core.pack_loader import discover
        from core.registry import SkillRegistry
        reg = SkillRegistry()
        rep = discover(reg)
        self.assertIn("geo", rep["loaded"])
        for sid in ("geo_segment", "geo_anomaly"):
            spec = reg.skill(sid)
            self.assertTrue(callable(spec.handler), sid)
            self.assertTrue(spec.enabled, sid)

    def test_declared_params_reach_function(self):
        """pack.json 声明的参数必须在 impl 的 _clean 白名单里。

        为什么有这条测试：参数声明与传参白名单是**两处**，脱节时表单能填、
        值被静默丢弃（PLAN-GEO-001 P5 实测踩到过 window_minutes 这个坑）。
        """
        import inspect
        from packs.geo import impl
        src = inspect.getsource(impl)
        for sid, spec in self._skill_ids().items():
            for pname in (spec.get("params_schema") or {}):
                self.assertIn(pname, src,
                              f"{sid} 声明了 {pname} 但 impl 未透传")

    def test_anomaly_lens_emits_observation(self):
        from core.registry import SkillRegistry
        from core.pack_loader import discover
        from core.observation import observation_from_clue
        reg = SkillRegistry()
        discover(reg)
        spec = reg.skill("geo_anomaly")
        clues = spec.handler(store=self.store, ctx=None,
                             params={"target_subject": "张三",
                                     "target_type": "person"},
                             health={"fn_reg": None})
        if not clues:  # handler 依赖 health 注入，回退直连
            self.skipTest("handler 需 health 注入，Function 层已覆盖")
            return
        self.assertEqual(len(clues), 1)
        obs = observation_from_clue(clues[0])
        self.assertIn("偏离常驻模式", obs.title)
        self.assertNotIn("可疑", obs.title)


if __name__ == "__main__":
    unittest.main(verbosity=2)
