"""REQ-D-018 敏感列启发式扫描测试。

两路证据：列名词根（id_card/phone/证件/手机/卡号...）+ 值模式（18 位身份证 /
11 位手机 / 16-19 位卡号，有序首中即停）。已声明遮蔽的属性去重不报（AC-2）；
误报率可测（AC-3：scanned 分母 + 陷阱列 order_no 量化已知误报）；
只告警不阻断（AC-5：只落诊断，绝不抛异常）。
"""
import json
import unittest

import duckdb

from core.ontology import build_ontology
from core.gateway import OntologyReadGateway
from core.policy import PolicyEngine
from core.run_health import RunHealth
from core import sensitive_scan
from tests.test_one2one import _PackCtx

_ID1 = "11010519491231002X"    # mod11 合法
_ID2 = "110105199001010029"    # mod11 合法

_OBJ = {"name": "person", "title": "人员", "pk": "person_id", "kind": "entity",
        "name_property": "name",
        "properties": {"name": "string",
                       "id_card": "string",      # 列名提示 + 身份证值
                       "contact": "string",      # 无提示列名，手机值模式
                       "order_no": "string",     # 陷阱列：16 位单号（已知误报）
                       "remark": "string"}}      # 混合文本（不报）

_ROWS = [
    ("张三", _ID1, "13812345678", "1234567890123456", "2024-03-15 签约"),
    ("李四", _ID2, "13998877666", "1234567890123456", "现金往来见附件"),
]


def _bind():
    return {"object": "person",
            "source": {"table": "PERS",
                       "columns": {"name": "名称", "id_card": "证件号",
                                   "contact": "联系方式", "order_no": "订单号",
                                   "remark": "备注"}}}


def _run(rows=_ROWS, policies=None):
    with _PackCtx([_OBJ], [_bind()]) as pc:
        if policies is not None:
            (pc.d / "policies.json").write_text(
                json.dumps(policies, ensure_ascii=False), encoding="utf-8")
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "证件号" VARCHAR, '
                     '"联系方式" VARCHAR, "订单号" VARCHAR, "备注" VARCHAR)')
        conn.executemany("INSERT INTO PERS VALUES (?,?,?,?,?)", rows)
        build_ontology(conn, pack="p")
        gw = OntologyReadGateway(conn, pack="p")
        rh = RunHealth(conn, run_id="t")
        pe = (PolicyEngine("p", path=pc.d / "policies.json")
              if policies is not None else None)
        res = sensitive_scan.scan(gw, health=rh, policy=pe)
        diags = conn.execute(
            "SELECT severity, reason, detail FROM run_diagnostic "
            "WHERE kind='sensitive_column_suspect' AND run_id='t' "
            "ORDER BY seq").fetchall()
        return res, diags


def _suspected(res, obj, prop):
    return next((d for d in res["details"]
                 if d["object"] == obj and d["property"] == prop), None)


class TestSensitiveScan(unittest.TestCase):
    def test_column_name_hint_flags_idcard(self):
        """列名词根 + 值模式双证据：id_card 被报（name_idcard + value_idcard）。"""
        res, _ = _run()
        d = _suspected(res, "person", "id_card")
        self.assertIsNotNone(d)
        codes = {e.get("name_hint") or e.get("pattern")
                 for e in d["evidence"]}
        self.assertIn("name_idcard", codes)
        self.assertIn("value_idcard", codes)

    def test_value_pattern_flags_phone_without_hint(self):
        """列名无提示（联系方式）但值全为 11 位手机 → 值模式证据命中。"""
        res, _ = _run()
        d = _suspected(res, "person", "contact")
        self.assertIsNotNone(d)
        codes = [e.get("pattern") for e in d["evidence"] if e.get("pattern")]
        self.assertEqual(codes, ["value_phone"])
        self.assertFalse([e for e in d["evidence"] if e.get("name_hint")])

    def test_declared_masked_dedup(self):
        """AC-2：policies.json 已声明遮蔽的属性不再报（去重口径）。"""
        policies = {"schema_version": 2,
                    "property_policies": [
                        {"object": "person", "property": "id_card",
                         "allow_roles": ["human"], "mask": "partial"}]}
        res, _ = _run(policies=policies)
        self.assertIsNone(_suspected(res, "person", "id_card"))
        self.assertIsNotNone(_suspected(res, "person", "contact"))  # 其余照报

    def test_normal_text_not_flagged(self):
        """误报控制：混合文本备注、正常人名列不报。"""
        res, _ = _run()
        self.assertIsNone(_suspected(res, "person", "remark"))
        self.assertIsNone(_suspected(res, "person", "name"))

    def test_bankcard_trap_fp_measurable(self):
        """AC-3：陷阱列（16 位纯数字单号）按值模式误报——已知误报可量化。"""
        res, _ = _run()
        d = _suspected(res, "person", "order_no")
        self.assertIsNotNone(d)
        ev = next(e for e in d["evidence"] if e.get("pattern"))
        self.assertEqual(ev["pattern"], "value_bankcard")
        self.assertEqual(ev["ratio"], 1.0)
        # 误报率分母：scanned 覆盖全部有值的 string 属性（5 列）
        self.assertEqual(res["scanned"], 5)
        self.assertLessEqual(len(res["details"]), res["scanned"])

    def test_diagnostic_recorded_warning_only(self):
        """AC-5：疑似列落诊断 severity=warning；扫描只告警不阻断（正常返回）。"""
        res, diags = _run()
        self.assertEqual(res["suspects"], len(diags))
        self.assertTrue(diags)
        for sev, reason, detail in diags:
            self.assertEqual(sev, "warning")
            self.assertIn("补充遮蔽声明", reason)
            d = json.loads(detail)
            self.assertIn("evidence", d)
            self.assertIn("suggestion", d)

    def test_quiet_pack_zero_suspects(self):
        """无敏感列的包 → suspects=0、零诊断。"""
        obj = {"name": "person", "title": "人员", "pk": "person_id",
               "kind": "entity", "name_property": "name",
               "properties": {"name": "string"}}
        bind = {"object": "person",
                "source": {"table": "PERS", "columns": {"name": "名称"}}}
        with _PackCtx([obj], [bind]):
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PERS ("名称" VARCHAR)')
            conn.executemany("INSERT INTO PERS VALUES (?)", [("张三",), ("李四",)])
            build_ontology(conn, pack="p")
            gw = OntologyReadGateway(conn, pack="p")
            rh = RunHealth(conn, run_id="t")
            res = sensitive_scan.scan(gw, health=rh)
            diags = conn.execute(
                "SELECT COUNT(*) FROM run_diagnostic "
                "WHERE kind='sensitive_column_suspect'").fetchone()[0]
        self.assertEqual(res["suspects"], 0)
        self.assertEqual(diags, 0)


if __name__ == "__main__":
    unittest.main()
