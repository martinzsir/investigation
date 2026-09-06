"""REQ-D-020 单位与口径一致性测试。

数据元声明 unit（元/万元/%/小数），扫描金额类数据元跨表量级一致性。元/万元混用
在统计特征上看不出来却会让金额结论错误，故只**提示**不自动定性（金额突增可能
是真实业务）。

  - AC-1/AC-2 同一金额数据元跨表中位数相差 ≥10000 倍（元 vs 万元）→ 疑似混用/突增提示
  - AC-3 金额类属性引用的数据元未声明 unit → info 提示，非硬失败
  - AC-4 只告警不阻断（scan 不抛异常、不影响装载）
"""
import json
import unittest
from contextlib import contextmanager

import duckdb

from core.gateway import OntologyReadGateway
from core.ontology import build_ontology
from core.run_health import RunHealth
from core.unit_scan import scan
from tests.test_one2one import _PackCtx


def _obj(name):
    return {"name": name, "title": name, "pk": f"{name}_id", "kind": "entity",
            "name_property": "name",
            "properties": {"name": "string",
                           "amount": {"type": "decimal", "data_element": "DE_AMOUNT"}}}


def _bind(name, table):
    return {"object": name,
            "source": {"table": table,
                       "columns": {"name": "户名", "amount": "金额"}}}


def _write_de(d, unit):
    spec = {"name": "金额", "type": "decimal"}
    if unit is not None:
        spec["unit"] = unit
    (d / "data_elements.json").write_text(
        json.dumps({"schema_version": 2, "elements": {"DE_AMOUNT": spec}},
                   ensure_ascii=False), encoding="utf-8")


@contextmanager
def _pack(a_vals, b_vals, unit="元"):
    objs = [_obj("pay_a"), _obj("pay_b")]
    binds = [_bind("pay_a", "TA"), _bind("pay_b", "TB")]
    with _PackCtx(objs, binds) as pc:
        _write_de(pc.d, unit)
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TA ("户名" VARCHAR, "金额" VARCHAR)')
        conn.execute('CREATE TABLE TB ("户名" VARCHAR, "金额" VARCHAR)')
        conn.executemany("INSERT INTO TA VALUES (?,?)",
                         [(f"甲{i}", str(v)) for i, v in enumerate(a_vals)])
        conn.executemany("INSERT INTO TB VALUES (?,?)",
                         [(f"乙{i}", str(v)) for i, v in enumerate(b_vals)])
        build_ontology(conn, pack="p")
        yield conn


def _gw(conn):
    return OntologyReadGateway(conn, pack="p")


class TestUnitConsistency(unittest.TestCase):
    def test_AC1_AC2_magnitude_mismatch_detected(self):
        """元（中位数~150万）vs 万元（中位数~150）相差 10000 倍 → 疑似混用告警。"""
        with _pack([1_000_000, 2_000_000, 1_500_000], [100, 200, 150]) as conn:
            rh = RunHealth(conn)
            r = scan(_gw(conn), health=rh)
            self.assertEqual(len(r["mismatches"]), 1)
            self.assertGreaterEqual(r["mismatches"][0]["ratio"], 10000)
            s = rh.summary()
            self.assertGreaterEqual(s["by_kind"].get("unit_mismatch", 0), 1)
            self.assertEqual(s["by_severity"].get("warning", 0), 1)

    def test_AC3_missing_unit_is_hint_not_hard_fail(self):
        """数据元未声明 unit → info 提示补充，装载与扫描均不硬失败。"""
        with _pack([100, 200, 150], [110, 210, 160], unit=None) as conn:
            rh = RunHealth(conn)
            r = scan(_gw(conn), health=rh)
            self.assertGreaterEqual(len(r["missing_unit"]), 1)
            self.assertEqual(r["mismatches"], [])
            s = rh.summary()
            self.assertGreaterEqual(s["by_severity"].get("info", 0), 1)

    def test_consistent_magnitude_no_mismatch(self):
        """两表量级接近（比率 <10000）→ 不误报混用。"""
        with _pack([100, 200, 150], [110, 210, 160], unit="元") as conn:
            r = scan(_gw(conn))
            self.assertEqual(r["mismatches"], [])

    def test_AC4_warning_only_does_not_block(self):
        """混用场景 scan 不抛异常；语义层正常装载（obj_* 行齐全）。"""
        with _pack([1_000_000, 2_000_000], [100, 200]) as conn:
            n_a = conn.execute("SELECT COUNT(*) FROM obj_pay_a").fetchone()[0]
            n_b = conn.execute("SELECT COUNT(*) FROM obj_pay_b").fetchone()[0]
            self.assertEqual((n_a, n_b), (2, 2))   # 装载不受扫描影响
            r = scan(_gw(conn))                     # 扫描器自身不抛异常
            self.assertGreaterEqual(len(r["mismatches"]), 0)


if __name__ == "__main__":
    unittest.main()
