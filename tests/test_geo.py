"""
tests/test_geo.py
REQ-G-021 地点标准化与空间匹配：
  AC1 「滨江路中段 K3+200」与「滨江路」判同地点
  AC2 完全无关地点不误判重合
  AC3 能力可被规则通过 function 挂钩调用（Function 目录可调）
  AC4 无坐标/无法解析时不报错而是降级标注
另：geocoder 注入后按距离阈值判定（近=同、远=异）。

PLAN-GEO-001 P2 空间研判 Function：
  grid_key 网格吸附、haversine 已知点距回归、CGT 核函数分段与确定性、
  geo_subject_sites / geo_co_located_radius / geo_buffer_scan /
  geo_profile_cgt 四函数（内存库最小语义表）+ 装载期声明校验。
"""
from __future__ import annotations

import unittest

from core import Store
from core import geo
from core.geo import normalize_location, locations_colocated
from core.functions import FUNCTION_IMPLS, invoke_function


class NormalizeTests(unittest.TestCase):
    def test_strip_k_marker_and_segment(self):
        self.assertEqual(normalize_location("滨江路中段 K3+200"), "滨江路")
        self.assertEqual(normalize_location("滨江路"), "滨江路")

    def test_intersection_trunk(self):
        self.assertEqual(
            normalize_location("中山路与解放路交叉口东侧50米"),
            "中山路与解放路")

    def test_unparseable_empty(self):
        self.assertEqual(normalize_location(""), "")
        self.assertEqual(normalize_location(None), "")
        self.assertEqual(normalize_location("某地点"), "")


class ColocatedTests(unittest.TestCase):
    def test_ac1_same_location(self):
        r = locations_colocated("滨江路中段 K3+200", "滨江路")
        self.assertTrue(r["colocated"])
        self.assertEqual(r["method"], "name_match")
        self.assertEqual(r["normalized_a"], "滨江路")
        self.assertEqual(r["normalized_b"], "滨江路")

    def test_ac2_unrelated_not_colocated(self):
        r = locations_colocated("滨江路中段 K3+200", "中山路与解放路交叉口")
        self.assertFalse(r["colocated"])
        # 异名且无坐标 → 保守不判同并降级标注（不误判）
        self.assertTrue(r["degraded"])

    def test_ac4_no_coords_degrades_not_raises(self):
        # 无坐标、异名：不抛错，返回降级标注
        r = locations_colocated("建设大道 K12+300", "解放大街 88 号")
        self.assertIn(r["method"], ("degraded", "name_match"))
        self.assertFalse(r["colocated"])
        # 无法解析：降级
        r2 = locations_colocated("无法识别地点", "滨江路")
        self.assertFalse(r2["colocated"])
        self.assertTrue(r2["degraded"])
        self.assertEqual(r2["method"], "degraded")

    def test_geocoder_distance(self):
        # 注入桩 geocoder：两个名字不同但坐标很近 → 同框；很远 → 不同框
        coords = {"A路": (30.0, 120.0), "B路": (30.0001, 120.0),   # ~11m
                  "C路": (31.0, 121.0)}                            # 上百公里
        geo.set_geocoder(lambda t: coords.get(t.strip()))
        try:
            near = locations_colocated("A路", "B路", radius_m=200)
            self.assertEqual(near["method"], "geocode")
            self.assertTrue(near["colocated"])
            self.assertIsNotNone(near["distance_m"])
            far = locations_colocated("A路", "C路", radius_m=200)
            self.assertFalse(far["colocated"])
        finally:
            geo.set_geocoder(None)

    def test_geocoder_failure_degrades(self):
        # geocoder 抛错 → 回落降级，不抛
        def boom(_t):
            raise RuntimeError("network down")
        geo.set_geocoder(boom)
        try:
            r = locations_colocated("A路", "B路", radius_m=200)
            self.assertFalse(r["colocated"])
            self.assertTrue(r["degraded"])
        finally:
            geo.set_geocoder(None)


class FunctionHookTests(unittest.TestCase):
    def test_registered(self):
        self.assertIn("location_colocated", FUNCTION_IMPLS)

    def test_ac3_invokable_via_catalog(self):
        store = Store(db_path=":memory:")
        # 经 Function 目录调用（与规则同一挂钩通道），自由文本 loc_a/loc_b 随 params 传入
        out = invoke_function(store, "location_colocated",
                              {"loc_a": "滨江路中段 K3+200", "loc_b": "滨江路",
                               "radius_m": 200})
        self.assertTrue(out.get("readonly"))
        res = out["result"]
        self.assertTrue(res["colocated"])
        store.close()

    def test_catalog_missing_args_degrades(self):
        store = Store(db_path=":memory:")
        out = invoke_function(store, "location_colocated", {"radius_m": 200})
        self.assertTrue(out["result"]["degraded"])
        self.assertFalse(out["result"]["colocated"])
        store.close()


# ======================================================================
# PLAN-GEO-001 P2：空间研判 Function
# ======================================================================
class GridKeyTests(unittest.TestCase):
    def test_none_coord_returns_none(self):
        self.assertIsNone(geo.grid_key(None, 120.0))
        self.assertIsNone(geo.grid_key(30.0, None))

    def test_same_cell_for_near_points(self):
        # ~73m 间距，200m 网格 → 同格
        a = geo.grid_key(30.0000, 120.0000, 200)
        b = geo.grid_key(30.0005, 120.0005, 200)
        self.assertIsNotNone(a)
        self.assertEqual(a, b)

    def test_far_points_different_cell(self):
        a = geo.grid_key(30.0000, 120.0000, 200)
        b = geo.grid_key(30.0100, 120.0000, 200)  # ~1.1km
        self.assertNotEqual(a, b)


class HaversineTests(unittest.TestCase):
    def test_known_distance_regression(self):
        # 0.001° 纬度 ≈ 111.19m（R=6371000）
        d = geo._haversine_m((30.0, 120.0), (30.001, 120.0))
        self.assertAlmostEqual(d, 111.19, delta=1.0)


class CgtKernelTests(unittest.TestCase):
    def test_term_segments(self):
        # d ≤ B：B^(g-f)/(2B-d)^g（缓冲区内低洼带，d=500 → 1/1500）
        self.assertAlmostEqual(geo.cgt_term(500, 1000), 1.0 / 1500)
        # d > B：1/d^f（缓冲带外衰减，d=1500 → 1/1500）
        self.assertAlmostEqual(geo.cgt_term(1500, 1000), 1.0 / 1500)
        # d = 0（防零下限外）：B^(g-f)/(2B)^g = 1/2000
        self.assertAlmostEqual(geo.cgt_term(0, 1000), 1.0 / 2000)
        # d ≥ 2B：0
        self.assertEqual(geo.cgt_term(2000, 1000), 0.0)
        self.assertEqual(geo.cgt_term(5000, 1000), 0.0)

    def test_surface_deterministic_and_normalized(self):
        pts = [(30.0, 120.0), (30.001, 120.0), (30.0005, 120.0005)]
        s1 = geo.cgt_surface(pts, grid_meters=200, buffer_m=1000)
        s2 = geo.cgt_surface(list(reversed(pts)), grid_meters=200,
                             buffer_m=1000)
        # 输入顺序无关（内部排序）→ 同输出
        self.assertEqual(s1["rows"], s2["rows"])
        self.assertEqual(s1["cols"], s2["cols"])
        self.assertEqual(s1["cells"], s2["cells"])
        # 归一化：顶格概率 = 1.0；cells 按概率降序
        self.assertAlmostEqual(s1["cells"][0]["probability"], 1.0)
        probs = [c["probability"] for c in s1["cells"]]
        self.assertEqual(probs, sorted(probs, reverse=True))
        # 修复分支互换后：顶格区不再贴在事件点上（缓冲区内是低洼带，
        # 格心到最近事件的距离显著大于防零下限 gm/4=50m）
        top = s1["cells"][0]
        d_top = min(geo._haversine_m((top["lat"], top["lng"]), p)
                    for p in pts)
        self.assertGreater(d_top, 500)

    def test_surface_empty_points(self):
        s = geo.cgt_surface([])
        self.assertEqual(s["cells"], [])
        self.assertEqual(s["rows"], 0)


class _SpatialFixture(unittest.TestCase):
    """内存库最小语义表：obj_person / obj_trackpoint / obj_location /
    lnk_trackpoint_at。坐标点围绕 (30.0, 120.0)，距离经 haversine 核算：
    loc_B≈111m、loc_C≈74m、loc_D≈222m、loc_E≈29m；loc_F 无坐标（同县）；
    loc_G 远在 ~167km（外县）。"""

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
            "location VARCHAR, date DATE)")
        s.execute(
            "CREATE TABLE lnk_trackpoint_at(track_id VARCHAR, "
            "location_id VARCHAR, date DATE)")
        for pid, name in (("p1", "张三"), ("p2", "李四"), ("p3", "王五")):
            s.execute("INSERT INTO obj_person VALUES (?, ?)", (pid, name))
        locs = [
            ("loc_A", "甲路", "330108", 30.0000, 120.0000),
            ("loc_B", "乙路", "330108", 30.0010, 120.0000),
            ("loc_C", "丙路", "330108", 30.0005, 120.0005),
            ("loc_D", "丁路", "330108", 30.0020, 120.0000),
            ("loc_E", "戊路", "330108", 30.0002, 120.0002),
            ("loc_F", "己路", "330108", None, None),       # 无坐标同县
            ("loc_G", "庚路", "310110", 31.5000, 121.5000),  # 远、外县
        ]
        for lid, addr, code, lat, lng in locs:
            s.execute(
                "INSERT INTO obj_location VALUES (?, ?, ?, NULL, NULL, '某县', "
                "NULL, ?, ?, ?, 'GCJ-02', 'test', 0.9, NULL)",
                (lid, addr, addr, code, lat, lng))
        tracks = [
            ("t1", "张三", "loc_A", "2026-01-01"),
            ("t2", "张三", "loc_B", "2026-01-05"),
            ("t3", "张三", "loc_C", "2026-01-09"),
            ("t4", "张三", "loc_D", "2026-01-13"),
            ("t5", "张三", "loc_E", "2026-01-17"),
            ("t6", "李四", "loc_A", "2026-01-01"),
            ("t7", "王五", "loc_F", "2026-01-02"),
            ("t8", "李四", "loc_G", "2026-01-10"),
        ]
        for tid, person, lid, d in tracks:
            s.execute("INSERT INTO obj_trackpoint VALUES (?, ?, ?, ?)",
                      (tid, person, f"地址{lid}", d))
            s.execute("INSERT INTO lnk_trackpoint_at VALUES (?, ?, ?)",
                      (tid, lid, d))

    def tearDown(self):
        self.store.close()

    def invoke(self, name, params=None):
        return invoke_function(self.store, name, params or {})


class GeoSubjectSitesTests(_SpatialFixture):
    def test_sites_aggregated(self):
        out = self.invoke("geo_subject_sites", {"target": "张三"})
        res = out["result"]
        self.assertTrue(res["hit"])
        self.assertEqual(res["site_count"], 5)
        self.assertEqual(res["total_visits"], 5)
        self.assertEqual(res["coord_coverage"],
                         {"total": 5, "with_coords": 5})
        self.assertFalse(res["degraded"])
        first_dates = {s["first_date"] for s in res["sites"]}
        self.assertIn("2026-01-01", first_dates)

    def test_missing_target_degrades(self):
        out = self.invoke("geo_subject_sites", {})
        self.assertTrue(out["result"]["degraded"])
        self.assertFalse(out["result"]["hit"])

    def test_unknown_subject_degrades(self):
        out = self.invoke("geo_subject_sites", {"target": "查无此人"})
        self.assertTrue(out["result"]["degraded"])
        self.assertFalse(out["result"]["hit"])


class GeoCoLocatedRadiusTests(_SpatialFixture):
    def test_dual_track_pairs(self):
        out = self.invoke("geo_co_located_radius",
                          {"radius_m": 200, "window_days": 1})
        res = out["result"]
        self.assertTrue(res["hit"])
        self.assertEqual(res["pair_count"], 3)
        self.assertEqual(res["geocode_pairs"], 1)      # 张三-李四 同点 0m
        self.assertEqual(res["admin_path_pairs"], 2)   # 无坐标回落同县
        methods = {(p["person_1"], p["person_2"]): p["method"]
                   for p in res["pairs"]}
        self.assertEqual(methods[("张三", "李四")], "geocode")
        self.assertEqual(methods[("张三", "王五")], "admin_path")
        self.assertEqual(methods[("李四", "王五")], "admin_path")

    def test_far_pair_not_colocated(self):
        # 李四 loc_G（上海）与任何人不同框；收紧窗口排除 admin_path 对
        out = self.invoke("geo_co_located_radius",
                          {"radius_m": 200, "window_days": 0})
        res = out["result"]
        pairs = res["pairs"]
        # window=0：t1-t6 同日 geocode 命中；t7 为 01-02 全部排除
        self.assertEqual(res["pair_count"], 1)
        self.assertEqual(pairs[0]["method"], "geocode")


class GeoBufferScanTests(_SpatialFixture):
    def test_explicit_center_bands(self):
        out = self.invoke("geo_buffer_scan",
                          {"center_lat": 30.0, "center_lng": 120.0,
                           "inner_radius_m": 200, "outer_radius_m": 500})
        res = out["result"]
        self.assertTrue(res["hit"])
        self.assertEqual(res["anchor"]["source"], "explicit")
        self.assertEqual(res["inner_count"], 5)   # A×2 + B + C + E
        self.assertEqual(res["ring_count"], 1)    # D ≈222m
        self.assertEqual(res["outside_count"], 1)  # G
        self.assertEqual(res["no_coord_count"], 1)  # F
        self.assertTrue(res["degraded"])           # 无坐标事件已标注
        ring_pk = res["ring_events"][0]["event_pk"]
        self.assertEqual(ring_pk, "t4")

    def test_missing_anchor_degrades(self):
        out = self.invoke("geo_buffer_scan", {})
        self.assertTrue(out["result"]["degraded"])
        self.assertFalse(out["result"]["hit"])

    def test_subject_centroid_anchor(self):
        out = self.invoke("geo_buffer_scan",
                          {"target": "张三", "inner_radius_m": 200,
                           "outer_radius_m": 500})
        res = out["result"]
        self.assertTrue(res["anchor"]["source"].startswith("subject_centroid:"))
        self.assertEqual(res["anchor"]["sites_used"], 5)


class GeoProfileCgtTests(_SpatialFixture):
    def test_single_subject_profile(self):
        out = self.invoke("geo_profile_cgt",
                          {"target": "张三", "min_events": 5})
        res = out["result"]
        self.assertTrue(res["hit"])
        self.assertEqual(res["events_used"], 5)
        self.assertEqual(res["events_dropped_no_coord"], 0)
        zones = res["priority_zones"]
        self.assertTrue(zones)
        self.assertAlmostEqual(zones[0]["probability"], 1.0)
        probs = [z["probability"] for z in zones]
        self.assertEqual(probs, sorted(probs, reverse=True))
        geojson = res["geojson"]
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertTrue(len(geojson["features"]) > 0)

    def test_deterministic_same_input_same_output(self):
        p = {"target": "张三", "min_events": 5}
        r1 = self.invoke("geo_profile_cgt", dict(p))["result"]
        r2 = self.invoke("geo_profile_cgt", dict(p))["result"]
        self.assertEqual(r1["priority_zones"], r2["priority_zones"])
        self.assertEqual(r1["geojson"], r2["geojson"])

    def test_min_events_degrades(self):
        # 李四仅 2 起带坐标事件 < min_events=5 → 降级不命中
        out = self.invoke("geo_profile_cgt",
                          {"target": "李四", "min_events": 5})
        res = out["result"]
        self.assertFalse(res["hit"])
        self.assertTrue(res["degraded"])
        self.assertFalse(res["profiles"][0]["profiled"])

    def test_scan_all_subjects(self):
        out = self.invoke("geo_profile_cgt", {"min_events": 5})
        res = out["result"]
        self.assertTrue(res["hit"])          # 张三 profiled
        self.assertEqual(res["profiled_count"], 1)
        by_subject = {p["subject_raw"]: p for p in res["profiles"]}
        self.assertTrue(by_subject["张三"]["profiled"])
        self.assertFalse(by_subject["李四"]["profiled"])
        self.assertFalse(by_subject["王五"]["profiled"])  # 0 起带坐标
        # 全扫模式不嵌 GeoJSON（产物紧凑，下钻走 target 模式）
        self.assertNotIn("geojson", by_subject["张三"])

    def test_missing_tables_degrade_not_raise(self):
        empty = Store(db_path=":memory:")
        try:
            out = invoke_function(empty, "geo_profile_cgt", {})
            res = out["result"]
            self.assertFalse(res["hit"])
            self.assertTrue(res["degraded"])
        finally:
            empty.close()


class P2DeclarationTests(unittest.TestCase):
    """装载期校验：4 个 Function 声明与 impl_ref 注册、R-GEO-1/R-GEO-2
    规则（assumption H5 已在假设库声明、dimension=behavior 合法）。"""

    def test_functions_registered(self):
        for name in ("geo_subject_sites", "geo_co_located_radius",
                     "geo_buffer_scan", "geo_profile_cgt"):
            self.assertIn(name, FUNCTION_IMPLS)

    def test_pack_loads_with_geo_declarations(self):
        from core.ontology_loader import load_pack
        spec = load_pack("default")
        for name in ("geo_subject_sites", "geo_co_located_radius",
                     "geo_buffer_scan", "geo_profile_cgt"):
            self.assertIn(name, spec.functions)
        self.assertIn("R-GEO-1", spec.rules)
        self.assertIn("R-GEO-2", spec.rules)
        self.assertEqual(spec.rules["R-GEO-1"].function, "geo_profile_cgt")
        self.assertEqual(spec.rules["R-GEO-2"].function, "geo_co_located_radius")


if __name__ == "__main__":
    unittest.main(verbosity=2)
