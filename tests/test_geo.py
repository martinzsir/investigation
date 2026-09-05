"""
tests/test_geo.py
REQ-G-021 地点标准化与空间匹配：
  AC1 「滨江路中段 K3+200」与「滨江路」判同地点
  AC2 完全无关地点不误判重合
  AC3 能力可被规则通过 function 挂钩调用（Function 目录可调）
  AC4 无坐标/无法解析时不报错而是降级标注
另：geocoder 注入后按距离阈值判定（近=同、远=异）。
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
