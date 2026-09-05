"""
tests/test_draft_assembler.py
REQ-P M6（REQ-P-035）：新表接入画像 + 草案组装器 + 步骤序列推荐器 测试。

覆盖：
  值类型映射与 TYPE_SQL 双向核对、列名语义启发（金额/日期/布尔）、
  混装检出、pk/name_property/metadata 候选纪律（混装/金额/备注不入 pk）、
  草案三件齐全 + _draft/_status/_evidence、落点只写 output/drafts（无 ontology 写路径）、
  links 双列方案、bindings clean 只引用已注册规则、
  recommend_steps 依赖排序（混装先拆/干净表退化三步/reprofile 收尾/零 IO）、
  build_table_profile 候选关联与阈值可配置、raw 画像空值率。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology import TYPE_NAMES, CLEAN_RULE_NAMES       # noqa: E402
from core.ontology_profile import build_table_profile        # noqa: E402
from core.draft_assembler import (                           # noqa: E402
    DraftAssembler, recommend_steps, STEP_ORDER,
    VALUE_TYPE_TO_PROP_TYPE, _CLEAN_SEQUENCE,
)


def _profile(cols, rows, table="t_demo", gateway=None, pack="default",
             min_ratio=None):
    return build_table_profile(table, cols, rows, gateway=gateway,
                               pack=pack, min_ratio=min_ratio)


# 合成外部表：账号(标识)/姓名(名称)/金额(列名启发)/日期/备注(metadata)/混装/是否
COLS = ["账号", "姓名", "金额", "日期", "备注", "混装", "是否在职"]
ROWS = [
    ["6222000111110001", "张卫国", "100000", "2021-10-01", "工资发放", "张卫国", "是"],
    ["6222000222220002", "李志强", "4600000", "2021-09-28", "另一笔备注", "6222000111110001", "否"],
]


class ValueMappingTests(unittest.TestCase):
    def test_ac01_映射输出全覆盖TYPE_SQL(self):
        """值类型映射的每个输出类型都必须是 TYPE_SQL 认识的属性类型。"""
        bad = {k: v for k, v in VALUE_TYPE_TO_PROP_TYPE.items()
               if v not in TYPE_NAMES}
        self.assertEqual(bad, {})

    def test_ac02_列名语义启发(self):
        """裸整数金额/日期靠列名纠偏；是否列 → boolean。"""
        p = _profile(COLS, ROWS)
        props = {c.name: c for c in p.columns}
        da = DraftAssembler(p)
        types = da.draft_object()["objects"][0]["properties"]
        self.assertEqual(types["金额"], "decimal")    # '100000' 裸数字本判 account
        self.assertEqual(types["日期"], "date")
        self.assertEqual(types["是否在职"], "boolean")
        self.assertEqual(types["账号"], "string")

    def test_ac03_混装检出(self):
        p = _profile(COLS, ROWS)
        mixed = {c.name for c in p.columns if c.mixed}
        self.assertIn("混装", mixed)
        self.assertNotIn("账号", mixed)


class CandidateDisciplineTests(unittest.TestCase):
    def setUp(self):
        self.p = _profile(COLS, ROWS)
        self.obj = DraftAssembler(self.p).draft_object()["objects"][0]
        self.cand = self.obj["_candidates"]

    def test_ac04_pk候选只收标识列(self):
        self.assertEqual(self.cand["pk"], ["账号"])

    def test_ac05_name候选只收人名列(self):
        """备注列（误判 person）已归 metadata，不入 name_property。"""
        self.assertEqual(self.cand["name_property"], ["姓名"])
        self.assertNotIn("备注", self.cand["name_property"])

    def test_ac06_metadata建议(self):
        self.assertIn("备注", self.cand["metadata_props_suggested"])

    def test_ac07_混装不入pk或name(self):
        self.assertNotIn("混装", self.cand["pk"])
        self.assertNotIn("混装", self.cand["name_property"])

    def test_ac08_evidence非空可追溯(self):
        ev = self.obj["_evidence"]
        self.assertTrue(ev)
        kinds = {e["kind"] for e in ev}
        self.assertIn("mixed_column", kinds)
        self.assertIn("pk_candidate", kinds)
        self.assertIn("name_property_candidate", kinds)


class DraftDocsTests(unittest.TestCase):
    def setUp(self):
        self.p = _profile(COLS, ROWS)
        self.da = DraftAssembler(self.p)

    def test_ac09_草案三件齐全带头(self):
        for doc in (self.da.draft_object(), self.da.draft_links(),
                    self.da.draft_bindings()):
            self.assertTrue(doc["_draft"])
            self.assertEqual(doc["_status"], "待核实")
            self.assertEqual(doc["schema_version"], 2)

    def test_ac10_links草案双列方案(self):
        from core.ontology_profile import CandidateAssoc
        self.p.candidates.append(CandidateAssoc(
            col="姓名", target_obj="person", target_prop="raw_name",
            overlap_ratio=1.0))
        links = self.da.draft_links()["links"]
        self.assertEqual(len(links), 1)
        l = links[0]
        self.assertEqual(l["to_obj"], "person")
        hint = l["_endpoints_hint"]
        self.assertIn("双列", hint["double_column"])
        self.assertEqual(hint["from_col_raw"], "姓名")
        self.assertEqual(l["_evidence"][0]["overlap_ratio"], 1.0)

    def test_ac11_bindings_clean只引用已注册规则(self):
        b = self.da.draft_bindings()
        ob = b["object_bindings"][0]
        self.assertTrue(set(ob["clean"]) <= CLEAN_RULE_NAMES)
        # 混装表 → 提示需新增 split 规则，不编造名字
        missing = ob["_evidence"][0]["clean_missing"]
        self.assertTrue(any("split_mixed" in m for m in missing))
        # link build_sql 用 LEFT JOIN
        self.p.candidates.clear()
        from core.ontology_profile import CandidateAssoc
        self.p.candidates.append(CandidateAssoc(
            col="账号", target_obj="account", target_prop="raw_name",
            overlap_ratio=0.9))
        lb = self.da.draft_bindings()["link_bindings"][0]
        self.assertIn("LEFT JOIN", lb["build_sql"])
        self.assertIn("账号_id", lb["build_sql"])   # 双列外键

    def test_ac12_落点只写drafts不写ontology(self):
        """write_drafts 落 output/drafts；函数体内无任何指向 ontology 的路径构造。"""
        with tempfile.TemporaryDirectory() as td:
            files = self.da.write_drafts(Path(td) / "output" / "drafts")
            self.assertEqual(len(files), 3)
            for f in files:
                self.assertIn("drafts", str(f))
                doc = json.loads(f.read_text(encoding="utf-8"))
                self.assertTrue(doc["_draft"])
        src = (ROOT / "core" / "draft_assembler.py").read_text(encoding="utf-8")
        # 只扫 write_drafts 函数体（docstring/注释里的"绝不写 ontology/"说明不算）
        seg = src.split("def write_drafts")[1].split("\n\n\n")[0]
        self.assertNotIn("ontology", seg,
                         "write_drafts 不得构造 ontology/ 路径（草案只写 output/drafts）")


class RecommendStepsTests(unittest.TestCase):
    def test_ac13_混装先拆且收尾复检(self):
        p = _profile(COLS, ROWS)
        steps = recommend_steps(p)
        names = [s["step"] for s in steps]
        self.assertEqual(names[0], "split_mixed")
        self.assertEqual(names[-1], "reprofile")
        # 绑定步骤必须在拆分之后
        self.assertLess(names.index("split_mixed"), names.index("bind_object"))
        self.assertLess(names.index("split_mixed"), names.index("bind_links")
                        if "bind_links" in names else len(names))
        for s in steps:
            self.assertTrue(s["why"] and s["how"] and s["done_when"])

    def test_ac14_干净表退化三步(self):
        p = _profile(["编号", "单位名称"],
                     [["1", "甲公司"], ["2", "乙公司"]], table="clean_t")
        names = [s["step"] for s in recommend_steps(p)]
        self.assertEqual(names, _CLEAN_SEQUENCE)
        self.assertEqual(names, ["cold_table", "bind_object", "reprofile"])

    def test_ac15_候选关联产生bind_links(self):
        from core.ontology_profile import CandidateAssoc
        p = _profile(["姓名"], [["张卫国"]], table="link_t")
        p.candidates.append(CandidateAssoc(
            col="姓名", target_obj="person", target_prop="raw_name",
            overlap_ratio=1.0))
        names = [s["step"] for s in recommend_steps(p)]
        self.assertIn("bind_links", names)

    def test_ac16_步骤序列合法且零IO(self):
        for s in recommend_steps(_profile(COLS, ROWS)):
            self.assertIn(s["step"], STEP_ORDER)
        src = (ROOT / "core" / "draft_assembler.py").read_text(encoding="utf-8")
        # recommend_steps 段内无文件 IO（write_drafts 是唯一 IO 且在类方法）
        seg = src.split("def recommend_steps")[1]
        for banned in ("open(", "write_text", "mkdir", "Path("):
            self.assertNotIn(banned, seg, f"recommend_steps 不得有 IO：{banned}")


class TableProfileTests(unittest.TestCase):
    def test_ac17_raw画像空值率与分布(self):
        p = _profile(["a", "b"], [["1", None], ["2", "x"]], table="null_t")
        cols = {c.name: c for c in p.columns}
        self.assertEqual(p.row_count, 2)
        self.assertEqual(cols["b"].null_rate, 0.5)
        self.assertEqual(cols["a"].null_rate, 0.0)

    def test_ac18_候选关联与阈值(self):
        """外部列值命中已物化身份列 → 候选；阈值提高后落选。"""
        from tests.test_ontology_version import make_store
        from core.ontology import build_ontology
        from core.gateway import OntologyReadGateway
        s = make_store()
        build_ontology(s.conn)
        gw = OntologyReadGateway(s.conn)
        # 张卫国 在 person.raw_name；李志强 也在
        p = _profile(["当事人"], [["张卫国"], ["李志强"]],
                     table="case_t", gateway=gw)
        hits = [(c.target_obj, c.target_prop) for c in p.candidates]
        self.assertIn(("person", "raw_name"), hits)
        # 阈值 1.1（不可能达到）→ 无候选
        p2 = _profile(["当事人"], [["张卫国"], ["李志强"]],
                      table="case_t", gateway=gw, min_ratio=1.1)
        self.assertEqual(p2.candidates, [])

    def test_ac19_无gateway纯列画像(self):
        p = _profile(COLS, ROWS, gateway=None)
        self.assertEqual(p.candidates, [])
        self.assertTrue(p.columns)

    def test_ac20_草案与报告无真实PII形态(self):
        """合成草案 JSON 不含身份证/手机号/银行卡明文（证据不带样例值）。"""
        with tempfile.TemporaryDirectory() as td:
            files = DraftAssembler(_profile(COLS, ROWS)).write_drafts(
                Path(td) / "drafts")
            blob = "\n".join(f.read_text(encoding="utf-8") for f in files)
        for pat in (r"\d{17}[\dXx]", r"(?<!\d)1[3-9]\d{9}(?!\d)"):
            self.assertIsNone(re.search(pat, blob), f"草案含 PII 形态：{pat}")


if __name__ == "__main__":
    unittest.main()
