"""REQ-D-016 数据元合规扫描测试。

语义：属性经 {"data_element": "DE_X"} 引用数据元（loader 校验 ID 已注册，
REQ-D-002 AC-6 提前接线）→ compliance.scan 走网关对已物化属性逐值检查
format/checksum/range/enum；违规行落 run_diagnostic（对象.属性+代理键+违规码，
样本脱敏）；检查项可启停；未引用数据元的属性不扫（AC-8）。
"""
import json
import unittest

import duckdb

from core.ontology import build_ontology
from core.gateway import OntologyReadGateway
from core.run_health import RunHealth
from core import compliance
from tests.test_one2one import _PackCtx

_VALID_ID = "11010519491231002X"     # GB 11643 mod11 校验位合法
_BAD_CHECKSUM = "110105194912310021"  # 格式合法、校验位错

# 引用数据元的对象声明（name 引用 DE_NAME 仅覆盖"引用但无检查字段"路径）
_OBJ = {"name": "person", "title": "人员", "pk": "person_id", "kind": "entity",
        "name_property": "name",
        "properties": {"name": {"type": "string", "data_element": "DE_NAME"},
                       "id_card": {"type": "string", "data_element": "DE_IDCARD"},
                       "age": {"type": "integer", "data_element": "DE_AGE"},
                       "level": {"type": "string", "data_element": "DE_LEVEL"}}}

# 未引用数据元的对象（AC-8：不扫）
_OBJ_PLAIN = {"name": "person", "title": "人员", "pk": "person_id",
              "kind": "entity", "name_property": "name",
              "properties": {"name": "string", "id_card": "string",
                             "age": "integer", "level": "string"}}

_ELEMENTS = {
    "DE_NAME": {"name": "姓名", "type": "string"},
    "DE_IDCARD": {"name": "公民身份号码", "type": "string",
                  "format": r"^\d{17}[\dXx]$", "checksum": "idcard_mod11"},
    "DE_AGE": {"name": "年龄", "type": "integer",
               "range": {"min": 0, "max": 120}},
    "DE_LEVEL": {"name": "风险等级", "type": "string",
                 "enum": ["低", "中", "高"]},
}

_ROWS = [
    ("张三", _VALID_ID, "40", "低"),        # 全合规
    ("李四", _BAD_CHECKSUM, "40", "低"),    # checksum_failed
    ("王五", "BAD-ID", "40", "低"),         # format_mismatch (+checksum)
    ("赵六", _VALID_ID, "200", "极高"),     # range_violation + enum_unknown
]


def _bind():
    return {"object": "person",
            "source": {"table": "PERS",
                       "columns": {"name": "名称", "id_card": "证件",
                                   "age": "年龄", "level": "等级"}}}


def _run(rows=_ROWS, obj=None, elements=_ELEMENTS, checks_decl=None,
         scan_kwargs=None):
    """临时包 + 内存源 → build → scan。返回 (scan 摘要, 诊断行, conn)。"""
    with _PackCtx([obj or _OBJ], [_bind()]) as pc:
        if elements is not None:
            doc = {"schema_version": 2, "elements": elements}
            if checks_decl is not None:
                doc["compliance_checks"] = checks_decl
            (pc.d / "data_elements.json").write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "证件" VARCHAR, '
                     '"年龄" VARCHAR, "等级" VARCHAR)')
        conn.executemany("INSERT INTO PERS VALUES (?,?,?,?)", rows)
        build_ontology(conn, pack="p")
        gw = OntologyReadGateway(conn, pack="p")
        rh = RunHealth(conn, run_id="t")
        summary = compliance.scan(gw, health=rh, **(scan_kwargs or {}))
        diags = conn.execute(
            "SELECT severity, reason, detail FROM run_diagnostic "
            "WHERE kind='compliance_violation' AND run_id='t' "
            "ORDER BY seq").fetchall()
        return summary, diags, conn


class TestScanScope(unittest.TestCase):
    def test_no_reference_no_scan(self):
        """AC-8：未引用数据元的属性不扫——targets=0、零诊断。"""
        s, diags, _ = _run(obj=_OBJ_PLAIN, elements=_ELEMENTS)
        self.assertEqual(s["targets"], 0)
        self.assertEqual(s["scanned"], 0)
        self.assertEqual(diags, [])

    def test_targets_from_references(self):
        """引用数据元的属性成为扫描目标（4 个引用属性 → 4 目标；
        其中 name->DE_NAME 无检查字段，不逐值扫描 → scanned=3）。"""
        s, _, _ = _run()
        self.assertEqual(s["targets"], 4)
        self.assertEqual(s["scanned"], 3)
        self.assertNotIn("person.name", s["by_property"])
        self.assertIn("person.id_card", s["by_property"])
        self.assertIn("person.age", s["by_property"])
        self.assertIn("person.level", s["by_property"])

    def test_unknown_element_reference_hard_fail(self):
        """REQ-D-002 AC-6：引用未注册数据元 → 装载硬失败。"""
        obj = {"name": "person", "title": "人员", "pk": "person_id",
               "kind": "entity", "name_property": "name",
               "properties": {"name": "string",
                              "ghost": {"type": "string",
                                        "data_element": "DE_GHOST"}}}
        bind = {"object": "person",
                "source": {"table": "PERS",
                           "columns": {"name": "名称", "ghost": "证件"}}}
        with _PackCtx([obj], [bind]):
            with self.assertRaises(ValueError) as cm:
                build_ontology(duckdb.connect(":memory:"), pack="p")
        self.assertIn("DE_GHOST", str(cm.exception))

    def test_all_compliant_zero_violations(self):
        """全合规数据 → violations=0、rate=0、零诊断。"""
        s, diags, _ = _run(rows=[("张三", _VALID_ID, "40", "低")])
        self.assertEqual(s["violations"], 0)
        self.assertEqual(diags, [])
        for p, agg in s["by_property"].items():
            self.assertEqual(agg["violations"], 0)
            self.assertEqual(agg["rate"], 0)


class TestViolationCodes(unittest.TestCase):
    def test_format_violation(self):
        """格式违规 → format_mismatch。"""
        s, diags, _ = _run(rows=[("王五", "BAD-ID", "40", "低")])
        codes = s["by_property"]["person.id_card"]["codes"]
        self.assertIn(compliance.CODE_FORMAT, codes)
        self.assertTrue(diags)

    def test_checksum_violation(self):
        """校验位错 → checksum_failed。"""
        s, _, _ = _run(rows=[("李四", _BAD_CHECKSUM, "40", "低")])
        codes = s["by_property"]["person.id_card"]["codes"]
        self.assertEqual(codes, {compliance.CODE_CHECKSUM: 1})

    def test_range_violation(self):
        """range 越界（age=200 > max=120）→ range_violation。"""
        s, _, _ = _run(rows=[("赵六", _VALID_ID, "200", "低")])
        codes = s["by_property"]["person.age"]["codes"]
        self.assertEqual(codes, {compliance.CODE_RANGE: 1})

    def test_enum_violation(self):
        """代码表外取值（极高 ∉ 低/中/高）→ enum_unknown。"""
        s, _, _ = _run(rows=[("赵六", _VALID_ID, "40", "极高")])
        codes = s["by_property"]["person.level"]["codes"]
        self.assertEqual(codes, {compliance.CODE_ENUM: 1})


class TestDiagnostics(unittest.TestCase):
    def test_violation_row_has_proxy_key(self):
        """违规行带代理键可下钻：detail.key = pk 值 + object/prop/code 齐。"""
        _, diags, conn = _run(rows=[("王五", "BAD-ID", "40", "低")])
        pk = conn.execute(
            "SELECT person_id FROM obj_person WHERE name='王五'").fetchone()[0]
        d = json.loads(diags[0][2])
        self.assertEqual(d["object"], "person")
        self.assertEqual(d["prop"], "id_card")
        self.assertEqual(d["key"], pk)
        self.assertEqual(d["code"], compliance.CODE_FORMAT)

    def test_sample_masked_in_reason(self):
        """违规样本脱敏：reason 不含原值（违规值可能是敏感值）。"""
        _, diags, _ = _run(rows=[("王五", "BAD-ID", "40", "低")])
        self.assertNotIn("BAD-ID", diags[0][1])
        self.assertIn("B", diags[0][1])          # 脱敏后保留首字符
        self.assertIn("*", diags[0][1])

    def test_max_records_cap(self):
        """max_records 上限：违规全计数、落账截断（防诊断风暴）。

        "ABCDEFGHIJ" 每行同时触发 format_mismatch + checksum_failed
        （非 18 位 → 校验算法也判 False）→ 5 行 × 2 码 = 10 违规。
        """
        rows = [(f"n{i}", "ABCDEFGHIJ", "40", "低") for i in range(5)]
        s, diags, _ = _run(rows=rows, scan_kwargs={"max_records": 2})
        self.assertEqual(s["violations"], 10)
        self.assertEqual(s["recorded"], 2)
        self.assertEqual(len(diags), 2)


class TestChecksToggle(unittest.TestCase):
    def test_disable_via_declaration(self):
        """AC-6：data_elements.json 顶层 compliance_checks 关停 checksum。"""
        s, diags, _ = _run(rows=[("李四", _BAD_CHECKSUM, "40", "低")],
                           checks_decl={"checksum": False})
        self.assertEqual(s["violations"], 0)
        self.assertNotIn("checksum", s["checks"])
        self.assertEqual(diags, [])

    def test_disable_via_param(self):
        """AC-6：调用方 checks 参数优先——只查 format 时 checksum 不检。"""
        s, _, _ = _run(rows=[("李四", _BAD_CHECKSUM, "40", "低")],
                       scan_kwargs={"checks": ("format",)})
        self.assertEqual(s["checks"], ["format"])
        self.assertEqual(s["violations"], 0)


if __name__ == "__main__":
    unittest.main()
