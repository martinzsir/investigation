"""REQ-D-017 合规结果纳入画像测试。

合规违规率作为独立扣分项进 L5 质量分：复用 block/warn 两级与五要素
（scope/ref/code/reason/severity），code 前缀 compliance_ 与统计扣分可区分（AC-3）；
报告"待核实候选"定性不变（AC-4）；无合规属性时分数不变（AC-5 回归）。
"""
import json
import unittest

import duckdb

from core.ontology import build_ontology
from core.gateway import OntologyReadGateway
from core.ontology_profile import OntologyProfiler
from core.run_health import RunHealth
from tests.test_one2one import _PackCtx

# 引用数据元的对象（level 引用 DE_LEVEL 代码表）
_OBJ_REF = {"name": "person", "title": "人员", "pk": "person_id",
            "kind": "entity", "name_property": "name",
            "properties": {"name": "string",
                           "level": {"type": "string", "data_element": "DE_LEVEL"}}}
# 不引用数据元的同构对象（AC-5 回归对照）
_OBJ_PLAIN = {"name": "person", "title": "人员", "pk": "person_id",
              "kind": "entity", "name_property": "name",
              "properties": {"name": "string", "level": "string"}}

_ELEMENTS = {"DE_LEVEL": {"name": "风险等级", "type": "string",
                          "enum": ["低", "中", "高"]}}


def _bind():
    return {"object": "person",
            "source": {"table": "PERS",
                       "columns": {"name": "名称", "level": "等级"}}}


def _profile(rows, obj, elements=_ELEMENTS):
    """临时包 + 内存源 → build → 画像，返回 profile_all 报告。"""
    with _PackCtx([obj], [_bind()]) as pc:
        if elements is not None:
            (pc.d / "data_elements.json").write_text(
                json.dumps({"schema_version": 2, "elements": elements},
                           ensure_ascii=False), encoding="utf-8")
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "等级" VARCHAR)')
        conn.executemany("INSERT INTO PERS VALUES (?,?)", rows)
        build_ontology(conn, pack="p")
        gw = OntologyReadGateway(conn, pack="p")
        rh = RunHealth(conn, run_id="t")
        return OntologyProfiler(gw, pack="p", health=rh).profile_all()


def _compliance_deductions(report):
    return [d for d in report["l5"]["deductions"]
            if str(d["code"]).startswith("compliance_")]


def _rows(violations, total=10):
    """violations 个违规（"极高"）+ 其余合规（"低"）；name 互不相同。"""
    rows = [(f"人员{i}", "极高") for i in range(violations)]
    rows += [(f"合规{i}", "低") for i in range(total - violations)]
    return rows


class TestComplianceScore(unittest.TestCase):
    def test_warn_rate_triggers_deduction(self):
        """AC-1：违规率 40%（<50%）触发 warn 档 compliance_violation 扣分。"""
        rep = _profile(_rows(violations=4), _OBJ_REF)
        codes = {d["code"] for d in _compliance_deductions(rep)}
        self.assertIn("compliance_violation", codes)
        self.assertNotIn("compliance_violation_high", codes)
        d = next(d for d in _compliance_deductions(rep)
                 if d["code"] == "compliance_violation")
        self.assertEqual(d["severity"], "warn")
        self.assertEqual(d["points"], -10)

    def test_block_rate_high(self):
        """AC-1：违规率 60%（≥50%）触发 block 档 compliance_violation_high。"""
        rep = _profile(_rows(violations=6), _OBJ_REF)
        codes = {d["code"] for d in _compliance_deductions(rep)}
        self.assertIn("compliance_violation_high", codes)
        d = next(d for d in _compliance_deductions(rep)
                 if d["code"] == "compliance_violation_high")
        self.assertEqual(d["severity"], "block")
        self.assertEqual(d["points"], -20)

    def test_deduction_has_five_elements(self):
        """AC-2：合规扣分条目含完整五要素（scope/ref/code/reason/severity）。"""
        rep = _profile(_rows(violations=4), _OBJ_REF)
        d = next(d for d in _compliance_deductions(rep))
        for k in ("scope", "ref", "code", "reason", "severity"):
            self.assertIn(k, d, f"扣分项缺五要素字段 {k}")
        self.assertEqual(d["scope"], "prop")
        self.assertEqual(d["ref"], "person.level")
        self.assertIn("DE_LEVEL", d["reason"])     # reason 可定位到数据元

    def test_code_prefix_and_candidate_note(self):
        """AC-3/AC-4：code 前缀 compliance_ 与统计扣分可区分；权重独立档；
        报告"待核实候选"定性声明保留。"""
        rep = _profile(_rows(violations=4), _OBJ_REF)
        all_codes = {d["code"] for d in rep["l5"]["deductions"]}
        self.assertTrue(any(c.startswith("compliance_") for c in all_codes))
        # 权重表暴露独立 compliance 档
        self.assertIn("compliance", rep["l5"]["weights"])
        self.assertIn("compliance_violation", rep["l5"]["weights"]["compliance"])
        # 待核实定性不变
        self.assertIn("待核实", rep["note"])
        self.assertIn("待核实", rep["l5"]["note"])

    def test_no_compliance_property_score_unchanged(self):
        """AC-5：无数据元引用的属性不产生合规扣分（回归），分数不因合规项变动。"""
        rows = _rows(violations=4)
        ref = _profile(rows, _OBJ_REF)
        plain = _profile(rows, _OBJ_PLAIN)
        # 引用版有合规扣分，plain 版无
        self.assertTrue(_compliance_deductions(ref))
        self.assertEqual(_compliance_deductions(plain), [])
        # 两版非合规扣分完全一致（同数据同属性，唯一差异是合规扫描）
        def non_comp(r):
            return sorted((d["code"], d["ref"]) for d in r["l5"]["deductions"]
                          if not str(d["code"]).startswith("compliance_"))
        self.assertEqual(non_comp(ref), non_comp(plain))
        # plain 版分数 = 引用版分数 + 10（引用版多扣 warn 档 10 分）
        self.assertEqual(plain["l5"]["score"], ref["l5"]["score"] + 10)


if __name__ == "__main__":
    unittest.main()
