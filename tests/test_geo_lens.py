"""
tests/test_geo_lens.py
PLAN-GEO-001 P3 空间研判镜头包测试（packs/geo）。

覆盖（计划 §5.5）：
  ① discover 从真实 packs/ 目录挂载 geo 包（零注册代码），声明元数据正确，
     uses_functions 引用的 Function 均已声明（否则注册期硬失败）；
  ② geo_site_profile 落脚点画像 → LineageClue，证据引用 person/location/
     trackpoint 三类真实语义行 + aggregate 聚合量，契约校验无悬空；
  ③ geo_serial_profile 系列案件 CGT → 线索 detail 嵌 GCJ-02 GeoJSON
     FeatureCollection 与优先排查区，事件点引用去重截断；
  ④ min_events 不足降级 → 零线索（缺口不充数）；未知主体 → 零线索；
  ⑤ 未声明参数硬失败；
  ⑥ 定向镜头分流（requires_params）+ 案件级启停兼容（case_batch_lens_ids）；
  ⑦ 目录拔出对账注销；
  ⑧ 观察层并线：observation_from_clue + directed_observations 落盘/回读。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.pack_loader import (
    discover,
    case_batch_lens_ids,
    batch_lens_ids,
)
from core.registry import SkillRegistry, skill_invoke
from core.observation import observation_from_clue
from server.app.clues_artifact import (
    save_directed_observations,
    load_directed_observations,
)

from tests.test_geo import _SpatialFixture


class GeoPackMountTests(unittest.TestCase):
    def setUp(self):
        self.reg = SkillRegistry()
        self.report = discover(self.reg)  # 真实 packs/ 目录

    def test_pack_loaded_without_failure(self):
        self.assertIn("geo", self.report["loaded"], msg=str(self.report))
        geo_failed = [f for f in self.report["failed"]
                      if f["path"].replace("\\", "/").endswith(
                          "packs/geo/pack.json")]
        self.assertEqual(geo_failed, [])

    def test_skill_metadata(self):
        spec = self.reg.skill("geo_site_profile")
        self.assertEqual(spec.pack_id, "geo")
        self.assertEqual(spec.mode, "deterministic")
        self.assertTrue(spec.enabled)
        self.assertIn("target_subject", spec.params_schema)
        self.assertTrue(
            spec.params_schema["target_subject"].get("required"))
        for t in ("trackpoint", "location", "trackpoint_at"):
            self.assertIn(t, spec.scope_reads)
        self.assertTrue(callable(spec.handler))
        spec2 = self.reg.skill("geo_serial_profile")
        for p in ("min_events", "grid_meters", "buffer_m", "top_n"):
            self.assertIn(p, spec2.params_schema)
        self.assertTrue(callable(spec2.handler))

    def test_classified_as_requires_params(self):
        # 两镜头都有必填 target_subject → 定向镜头，不进无参批量
        runnable, requires_params = batch_lens_ids(self.reg)
        self.assertIn("geo_site_profile", requires_params)
        self.assertIn("geo_serial_profile", requires_params)
        self.assertNotIn("geo_site_profile", runnable)

    def test_pack_unregistered_when_dir_gone(self):
        # 对同一注册表从空目录重扫：geo 两镜头随目录拔出对账注销
        empty = Path(tempfile.mkdtemp())
        try:
            rep = discover(self.reg, packs_dir=empty)
            self.assertIn("geo_site_profile", rep["removed"])
            self.assertIn("geo_serial_profile", rep["removed"])
            with self.assertRaises(KeyError):
                self.reg.skill("geo_site_profile")
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class GeoSiteProfileLensTests(_SpatialFixture):
    def setUp(self):
        super().setUp()
        self.reg = SkillRegistry()
        discover(self.reg)

    def test_produces_clue_with_contract_refs(self):
        clues = skill_invoke(self.reg, "geo_site_profile",
                             store=self.store,
                             params={"target_subject": "张三"})
        self.assertEqual(len(clues), 1)
        c = clues[0]
        self.assertEqual(c.skill_id, "geo_site_profile")
        self.assertIn("张三", c.title)
        refs = c.evidence_refs
        key_cols = {r.get("key_column") for r in refs if r["kind"] == "node"}
        # 主体（person）+ 落脚点实体（location）+ 事件点（trackpoint）
        self.assertEqual(
            key_cols, {"person_id", "location_id", "track_id"})
        # 张三 5 个落脚点 A-E、5 次到访
        loc_refs = {r["ref"] for r in refs
                    if r.get("key_column") == "location_id"}
        self.assertEqual(
            loc_refs, {f"obj_location#loc_{x}" for x in "ABCDE"})
        tk_refs = {r["ref"] for r in refs
                   if r.get("key_column") == "track_id"}
        self.assertEqual(tk_refs, {f"obj_trackpoint#t{i}" for i in range(1, 6)})
        metrics = {r["metric"]: r.get("value")
                   for r in refs if r["kind"] == "aggregate"}
        self.assertEqual(metrics["site_count"], 5)
        self.assertEqual(metrics["total_visits"], 5)
        self.assertEqual(metrics["coord_coverage"], "5/5")
        # detail 带完整落脚点结构（P4 地图直接消费）
        self.assertEqual(len(c.detail["sites"]), 5)
        self.assertTrue(c.detail["basis"])
        self.assertTrue(c.detail["falsification"])
        self.assertFalse(c.detail["degraded"])
        # trackpoint → 生间（五间映射反查）
        self.assertIn("生间", c.jian_types)

    def test_unknown_subject_no_clue(self):
        self.assertEqual(skill_invoke(
            self.reg, "geo_site_profile", store=self.store,
            params={"target_subject": "查无此人"}), [])

    def test_undeclared_param_rejected(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "geo_site_profile", store=self.store,
                         params={"target_subject": "张三", "bogus": 1})


class GeoSerialProfileLensTests(_SpatialFixture):
    def setUp(self):
        super().setUp()
        self.reg = SkillRegistry()
        discover(self.reg)

    def test_produces_clue_with_geojson(self):
        clues = skill_invoke(self.reg, "geo_serial_profile",
                             store=self.store,
                             params={"target_subject": "张三",
                                     "min_events": 5})
        self.assertEqual(len(clues), 1)
        c = clues[0]
        self.assertEqual(c.skill_id, "geo_serial_profile")
        # 证据：主体 + 5 个真实事件点 + 聚合量
        tk_refs = {r["ref"] for r in c.evidence_refs
                   if r.get("key_column") == "track_id"}
        self.assertEqual(tk_refs, {f"obj_trackpoint#t{i}" for i in range(1, 6)})
        metrics = {r["metric"]: r.get("value")
                   for r in c.evidence_refs if r["kind"] == "aggregate"}
        self.assertEqual(metrics["events_used"], 5)
        # 优先排查区 + 顶格归一化
        zones = c.detail["priority_zones"]
        self.assertTrue(zones)
        self.assertAlmostEqual(zones[0]["probability"], 1.0)
        self.assertEqual(c.detail["top_zone"]["probability"], 1.0)
        # GeoJSON：FeatureCollection、GCJ-02 坐标序 [lng,lat]（经度 120 系）
        gj = c.detail["geojson"]
        self.assertEqual(gj["type"], "FeatureCollection")
        self.assertTrue(gj["features"])
        ring = gj["features"][0]["geometry"]["coordinates"][0]
        lngs = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        self.assertTrue(all(119.9 < x < 120.2 for x in lngs))
        self.assertTrue(all(29.9 < x < 30.1 for x in lats))
        self.assertTrue(c.detail["basis"])
        self.assertIn("生间", c.jian_types)

    def test_min_events_below_threshold_no_clue(self):
        # 李四仅 2 起坐标事件 < 5 → 函数降级 hit=False，镜头零线索不造数
        clues = skill_invoke(self.reg, "geo_serial_profile",
                             store=self.store,
                             params={"target_subject": "李四",
                                     "min_events": 5})
        self.assertEqual(clues, [])

    def test_unknown_subject_no_clue(self):
        self.assertEqual(skill_invoke(
            self.reg, "geo_serial_profile", store=self.store,
            params={"target_subject": "查无此人", "min_events": 5}), [])

    def test_undeclared_param_rejected(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "geo_serial_profile", store=self.store,
                         params={"target_subject": "张三", "bogus": 1})


class GeoLensCaseSwitchTests(_SpatialFixture):
    def setUp(self):
        super().setUp()
        self.reg = SkillRegistry()
        discover(self.reg)

    def test_case_disable_filters_geo_lenses(self):
        runnable, requires, disabled = case_batch_lens_ids(
            self.reg, {"geo_site_profile": False})
        self.assertIn("geo_site_profile", disabled)
        self.assertNotIn("geo_site_profile", requires)
        # 未停用的另一个不受影响
        self.assertIn("geo_serial_profile", requires)
        self.assertNotIn("geo_serial_profile", disabled)

    def test_observation_layer_roundtrip(self):
        """定向观察并线：线索 → Observation → directed_observations.json
        回读（案件级、跨版本持久）；GeoJSON 随 detail 保留供 P4 成图。"""
        clues = skill_invoke(self.reg, "geo_serial_profile",
                             store=self.store,
                             params={"target_subject": "张三",
                                     "min_events": 5})
        self.assertEqual(len(clues), 1)
        o = observation_from_clue(
            clues[0], source="directed",
            origin={"clue_id": "clue_x1", "surface": "canvas"},
            run_id="lensrun_test01", operator="李侦查员", version=3)
        self.assertEqual(o.skill_id, "geo_serial_profile")
        self.assertEqual(o.subject, "张三")
        self.assertTrue(o.basis)
        self.assertEqual(o.run_id, "lensrun_test01")
        self.assertEqual(o.detail["geojson"]["type"], "FeatureCollection")

        case_dir = Path(tempfile.mkdtemp())
        try:
            save_directed_observations(case_dir, [o])
            loaded = load_directed_observations(case_dir)
            self.assertEqual(len(loaded), 1)
            o2 = loaded[0]
            self.assertEqual(o2.observation_id, o.observation_id)
            self.assertEqual(o2.subject, "张三")
            self.assertEqual(
                o2.detail["geojson"]["type"], "FeatureCollection")
            # upsert 幂等：同 id 再写一次不堆条目
            save_directed_observations(case_dir, [o])
            self.assertEqual(len(load_directed_observations(case_dir)), 1)
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
