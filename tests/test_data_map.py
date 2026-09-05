"""
tests/test_data_map.py
REQ-P M 波（025~030）数据地图 AC——全部零依赖（不建库、不 import duckdb）：
  L0 拓扑（025）：资产清单、语义度/物理度、隐形枢纽（缺陷1）、孤立对象、待归一属性候选
  L1 血缘（026）：对象←源表（UNION 拆解）、清洗规则、边←物理来源、
                归一定向（缺陷3）、等值归一判定（缺陷2）、业务条件不算归一
  缺口（028，缺陷4）：已归一的不是缺口；M1 修复后真实包缺口=0；内容字段排除
  解析鲁棒（029）：无别名表、关键字误抓行为固化、bindings 缺失不判定、无 JOIN 不报错、
                mini 声明独立解析、正则无 CTE 前提
  渲染（030）：Markdown/Mermaid、只观察不写回、零依赖
"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from core.data_map import (
    DataMap, TABLE_ALIAS_RE, _alias_map, _parse_link_sql,
)

ROOT = Path(__file__).resolve().parent.parent


def _real_map() -> DataMap:
    return DataMap.from_pack(ROOT / "ontology", "default")


# ----------------------------------------------------------------------
# mini 声明夹具：对齐 M1 修复前的真实缺陷形态（transfers 断链 + 举报人未归一）
# ----------------------------------------------------------------------
def _mini_objects_doc() -> dict:
    return {"schema_version": 2, "objects": [
        {"name": "person", "title": "人", "pk": "person_id", "kind": "entity",
         "name_property": "raw_name", "properties": {"raw_name": "string"}},
        {"name": "account", "title": "账户", "pk": "account_id", "kind": "entity",
         "name_property": "raw_name", "properties": {"raw_name": "string"}},
        {"name": "transaction", "title": "交易", "pk": "txn_id", "kind": "event",
         "name_property": "from_raw",
         "properties": {"from_raw": "string", "to_raw": "string",
                        "amount": "decimal", "date": "date"}},
        {"name": "tipoff", "title": "举报", "pk": "tipoff_id", "kind": "event",
         "name_property": "title",
         "properties": {"title": "string", "submit_date": "date",
                        "target_raw": "string", "reporter_raw": "string",
                        "content_raw": "string"},
         "metadata_props": ["content_raw"]},
    ]}


def _mini_links_doc() -> dict:
    # 缺陷 4 夹具：transfers 在 links.json 里端点 ref 声明齐全，但 build_sql 不 JOIN
    return {"schema_version": 2, "links": [
        {"name": "transfers", "title": "转账", "from_obj": "account", "to_obj": "account",
         "endpoints": {
             "from": {"col": "from_account_id",
                      "ref": {"object": "account", "key": "account_id", "name": "raw_name"}},
             "to": {"col": "to_account_id",
                    "ref": {"object": "account", "key": "account_id", "name": "raw_name"}}}},
        {"name": "tipoff_targets_person", "title": "举报针对",
         "from_obj": "tipoff", "to_obj": "person"},
    ]}


def _mini_bindings_doc() -> dict:
    return {"schema_version": 2,
            "object_bindings": [
                {"object": "person",
                 "source_sql": "SELECT 主体 AS raw_name FROM 通话记录 "
                               "UNION SELECT 对端 FROM 通话记录"},
                {"object": "account",
                 "source": {"table": "银行流水", "columns": {"raw_name": "主体"}}},
                {"object": "transaction",
                 "source": {"table": "银行流水", "columns": {"from_raw": "主体", "to_raw": "对方"}}},
                {"object": "tipoff",
                 "source": {"table": "举报材料",
                            "columns": {"target_raw": "被举报人", "reporter_raw": "举报人"}}},
            ],
            "link_bindings": [
                # 断链：无任何 JOIN obj_account（M1 修复前真实 transfers 形态）
                {"link": "transfers",
                 "build_sql": "SELECT t.txn_id, t.from_raw AS from_account, "
                              "t.to_raw AS to_account FROM obj_transaction t"},
                {"link": "tipoff_targets_person",
                 "build_sql": "SELECT t.tipoff_id, p.person_id, t.target_raw "
                              "FROM obj_tipoff t JOIN obj_person p ON p.raw_name = t.target_raw",
                 "normalize": [{"as": "person_id", "alias": "p", "table": "obj_person",
                                "on": "p.raw_name = t.target_raw",
                                "select": "p.person_id"}]},
            ]}


def _write_mini(bindings: dict | None) -> DataMap:
    td = tempfile.mkdtemp()
    pack = Path(td) / "mini"
    pack.mkdir()
    (pack / "objects.json").write_text(
        json.dumps(_mini_objects_doc(), ensure_ascii=False), encoding="utf-8")
    (pack / "links.json").write_text(
        json.dumps(_mini_links_doc(), ensure_ascii=False), encoding="utf-8")
    if bindings is not None:
        (pack / "bindings.json").write_text(
            json.dumps(bindings, ensure_ascii=False), encoding="utf-8")
    return DataMap.from_pack(pack.parent, "mini")


# ----------------------------------------------------------------------
# L0 静态拓扑（REQ-P-025）—— 真实 default 包
# ----------------------------------------------------------------------
class L0TopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dm = _real_map()
        cls.inv = {i["name"]: i for i in cls.dm.objects_inventory()}

    def test_ac01_资产清单覆盖全部声明对象(self):
        declared = {o["name"] for o in json.loads(
            (ROOT / "ontology/default/objects.json").read_text(encoding="utf-8"))["objects"]}
        self.assertEqual(set(self.inv), declared)
        self.assertEqual(len(self.inv), 11)

    def test_ac02_语义度为非runtime链接端点计数(self):
        # M1 新增 tipoff_from_reporter 后 person=8（沙盒基线 7 为修复前口径）
        self.assertEqual(self.inv["person"]["semantic_degree"], 8)
        self.assertEqual(self.inv["account"]["semantic_degree"], 3)
        self.assertEqual(self.inv["bid_project"]["semantic_degree"], 2)
        self.assertEqual(self.inv["transaction"]["semantic_degree"], 1)
        self.assertEqual(self.inv["call"]["semantic_degree"], 0)
        self.assertEqual(self.inv["trackpoint"]["semantic_degree"], 0)
        self.assertEqual(self.inv["clue"]["semantic_degree"], 0)  # decision_for 是 runtime 不计

    def test_ac03_物理度为引用该对象的链接绑定数(self):
        self.assertEqual(self.inv["person"]["physical_degree"], 6)
        self.assertEqual(self.inv["account"]["physical_degree"], 2)   # transfers(M1)/owns
        self.assertEqual(self.inv["transaction"]["physical_degree"], 2)
        self.assertEqual(self.inv["call"]["physical_degree"], 1)
        self.assertEqual(self.inv["trackpoint"]["physical_degree"], 1)
        self.assertEqual(self.inv["clue"]["physical_degree"], 0)

    def test_ac04_隐形枢纽不因孤儿判定丢失(self):
        # 缺陷 1：物理度累计在孤儿判定之前——call/trackpoint 是隐形枢纽而非孤立
        for name in ("call", "trackpoint"):
            self.assertTrue(self.inv[name]["hidden_hub"])
            self.assertIn("隐形枢纽", self.inv[name]["verdict"])
            self.assertFalse(self.inv[name]["orphan"])
        self.assertEqual(self.inv["person"]["verdict"], "核心枢纽")

    def test_ac05_孤立对象(self):
        self.assertTrue(self.inv["clue"]["orphan"])
        self.assertTrue(self.inv["decision"]["orphan"])
        self.assertIn("runtime", self.inv["decision"]["verdict"])

    def test_ac06_待归一属性候选清单(self):
        cands = set(self.dm._raw_candidates())
        self.assertIn(("transaction", "from_raw"), cands)
        self.assertIn(("transaction", "to_raw"), cands)
        self.assertIn(("osint_article", "raw_name"), cands)  # event 的 name_property 仍需归一判定
        self.assertNotIn(("person", "raw_name"), cands)      # entity 身份列是归一目标本身
        self.assertNotIn(("account", "raw_name"), cands)
        self.assertNotIn(("org", "raw_name"), cands)


# ----------------------------------------------------------------------
# L1 物理血缘（REQ-P-026/027/028）—— 真实 default 包
# ----------------------------------------------------------------------
class L1LineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dm = _real_map()

    def test_ac07_对象来源表含UNION多源拆解(self):
        lin = self.dm.lineage()
        p = lin["objects"]["person"]
        self.assertEqual(p["union_branches"], 6)
        self.assertEqual(len(p["source_tables"]), 5)
        self.assertIn("通话记录", p["source_tables"])
        self.assertIn("举报材料", p["source_tables"])
        self.assertEqual(lin["objects"]["org"]["source_tables"], ["工商信息"])
        self.assertEqual(lin["objects"]["account"]["union_branches"], 2)

    def test_ac08_清洗规则清单(self):
        lin = self.dm.lineage()
        self.assertIn("strip", lin["clean_rules"])
        self.assertIn("exclude_org_tokens", lin["clean_rules"])
        self.assertEqual(lin["objects"]["person"]["clean"], ["strip", "exclude_org_tokens"])

    def test_ac09_边物理来源对象(self):
        lin = self.dm.lineage()
        self.assertLessEqual({"transaction", "account"},
                             set(lin["links"]["transfers"]["source_objects"]))
        self.assertIn("call", lin["links"]["calls_to"]["source_objects"])
        self.assertIn("trackpoint", lin["links"]["co_located"]["source_objects"])
        self.assertEqual(lin["links"]["transfers"]["declared"], ["account", "account"])

    def test_ac10_归一定向JOIN表是target(self):
        # 缺陷 3：JOIN 的表是 target、另一侧是 source（8 条边实测方向）
        def directions(link):
            return {(j["source_obj"], j["target_obj"])
                    for j in self.dm.normalize_joins() if j["link"] == link}

        self.assertEqual(directions("calls_to"), {("call", "person")})
        self.assertEqual(directions("owns"), {("account", "person")})
        self.assertEqual(directions("involved_in"), {("bid_project", "org")})
        self.assertEqual(directions("transfers"), {("transaction", "account")})
        self.assertEqual(directions("osint_mentions"), {("osint_article", "person")})
        self.assertEqual(directions("tipoff_targets_person"), {("tipoff", "person")})
        self.assertEqual(directions("tipoff_from_reporter"), {("tipoff", "person")})
        self.assertEqual(directions("co_located"), {("trackpoint", "person")})

    def test_ac11_等值归一判定与业务条件(self):
        joins = self.dm.normalize_joins()
        # 缺陷 2：owns/osint_mentions 两侧都是 raw_name——判归一不误判业务条件
        self.assertTrue(any(j["link"] == "owns" and j["source_prop"] == "raw_name"
                            and j["target_prop"] == "raw_name" for j in joins))
        self.assertTrue(any(j["link"] == "osint_mentions"
                            and j["source_obj"] == "osint_article" for j in joins))
        # co_located 的 location 自连接/时间差条件不是归一（无 raw）
        self.assertEqual({(j["source_obj"], j["source_prop"])
                          for j in joins if j["link"] == "co_located"},
                         {("trackpoint", "person_raw")})
        lin = self.dm.lineage()
        self.assertEqual(lin["links"]["time_window"]["kind"], "业务条件连接")
        self.assertEqual(lin["links"]["co_located"]["kind"], "归一连接")
        # M1 normalize 段声明与 build_sql 一致 → 全部已声明
        self.assertTrue(all(j["declared"] for j in joins))

    def test_ac12_已归一的不是缺口(self):
        # 缺陷 4：判据看 build_sql 是否已归一，不看 links.json 端点
        gaps = {(g["object"], g["prop"]) for g in self.dm.normalize_gaps()}
        for ref in [("call", "caller_raw"), ("call", "callee_raw"),
                    ("trackpoint", "person_raw"), ("bid_project", "winner_raw"),
                    ("tipoff", "target_raw")]:
            self.assertNotIn(ref, gaps, ref)

    def test_ac13_M1修复后真实包缺口为0(self):
        self.assertEqual(self.dm.normalize_gaps(), [])

    def test_ac14_内容字段排除(self):
        gaps = {(g["object"], g["prop"]) for g in self.dm.normalize_gaps() or []}
        self.assertNotIn(("tipoff", "content_raw"), gaps)
        # mini：content_raw 有 metadata_props 标记 → 不入归一候选
        dm = _write_mini(_mini_bindings_doc())
        self.assertNotIn(("tipoff", "content_raw"), set(dm._raw_candidates()))


# ----------------------------------------------------------------------
# 解析鲁棒（REQ-P-029）
# ----------------------------------------------------------------------
class ParseRobustnessTests(unittest.TestCase):
    def test_ac15_无别名表解析(self):
        dm = _write_mini({"link_bindings": [
            {"link": "x_norm",
             "build_sql": "SELECT obj_transaction.txn_id, obj_person.person_id "
                          "FROM obj_transaction JOIN obj_person "
                          "ON obj_person.raw_name = obj_transaction.from_raw"}]})
        joins = dm.normalize_joins()
        self.assertEqual(len(joins), 1)
        self.assertEqual((joins[0]["source_obj"], joins[0]["target_obj"]),
                         ("transaction", "person"))
        self.assertIsNone(joins[0]["join_alias"])

    def test_ac16_关键字误抓行为固化与别名层过滤(self):
        # 固化已知行为：表无别名且紧跟 JOIN 时，JOIN 被抓成该表的"别名"（正则层不修）
        probe = "FROM obj_transaction JOIN obj_person p ON p.raw_name = obj_transaction.from_raw"
        self.assertIn(("obj_transaction", "JOIN"), TABLE_ALIAS_RE.findall(probe))
        # 过滤在 _alias_map 层：关键字别名被丢弃
        self.assertEqual(_alias_map(probe), {"p": "obj_person"})
        # 全管线不丢表：带别名的正常 SQL 两表都解析、归一对正确检出（JOIN 表是 target）
        p = _parse_link_sql("SELECT t.txn_id, p.person_id FROM obj_transaction t "
                            "JOIN obj_person p ON p.raw_name = t.from_raw", {})
        self.assertEqual(p["tables"], ["obj_transaction", "obj_person"])
        self.assertEqual((p["normalizes"][0]["source_obj"],
                          p["normalizes"][0]["target_obj"]), ("transaction", "person"))

    def test_ac17_bindings缺失不崩溃且不判定(self):
        dm = _write_mini(None)
        self.assertIsNone(dm.normalize_gaps())
        self.assertTrue(any("未计算" in n for n in dm.notes))
        self.assertIn("未计算", dm.render_markdown())

    def test_ac18_无JOIN链接不报错标业务条件(self):
        dm = _write_mini({"link_bindings": [
            {"link": "nojoin", "build_sql": "SELECT t.txn_id FROM obj_transaction t"}]})
        self.assertEqual(dm.normalize_joins(), [])
        self.assertEqual(dm.lineage()["links"]["nojoin"]["kind"], "业务条件连接")

    def test_ac19_mini声明独立解析检出缺口与断链虚线(self):
        dm = _write_mini(_mini_bindings_doc())
        gaps = {(g["object"], g["prop"]) for g in dm.normalize_gaps()}
        self.assertEqual(gaps, {("transaction", "from_raw"), ("transaction", "to_raw"),
                                ("tipoff", "reporter_raw")})
        # 缺陷 4：transfers 端点在 links.json 声明了 ref，仍按 build_sql 判缺口
        self.assertEqual(dm.lineage()["links"]["tipoff_targets_person"]["kind"], "归一连接")
        self.assertIn("-.->", dm.render_mermaid())   # transfers 断链虚线

    def test_ac20_正则无CTE前提(self):
        # 前提固化：真实包全部 build_sql 无 WITH——引入 CTE 时此测试失败提醒重估
        dm = _real_map()
        self.assertEqual([n for n, p in dm._parsed.items() if p["has_with"]], [])


# ----------------------------------------------------------------------
# 渲染与红线（REQ-P-030）
# ----------------------------------------------------------------------
class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dm = _real_map()

    def test_ac21_markdown结构与红线声明(self):
        md = self.dm.render_markdown()
        for frag in ("L0 静态拓扑", "L1 物理血缘", "归一 JOIN 清单", "归一缺口",
                     "只观察不写回", "【待核实】"):
            self.assertIn(frag, md)

    def test_ac22_mermaid隐形枢纽星标与无断链虚线(self):
        mer = self.dm.render_mermaid()
        self.assertTrue(mer.startswith("graph"))
        self.assertIn("★ call", mer)
        self.assertIn("★ trackpoint", mer)
        self.assertIn("（runtime）", mer)
        self.assertNotIn("-.->", mer)   # M1 修复后无断链

    def test_ac23_只观察不写回静态扫描(self):
        src = (ROOT / "core" / "data_map.py").read_text(encoding="utf-8")
        up = src.upper()
        for kw in ("INSERT ", "UPDATE ", "DELETE ", "COPY ", "CREATE "):
            self.assertNotIn(kw, up)
        self.assertNotIn('"w"', src)   # 无写文件模式


# ----------------------------------------------------------------------
# 零依赖（REQ-P-030）
# ----------------------------------------------------------------------
class ZeroDependencyTests(unittest.TestCase):
    def test_ac24_零依赖只import_json_re(self):
        src = (ROOT / "core" / "data_map.py").read_text(encoding="utf-8")
        mods = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods.add(n.module.split(".")[0])
        self.assertLessEqual(mods, {"__future__", "json", "re"}, mods)


if __name__ == "__main__":
    unittest.main()
