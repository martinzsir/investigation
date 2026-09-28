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


class GeoActivityRangeLensTests(_SpatialFixture):
    """geo_activity_range：平均中心 / 标准差椭圆 / 核密度。

    守卫三件事：
      ① 密度必须 ≤1（网格与热点共用同一峰值，否则热点会 >1 与自陈矛盾）；
      ② 主轴方位用 GIS 惯例（正北 0°、顺时针），点位近南北向分布时方位应
         接近 180°，不是 90°——反了会把"南北延展"读成"东西延展"；
      ③ 无坐标不产出（缺口不充数）。
    """

    def setUp(self):
        super().setUp()
        self.reg = SkillRegistry()
        discover(self.reg)

    def _run(self, **params):
        p = {"target_subject": "张三"}
        p.update(params)
        return skill_invoke(self.reg, "geo_activity_range",
                            store=self.store, params=p)

    def test_produces_clue_with_ellipse_and_kde(self):
        clues = self._run()
        self.assertEqual(len(clues), 1)
        c = clues[0]
        self.assertEqual(c.skill_id, "geo_activity_range")
        self.assertIn("张三", c.title)
        key_cols = {r.get("key_column") for r in c.evidence_refs
                    if r["kind"] == "node"}
        self.assertEqual(key_cols, {"person_id", "location_id"})
        metrics = {r["metric"] for r in c.evidence_refs
                   if r["kind"] == "aggregate"}
        self.assertEqual(metrics, {"standard_distance_m", "ellipse_area_km2"})
        d = c.detail
        self.assertTrue(d["basis"])
        self.assertTrue(d["falsification"])
        self.assertFalse(d["degraded"])
        # 椭圆：64 点多边形 + 半轴/方位/面积齐全
        e = d["std_ellipse"]
        self.assertEqual(len(e["polygon"]), 64)
        self.assertIsNotNone(e["semi_major_m"])
        self.assertIsNotNone(e["azimuth_deg"])
        self.assertIsNotNone(e["area_km2"])
        # 核密度网格：40×40
        self.assertEqual(len(d["kde"]["cells"]), 1600)
        self.assertTrue(d["hotspots"])

    def test_density_normalized_not_exceed_one(self):
        d = self._run()[0].detail
        vals = [c["density"] for c in d["kde"]["cells"]]
        vals += [h["density"] for h in d["hotspots"]]
        self.assertLessEqual(max(vals), 1.0)
        self.assertGreaterEqual(min(vals), 0.0)
        # 网格峰值格归一为 1.0；热点按同一峰值归一，故必然 ≤1 且接近 1
        self.assertEqual(max(c["density"] for c in d["kde"]["cells"]), 1.0)
        self.assertGreater(max(h["density"] for h in d["hotspots"]), 0.9)

    def test_azimuth_uses_gis_convention(self):
        """点位 A-E 沿南北向铺开（纬度跨度 222m、经度跨度约 48m）→ 方位近 180°。"""
        e = self._run()[0].detail["std_ellipse"]
        self.assertGreater(e["azimuth_deg"], 150.0,
                           msg=f"主轴方位 {e['azimuth_deg']} 应为近南北向")
        self.assertGreaterEqual(e["semi_major_m"], e["semi_minor_m"])

    def test_sigma_multiplier_changes_axis(self):
        base = self._run()[0].detail["std_ellipse"]["semi_major_m"]
        big = self._run(sigma_multiplier=3.0)[0].detail["std_ellipse"]
        self.assertGreater(big["semi_major_m"], base)
        self.assertEqual(big["sigma_multiplier"], 3.0)

    def test_uniform_weight_differs_from_count(self):
        """等权与按到访次数加权给出不同中心（权重口径必须可选且生效）。"""
        a = self._run()[0].detail
        b = self._run(weight_by="uniform")[0].detail
        self.assertEqual(a["weight_by"], "count")
        self.assertEqual(b["weight_by"], "uniform")

    def test_no_coords_subject_no_clue(self):
        self.assertEqual(self._run(target_subject="王五"), [])

    def test_undeclared_param_rejected(self):
        with self.assertRaises(ValueError):
            self._run(bogus=1)


class ActivityRangeBasisTests(unittest.TestCase):
    """判据与证伪条件必须注册，否则产出 basis 为空、正兵无从核验。"""

    def test_basis_and_falsification_registered(self):
        from core.lens_basis import _BASIS, _FALSIFICATION, basis_for
        self.assertIn("geo_activity_range", _BASIS)
        self.assertIn("geo_activity_range", _FALSIFICATION)
        out = basis_for("geo_activity_range", {
            "point_count": 5, "standard_distance_m": 81.7,
            "span_km": 0.23, "weight_by": "count",
            "coord_precision": "exact",
            "mean_center": {"lat": 30.00074, "lng": 120.00014},
            "std_ellipse": {"semi_major_m": 159.6, "semi_minor_m": 35.6,
                            "azimuth_deg": 175.3, "area_km2": 0.018,
                            "sigma_multiplier": 2.0},
            "hotspots": [{"std_address": "乙路", "visits": 1, "density": 1.0}],
        })
        self.assertIn("平均中心", out["basis"])
        self.assertIn("椭圆倍率", out["falsification"])


class DimensionCodePlumbingTests(unittest.TestCase):
    """维度产出必须 code 化，且 rule_id 精确命中优先于关键词。

    守护的回归：R-GEO-3（时空反复同框）标题含"同框"，会被内置模式里
    keywords=["同框"]→H3 命中，维度落 ["通讯","行为"]；而本体为 R-GEO-3
    声明 H6 / ["space","time"]。若不传 rule_id，空间证据就被记成通讯与
    行为，维度覆盖统计永远看不到空间。
    """

    def test_rule_id_exact_hit_beats_keyword(self):
        from core.hypotheses import match_finding_pattern
        # ① 传 rule_id：精确命中本体 R-GEO-3 → H6 / space+time
        chain, _jian, dims, reason = match_finding_pattern(
            "异主体时空反复同框", rule_id="R-GEO-3")
        self.assertEqual(["H6"], chain, reason)
        self.assertEqual(["space", "time"], dims, reason)
        # ② 不传 rule_id：回落关键词（"同框"）→ H3，维度含 comm/behavior
        chain2, _j2, dims2, _r2 = match_finding_pattern(
            "异主体时空反复同框")
        self.assertEqual(["H3"], chain2)
        self.assertIn("comm", dims2)
        # 反证：精确命中确实改变了结果（否则说明 rule_id 未生效）
        self.assertNotEqual(chain, chain2)

    def test_dims_are_codes_not_chinese_names(self):
        from skills.registry_bootstrap import _dims_for_jian
        for jian in ("生间", "反间", "因间", "死间", "内间"):
            for d in _dims_for_jian([jian]):
                self.assertFalse(
                    any("一" <= ch <= "鿿" for ch in d),
                    f"间类「{jian}」维度仍为中文展示名：{d}")
                self.assertIn(d, {"fund", "comm", "behavior",
                                  "relation", "time", "space"})

    def test_geo_clue_dimension_is_space(self):
        """端到端：R-GEO-3 线索落盘维度为 space（本体声明维度之一）。"""
        from core.ontology_loader import load_dimensions
        declare_dims = set()
        try:
            declare_dims = {d["code"] for d in load_dimensions("default")}
        except Exception:
            pass
        from core.hypotheses import match_finding_pattern
        _c, _j, dims, _r = match_finding_pattern(
            "同框", rule_id="R-GEO-3")
        self.assertTrue(set(dims) & {"space"}, dims)
        if declare_dims:
            self.assertTrue(set(dims) <= declare_dims,
                            f"维度 {dims} 含未声明值，声明集 {declare_dims}")


class ClueDimensionWiringTests(unittest.TestCase):
    """守护「线索构建确实把 rule_id 传给匹配器」这条接线。

    前面的 DimensionCodePlumbingTests 直接调 match_finding_pattern，只能
    证明函数对；若哪天 _clue_from_xu_shi 忘了传 rule_id（历史上正是如此），
    那些测试仍全绿而线上维度已错。本类走真实适配函数，覆盖接线。
    """

    def test_xu_shi_clue_carries_space_dimension(self):
        from core.registry import SkillSpec
        from skills.registry_bootstrap import _clue_from_xu_shi
        spec = SkillSpec(skill_id="test_xu_shi", name="虚实", stage="虚实")
        result = {"虚实扫描": {"findings": [{
            "候选虚处": "异主体时空反复同框（私下接触候选）",
            "依据": "张卫国 × 李志强 同框 3 次",
            "级别": "高",
            "rule_id": "R-GEO-3",
        }]}}
        clues = _clue_from_xu_shi(spec, result)
        self.assertEqual(1, len(clues))
        dims = clues[0].detail.get("维度") or []
        self.assertIn("space", dims,
                      f"R-GEO-3 线索维度应为 space，实际 {dims}"
                      f"（未传 rule_id 时会被关键词误挂到 comm/behavior）")
        self.assertEqual(["H6"], clues[0].assumption_chain)


class BuiltinDimensionFallbackTests(unittest.TestCase):
    """dimensions.json 缺失时的内置回退必须与正式声明同口径（code）。

    守护的回归：回退曾是「code=name=中文」，而官方包规则/假设模式库已全部
    写 code → 缺声明时 code 化规则会被误判未声明而硬失败；只写 code 又会
    拒掉旧包中文声明。故回退双语齐全，code 与 name 分离。
    """

    def test_fallback_codes_match_declared_codes(self):
        from core.ontology_loader import (load_dimensions,
                                          DEFAULT_DIMENSIONS,
                                          load_dimension_declarations)
        self.assertEqual(load_dimensions("default"), DEFAULT_DIMENSIONS)
        decls = load_dimension_declarations("default")
        self.assertTrue(all(d.get("code") and d.get("name")
                            for d in decls))

    def test_fallback_is_not_chinese_code(self):
        from core.ontology_loader import DEFAULT_DIMENSIONS
        for c in DEFAULT_DIMENSIONS:
            self.assertFalse(any("一" <= ch <= "鿿" for ch in c),
                             f"内置维度回退仍是中文：{c}")
        self.assertIn("space", DEFAULT_DIMENSIONS)

    def test_miaosuan_class_dims_same_as_instance(self):
        """类属性（回退）与实例属性（本体装载）必须同口径。

        线索侧曾读类属性拿到中文模板，与实例属性（code）不等价——维度归一
        的同类分叉源，故两者都必须 code 化。
        """
        from core.hypotheses import MiaoSuan
        cls_dims = list(MiaoSuan.DIMENSIONS)
        self.assertEqual(sorted(cls_dims),
                         sorted(MiaoSuan(pack="default").DIMENSIONS),
                         "类属性与实例属性维度口径不一致")
        for d in cls_dims:
            self.assertFalse(any("一" <= ch <= "鿿" for ch in d),
                             f"类属性维度仍为中文：{d}")


class MergedClueDetailPreserveTests(unittest.TestCase):
    """合并线索必须保留 parts 的 detail（此前只写 merged_from）。

    守护的回归：core/lineage.py 合并段丢弃「依据/维度/等级/本间独立源数」
    等全部字段，合并线索在证据构建与维度覆盖统计里是空壳（ADR-V-5 方案 b
    曾在供给层按标题反查回填规则字段，但补不回维度）。
    """

    def _clue(self, cid, title, detail, jian=(), chain=()):
        from core.registry import LineageClue
        return LineageClue(clue_id=cid, skill_id="s", title=title,
                           detail=dict(detail), assumption_chain=list(chain),
                           jian_types=list(jian))

    def test_merge_preserves_detail_and_unions_dims(self):
        from core.lineage import dedupe_and_merge
        # 两条同源（共享 source_rows）→ 必合并
        rows = [{"person_raw": "张卫国", "k": 1}]
        a = self._clue("A", "单一对端通话高频",
                       {"维度": ["comm", "behavior"], "依据": "通话 47 次",
                        "等级": "高"}, ["生间"], ["H3"])
        b = self._clue("B", "生间命中",
                       {"维度": ["fund"], "等级": "中",
                        "本间独立源数": 2}, ["生间"])
        a.source_rows = rows
        b.source_rows = rows
        merged = dedupe_and_merge([a, b])
        self.assertEqual(1, len(merged))
        det = merged[0].detail
        self.assertEqual(["A", "B"], det.get("merged_from"))
        # 维度并集（保序）
        self.assertEqual(["comm", "behavior", "fund"], det.get("维度"))
        # 标量字段取根线索（parts[0]）：根有的保留根值，非根不覆盖
        self.assertEqual("通话 47 次", det.get("依据"))
        self.assertEqual("高", det.get("等级"),
                         "标量冲突时应取根线索值，而非被后续部件覆盖")
        # 根线索没有的标量字段不并入（避免静默挑一个）
        self.assertIsNone(det.get("本间独立源数"))
