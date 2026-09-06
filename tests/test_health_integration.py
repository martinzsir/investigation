"""REQ-D-022 质量结果进健康度（贯通汇聚）测试。

清洗剔除（clean_drop_rate）、CAST 隔离（source_value_quarantined）、
合规违规（compliance_violation）、敏感列告警（sensitive_column_suspect）四类
数据质量结果统一进同一 RunHealth 小节：
  - AC-1~AC-4 四类结果分别进入健康度且可检索；
  - AC-5 每类有独立 source 标识，可区分来源；
  - AC-6 健康度状态因数据质量问题降级（warning → degraded）；
  - AC-7 条目可下钻到具体行样本（代理键/脱敏样本）。
"""
import json
import unittest

import duckdb

from core.ontology import build_ontology
from core.gateway import OntologyReadGateway
from core.run_health import (RunHealth, record_clean_stats,
                             record_build_quarantine)
from core import compliance, sensitive_scan
from tests.test_one2one import _PackCtx

_OBJ = {"name": "person", "title": "人员", "pk": "person_id",
        "kind": "entity", "name_property": "name",
        "properties": {"name": "string",
                       "level": {"data_element": "DE_LEVEL"},
                       "id_card": "string"}}

# 三行：张三等级越界（合规违规）、A建材公司（清洗剔除）、李四合规；
# id_card 列名 + 18 位身份证形态触发敏感列（未声明遮蔽）。
_ROWS = [
    ("张三", "极高", "11010519491231002X"),
    ("A建材公司", "低", "310101199001011234"),
    ("李四", "低", "11010519491231002X"),
]


class TestHealthIntegration(unittest.TestCase):
    def setUp(self):
        bind = {"object": "person",
                "source": {"table": "PERS",
                           "columns": {"name": "名称", "level": "等级",
                                       "id_card": "证件"}},
                "clean": ["exclude_org_tokens"]}
        with _PackCtx([_OBJ], [bind]) as pc:
            (pc.d / "data_elements.json").write_text(
                json.dumps({"schema_version": 2,
                            "elements": {"DE_LEVEL": {"name": "风险等级",
                                                      "type": "string",
                                                      "enum": ["低", "中", "高"]}}},
                           ensure_ascii=False), encoding="utf-8")
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "等级" VARCHAR, '
                         '"证件" VARCHAR)')
            conn.executemany("INSERT INTO PERS VALUES (?,?,?)", _ROWS)
            stats = build_ontology(conn, pack="p")
            gw = OntologyReadGateway(conn, pack="p")
            self.rh = RunHealth(conn, run_id="t")
            # 构建期四类之"清洗剔除"与"CAST 隔离"落账（run_all 同路径）
            record_clean_stats(conn, stats, run_id="t")
            record_build_quarantine(conn, {"quarantine": [
                {"object": "person", "property": "amount", "column": "amount",
                 "quarantined_rows": 2, "reason": "金额 TRY_CAST 失败"}]},
                run_id="t")
            # 构建后质量门两类扫描（run_all 6.6 同路径）
            compliance.scan(gw, health=self.rh)
            sensitive_scan.scan(gw, health=self.rh)
            self.section = self.rh.health_section("t")
            self.rows = self.rh.rows("t")

    def _kinds(self):
        return self.section["分类计数"]

    def test_clean_drop_in_health(self):
        """AC-1：清洗剔除数进健康度（clean_drop_rate，可检索）。"""
        self.assertIn("clean_drop_rate", self._kinds())
        self.assertGreaterEqual(self._kinds()["clean_drop_rate"], 1)

    def test_quarantine_in_health(self):
        """AC-2：CAST 隔离行数进健康度（source_value_quarantined）。"""
        self.assertIn("source_value_quarantined", self._kinds())

    def test_compliance_in_health(self):
        """AC-3：合规违规数进健康度（compliance_violation）。"""
        self.assertIn("compliance_violation", self._kinds())

    def test_sensitive_in_health(self):
        """AC-4：敏感字段告警进健康度（sensitive_column_suspect）。"""
        self.assertIn("sensitive_column_suspect", self._kinds())

    def test_sources_distinguishable(self):
        """AC-5：四类结果独立 source 标识，来源可区分。"""
        src = self.section["来源计数"]
        # clean/quarantine 同源 build_ontology；compliance/sensitive_scan 各独立
        self.assertIn("build_ontology", src)
        self.assertIn("compliance", src)
        self.assertIn("sensitive_scan", src)
        comp_rows = [r for r in self.rows if r["kind"] == "compliance_violation"]
        sens_rows = [r for r in self.rows if r["kind"] == "sensitive_column_suspect"]
        self.assertEqual({r["source"] for r in comp_rows}, {"compliance"})
        self.assertEqual({r["source"] for r in sens_rows}, {"sensitive_scan"})

    def test_status_degraded_by_quality(self):
        """AC-6：数据质量问题（warning）使健康度降级为 degraded。"""
        self.assertEqual(self.section["status"], "degraded")
        self.assertGreaterEqual(self.section["计数"]["warning"], 4)

    def test_drilldown_to_samples(self):
        """AC-7：条目可下钻——合规行带代理键 + 脱敏样本，清洗行带脱敏样本。"""
        comp = next(r for r in self.rows
                    if r["kind"] == "compliance_violation")
        detail = comp["detail"] if isinstance(comp["detail"], dict) else {}
        self.assertTrue(detail.get("key"))              # 代理键可下钻
        self.assertEqual(detail.get("object"), "person")
        self.assertEqual(detail.get("prop"), "level")
        self.assertIn("*", comp["reason"])              # 样本脱敏
        clean = next(r for r in self.rows
                     if r["kind"] == "clean_drop_rate")
        cdetail = clean["detail"] if isinstance(clean["detail"], dict) else {}
        self.assertTrue(cdetail.get("sample_masked"))   # 剔除样本脱敏可下钻
        self.assertNotIn("A建材公司", json.dumps(cdetail, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
