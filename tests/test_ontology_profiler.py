"""
tests/test_ontology_profiler.py
REQ-P M4 / P2 六层本体画像 测试（core/ontology_profile.py OntologyProfiler）。

覆盖（实施计划 §四 M4 + ONTOLOGY_PROFILER 六层）：
  L1/L2：已物化画像、未物化占位不报错、空值率、值类型分布/混装/落点、
         非 string 不识别、部分物化（缺列）不崩溃
  L3：万元整数率、时间窗覆盖、窗口外不命中、窗口可配置、无锚点 not_evaluated、
      关注命中、重合数、无 focus not_evaluated、metric 白名单
  L4：五间映射合法、未物化全缺失态、因间有数据源、禁 DEFAULT_JIAN_MAP
  L5：混装阻断、未物化仅告警、空值率阻断、分数区间、结论可推翻、只对可连接属性计分
  集成：只经 gateway（无自由 SQL/写路径）、只观察不写回
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.gateway import OntologyReadGateway              # noqa: E402
from core.ontology import build_ontology                 # noqa: E402
from core.ontology_profile import (                      # noqa: E402
    OntologyProfiler, JIAN_ORDER,
)
from core.threshold import (                              # noqa: E402
    load_profiler_settings, DEFAULT_PROFILER_SETTINGS,
)
from tests.test_ontology_version import make_store        # noqa: E402


def _profiler(**kw):
    s = make_store()
    build_ontology(s.conn)
    gw = OntologyReadGateway(s.conn)
    return s, gw, OntologyProfiler(gw, **kw)


def _entry(report, obj, prop):
    for e in report["l1_l2"]:
        if e["obj"] == obj and e["prop"] == prop:
            return e
    raise AssertionError(f"画像条目缺失：{obj}.{prop}")


def _l3(report, obj, prop, metric):
    for e in report["l3"]:
        if e.get("obj") == obj and e.get("prop") == prop \
                and e.get("metric") == metric:
            return e
    raise AssertionError(f"L3 指标缺失：{obj}.{prop}.{metric}")


class L1L2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.gw, cls.prof = _profiler(
            focus_entities=["张卫国"], anchor_date="2021-10-01")
        cls.report = cls.prof.profile_all()

    def test_ac01_已物化属性画像(self):
        e = _entry(self.report, "transaction", "from_raw")
        self.assertEqual(e["status"], "ok")
        vp = e["value_profile"]
        self.assertGreater(vp["row_count"], 0)
        self.assertIn("null_rate", vp)
        self.assertLessEqual(len(vp["samples"]), 5)   # 样例硬上限 5

    def test_ac02_未物化对象占位不报错(self):
        """osint_article 夹具缺列 optional 跳过 → 占位条目，画像不崩溃。"""
        e = _entry(self.report, "osint_article", "raw_name")
        self.assertEqual(e["status"], "unmaterialized_object")
        self.assertFalse(e["materialized_object"])

    def test_ac03_空值率(self):
        """org.relation 在夹具中全 NULL → 空值率 1.0。"""
        e = _entry(self.report, "org", "relation")
        self.assertEqual(e["value_profile"]["null_rate"], 1.0)

    def test_ac04_混装判定与落点(self):
        """from_raw 机构/人名混写 → mixed，两方向落点；断言有分布而非沙盒 50/50。"""
        e = _entry(self.report, "transaction", "from_raw")
        self.assertIn("type_dist", e)
        self.assertTrue(e["mixed"])
        self.assertEqual(e["landing_suggestions"], ["org", "person"])

    def test_ac05_非string不做值类型识别(self):
        e = _entry(self.report, "transaction", "amount")
        self.assertNotIn("type_dist", e)
        self.assertNotIn("mixed", e)

    def test_ac06_部分物化缺列不崩溃(self):
        """对象已物化但缺列 → missing_column 占位（缺陷 4）。

        缺列属 schema 演进，freshness 判 STALE 是正确防护；缺列诊断走显式
        allow_stale 留痕通道（调试画像的既定路径）。
        """
        from core.gateway import OntologyReadGateway as GW
        self.gw._conn.execute("ALTER TABLE obj_person DROP COLUMN raw_name")
        gw2 = GW(self.gw._conn, allow_stale=True)
        r = OntologyProfiler(gw2, focus_entities=["张卫国"],
                             anchor_date="2021-10-01").profile_all()
        e = _entry(r, "person", "raw_name")
        self.assertEqual(e["status"], "missing_column")
        self.assertFalse(e["materialized_prop"])


class L3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.gw, cls.prof = _profiler(
            focus_entities=["张卫国"], anchor_date="2021-10-01")
        cls.report = cls.prof.profile_all()

    def test_ac07_万元整数率(self):
        """夹具两笔（10 万/460 万）均万元整数 → 1.0。"""
        ind = _l3(self.report, "transaction", "amount", "wan_integer_rate")
        self.assertEqual(ind["value"], 1.0)
        self.assertEqual(ind["denominator"], 2)

    def test_ac08_时间窗覆盖与窗口外不命中(self):
        """交易日期在锚点 ±20 天 → 1.0；举报日 2022-01-10 在窗口外 → 0.0。"""
        self.assertEqual(
            _l3(self.report, "transaction", "date", "window_coverage")["value"], 1.0)
        self.assertEqual(
            _l3(self.report, "tipoff", "submit_date", "window_coverage")["value"], 0.0)

    def test_ac09_窗口可配置(self):
        """window_days 参数覆盖 thresholds：窗口收窄覆盖率下降。

        交易两笔 2021-09-28/2021-10-01，锚点 2021-10-01：
        window=20 两笔全中（1.0），window=0 仅当天一笔（0.5）。
        """
        _, _, p_zero = _profiler(
            focus_entities=["张卫国"], anchor_date="2021-10-01", window_days=0)
        r0 = p_zero.profile_all()
        self.assertEqual(
            _l3(r0, "transaction", "date", "window_coverage")["value"], 0.5)
        self.assertEqual(
            _l3(self.report, "transaction", "date", "window_coverage")["value"], 1.0)

    def test_ac10_无锚点not_evaluated(self):
        _, _, p = _profiler(focus_entities=["张卫国"])   # 无 anchor_date
        r = p.profile_all()
        e = _l3(r, "transaction", "date", "window_coverage")
        self.assertEqual(e["status"], "not_evaluated")

    def test_ac11_关注命中(self):
        """focus_entities=['张卫国']：from_raw distinct 2 个命中 1 → 0.5。"""
        ind = _l3(self.report, "transaction", "from_raw", "focus_hit_rate")
        self.assertEqual(ind["value"], 0.5)
        self.assertEqual(ind["numerator"], 1)

    def test_ac12_重合数(self):
        """与已知实体（person 表 raw_name）重合 ≥1。"""
        ind = _l3(self.report, "transaction", "from_raw", "known_overlap_count")
        self.assertGreaterEqual(ind["numerator"], 1)

    def test_ac13_无focus_not_evaluated(self):
        _, _, p = _profiler(anchor_date="2021-10-01")   # 无 focus_entities
        r = p.profile_all()
        e = _l3(r, "transaction", "from_raw", "focus_hit_rate")
        self.assertEqual(e["status"], "not_evaluated")

    def test_ac14_指标白名单(self):
        with self.assertRaises(ValueError):
            self.gw.prop_indicator("transaction", "amount", "not_a_metric")


class L4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, cls.prof = _profiler(
            focus_entities=["张卫国"], anchor_date="2021-10-01")
        cls.report = cls.prof.profile_all()

    def test_ac15_五间映射合法(self):
        rev = self.report["l4"]["reverse"]
        self.assertEqual([x["jian"] for x in rev], list(JIAN_ORDER))
        for x in rev:
            self.assertIn("declared", x)
            self.assertIn("objects", x)

    def test_ac16_未物化全缺失态(self):
        """不构建语义层 → 五间 has_materialized 全 False（数据缺失，声明仍在）。"""
        from core import Store
        gw = OntologyReadGateway(Store(db_path=":memory:").conn)
        r = OntologyProfiler(gw).profile_all()
        self.assertTrue(all(not x["has_materialized"]
                            for x in r["l4"]["reverse"]))

    def test_ac17_因间反间有声明(self):
        rev = {x["jian"]: x for x in self.report["l4"]["reverse"]}
        self.assertIn("bid_project", rev["因间"]["objects"])
        self.assertIn("time_window", rev["反间"]["links"])
        self.assertIn("tipoff", rev["内间"]["objects"])

    def test_ac18_无硬编码间类映射(self):
        """间类只来自 objects/links.json 的 jian 声明，模块无映射表赋值。"""
        import re
        src = (ROOT / "core" / "ontology_profile.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"DEFAULT_[A-Z_]*MAP\s*=", src),
                          "profiler 不得硬编码间类映射表")


class L5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, cls.prof = _profiler(
            focus_entities=["张卫国"], anchor_date="2021-10-01")
        cls.report = cls.prof.profile_all()

    def _deductions(self, code=None):
        ds = self.report["l5"]["deductions"]
        return [d for d in ds if code is None or d["code"] == code]

    def test_ac20_混装阻断(self):
        ds = self._deductions("mixed")
        self.assertTrue(ds)
        self.assertTrue(all(d["severity"] == "block" and d["points"] == -25
                            for d in ds))

    def test_ac21_未物化仅告警(self):
        ds = self._deductions("unmaterialized")
        self.assertTrue(ds)
        self.assertTrue(all(d["severity"] == "warn" and d["points"] == -12
                            for d in ds))

    def test_ac22_空值率阻断(self):
        ds = self._deductions("null_rate_high")
        self.assertTrue(any(d["ref"] == "org.relation" for d in ds))
        self.assertTrue(all(d["points"] == -20 for d in ds))

    def test_ac23_分数区间(self):
        lo, hi = self.report["l5"]["score_range"]
        self.assertGreaterEqual(lo, 0)
        self.assertLessEqual(hi, 100)
        self.assertLessEqual(lo, hi)

    def test_ac24_结论可推翻(self):
        l5 = self.report["l5"]
        self.assertTrue(l5["reviewable"])
        self.assertIn("【待核实】", l5["note"])
        self.assertIn("【待核实】", self.report["note"])

    def test_ac25_只对可连接属性计分(self):
        """元数据属性（org.status）不产生扣分（值类型识别无意义，防刷屏）。"""
        refs = {d["ref"] for d in self.report["l5"]["deductions"]}
        self.assertNotIn("org.status", refs)
        self.assertNotIn("tipoff.content_raw", refs)


class IntegrationTests(unittest.TestCase):
    def test_ac26_只经gateway无自由SQL(self):
        """profiler 不直连数据库执行 SQL（无 conn.execute/SELECT 字面量）。"""
        src = (ROOT / "core" / "ontology_profile.py").read_text(encoding="utf-8")
        for banned in ("conn.execute", "self._conn", "INSERT INTO",
                       "CREATE TABLE", "UPDATE ", "DELETE FROM", "COPY "):
            self.assertNotIn(banned, src, f"profiler 不得出现：{banned}")

    def test_ac27_只观察不写回(self):
        s, gw, prof = _profiler(focus_entities=["张卫国"],
                                anchor_date="2021-10-01")
        before = {n: gw.count("object", n) for n in gw.materialized_objects()}
        prof.profile_all()
        after = {n: gw.count("object", n) for n in gw.materialized_objects()}
        self.assertEqual(before, after)

    def test_ac29_阈值声明化(self):
        """window_days 从 thresholds.json profiler 段读（默认回落 + 声明覆盖）。"""
        s = load_profiler_settings("default")
        self.assertEqual(s["window_days"], 20)
        self.assertIn("draft_overlap_min_ratio", s)
        # 无文件 pack → 内置缺省
        self.assertEqual(
            load_profiler_settings("__no_such_pack__",
                                   base_dir=ROOT / "ontology" / "__none__"),
            DEFAULT_PROFILER_SETTINGS)


if __name__ == "__main__":
    unittest.main()
