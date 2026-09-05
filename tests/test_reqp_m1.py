"""
tests/test_reqp_m1.py
REQ-P M1（F 波）已核实数据层缺陷修复：
  AC1/AC2 REQ-P-031 transfers 断链修复——双列方案（raw 保留 + account 外键），LEFT JOIN 无丢失
  AC3/AC4 REQ-P-032 tipoff_from_reporter 举报人归一——匿名降级（材料不丢、边不成立）
  AC5~AC8 REQ-P-033v1 normalize 段声明化——声明与 build_sql 一致性硬失败四态
  AC9/AC10 REQ-P-034 metadata_props 排除声明——装载与硬失败
兼容红线：不改变任何既有检测器行为；全部通过声明层 + loader 校验落地。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import Store
from core.ontology import ObjectType, LinkType
from core.ontology_loader import (
    load_pack, _load_bindings, _load_objects,
)

ROOT = Path(__file__).resolve().parent.parent


def _write_bindings(data: dict) -> Path:
    td = tempfile.mkdtemp()
    p = Path(td) / "bindings.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _mini_objects() -> list[ObjectType]:
    return [
        ObjectType(name="person", title="人", pk="person_id", kind="entity",
                   name_property="raw_name", properties={"raw_name": "string"}),
        ObjectType(name="tipoff", title="举报", pk="tipoff_id", kind="event",
                   name_property="title",
                   properties={"title": "string", "submit_date": "date",
                               "target_raw": "string", "reporter_raw": "string",
                               "content_raw": "string"}),
    ]


def _mini_link(name: str = "tip_from_reporter") -> list[LinkType]:
    return [LinkType(name=name, title=name, from_obj="tipoff", to_obj="person",
                     properties={})]


# ----------------------------------------------------------------------
# REQ-P-031 transfers 双列修复
# ----------------------------------------------------------------------
class TransfersFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_ontology import make_store
        from core.ontology import build_ontology
        cls.s = make_store()
        cls.stats = build_ontology(cls.s.conn)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_ac1_left_join无丢失且id非空raw保留(self):
        n_txn = self.s.query("SELECT COUNT(*) n FROM obj_transaction")[0]["n"]
        n_lnk = self.s.query("SELECT COUNT(*) n FROM lnk_transfers")[0]["n"]
        self.assertEqual(n_lnk, n_txn)            # LEFT JOIN 不丢边
        nulls = self.s.query(
            "SELECT COUNT(*) n FROM lnk_transfers "
            "WHERE from_account_id IS NULL OR to_account_id IS NULL")[0]["n"]
        self.assertEqual(nulls, 0)                # demo 数据同源，id 非空率 100%
        raws = self.s.query(
            "SELECT COUNT(*) n FROM lnk_transfers "
            "WHERE from_account IS NULL OR to_account IS NULL")[0]["n"]
        self.assertEqual(raws, 0)                 # raw 列保留（图库双轨同源红线）

    def test_ac2_endpoints指向外键列且声明含归一JOIN(self):
        pack = load_pack("default")
        tr = {l.name: l for l in pack.links}["transfers"]
        self.assertEqual(tr.endpoints["from"]["col"], "from_account_id")
        self.assertEqual(tr.endpoints["to"]["col"], "to_account_id")
        self.assertEqual(tr.endpoints["from"]["ref"]["object"], "account")
        extras = set(tr.endpoints["extra"])
        self.assertLessEqual({"from_account", "to_account"}, extras)
        lb = pack.link_bindings["transfers"]
        norm_pairs = {(n["table"], n["alias"]) for n in lb.normalize}
        self.assertEqual(norm_pairs, {("obj_account", "fa"),
                                      ("obj_account", "ta")})


# ----------------------------------------------------------------------
# REQ-P-032 举报人归一（匿名降级）
# ----------------------------------------------------------------------
class ReporterLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_ontology import make_store
        from core.ontology import build_ontology
        cls.s = make_store()
        cls.stats = build_ontology(cls.s.conn)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_ac3_材料不丢且匿名降级(self):
        n_tip = self.s.query("SELECT COUNT(*) n FROM obj_tipoff")[0]["n"]
        n_lnk = self.s.query(
            "SELECT COUNT(*) n FROM lnk_tipoff_from_reporter")[0]["n"]
        self.assertEqual(n_lnk, n_tip)            # LEFT JOIN：材料不丢
        # demo 举报人（匿名/内部职工/同行）未命中 person → 边不成立但不崩
        self.assertEqual(
            self.s.query("SELECT COUNT(*) n FROM lnk_tipoff_from_reporter "
                         "WHERE person_id IS NOT NULL")[0]["n"], 0)

    def test_ac4_命中时边成立_mini声明SQL(self):
        """同一段声明 SQL，reporter_raw 命中 person.raw_name 时边成立。"""
        s = Store(db_path=":memory:")
        s.execute("CREATE TABLE obj_person (person_id VARCHAR, raw_name VARCHAR)")
        s.execute("CREATE TABLE obj_tipoff (tipoff_id VARCHAR, title VARCHAR, "
                  "submit_date DATE, target_raw VARCHAR, reporter_raw VARCHAR, "
                  "content_raw VARCHAR)")
        s.execute("INSERT INTO obj_person VALUES ('p1','张三')")
        s.execute("INSERT INTO obj_tipoff VALUES "
                  "('t1','举报A','2021-10-01','李四','张三','x'),"
                  "('t2','举报B','2021-10-02','王五','匿名','y')")
        sql = load_pack("default").link_bindings["tipoff_from_reporter"].build_sql
        s.execute("CREATE TABLE lnk_mini AS " + sql)
        rows = {r["tipoff_id"]: r["person_id"]
                for r in s.query("SELECT tipoff_id, person_id FROM lnk_mini")}
        self.assertEqual(rows["t1"], "p1")        # 命中 → 边成立
        self.assertIsNone(rows["t2"])             # 匿名 → 边不成立、材料仍在
        self.assertEqual(len(rows), 2)
        s.close()


# ----------------------------------------------------------------------
# REQ-P-033v1 normalize 声明一致性（硬失败四态）
# ----------------------------------------------------------------------
class NormalizeValidationTests(unittest.TestCase):
    def _bindings(self, build_sql: str, normalize) -> dict:
        return {"schema_version": 2,
                "object_bindings": [
                    {"object": "person",
                     "source": {"table": "人员", "columns": {"raw_name": "姓名"}},
                     "optional": True},
                    {"object": "tipoff",
                     "source": {"table": "举报材料",
                                "columns": {"title": "分类", "submit_date": "举报日期",
                                            "target_raw": "被举报人",
                                            "reporter_raw": "举报人",
                                            "content_raw": "内容"}},
                     "optional": True}],
                "link_bindings": [
                    {"link": "tip_from_reporter", "build_sql": build_sql,
                     **({"normalize": normalize} if normalize is not None else {})}]}

    def test_ac5_声明JOIN与SQL不一致硬失败(self):
        data = self._bindings(
            "SELECT t.tipoff_id, p.person_id FROM obj_tipoff t "
            "JOIN obj_person p ON p.raw_name = t.target_raw",
            [{"as": "person_id", "alias": "q", "table": "obj_person",
              "on": "q.raw_name = t.target_raw", "select": "q.person_id"}])
        with self.assertRaises(ValueError):
            _load_bindings(_write_bindings(data), _mini_objects(), _mini_link())

    def test_ac6_select未投影硬失败(self):
        data = self._bindings(
            "SELECT t.tipoff_id, p.person_id FROM obj_tipoff t "
            "JOIN obj_person p ON p.raw_name = t.target_raw",
            [{"as": "reporter_person", "alias": "p", "table": "obj_person",
              "on": "p.raw_name = t.target_raw", "select": "p.person_id"}])
        with self.assertRaises(ValueError):
            _load_bindings(_write_bindings(data), _mini_objects(), _mini_link())

    def test_ac7_未声明归一JOIN硬失败(self):
        data = self._bindings(
            "SELECT t.tipoff_id, p.person_id FROM obj_tipoff t "
            "JOIN obj_person p ON p.raw_name = t.target_raw",
            None)
        with self.assertRaises(ValueError):
            _load_bindings(_write_bindings(data), _mini_objects(), _mini_link())

    def test_ac8_业务JOIN不触发归一校验(self):
        data = self._bindings(
            "SELECT t.tipoff_id FROM obj_tipoff t JOIN obj_tipoff u "
            "ON u.title = t.title",   # 业务 JOIN：非 raw_name 等值
            None)
        _, lb = _load_bindings(_write_bindings(data), _mini_objects(), _mini_link())
        self.assertEqual(lb["tip_from_reporter"].normalize, ())

    def test_ac9_default包八条链接全声明(self):
        pack = load_pack("default")
        declared = [n for n, b in pack.link_bindings.items() if b.normalize]
        self.assertEqual(len(declared), 8)
        self.assertNotIn("time_window", declared)   # 业务条件连接，无归一


# ----------------------------------------------------------------------
# REQ-P-034 metadata_props 排除声明
# ----------------------------------------------------------------------
class MetadataPropsTests(unittest.TestCase):
    def test_ac10_default包标注与理由(self):
        objs = {o.name: o for o in load_pack("default").objects}
        self.assertEqual(objs["tipoff"].metadata_props,
                         ("title", "submit_date", "content_raw"))
        self.assertEqual(objs["org"].metadata_props, ("status",))
        # 关系信号不标注：transaction.date / trackpoint.location 参与连接语义
        self.assertEqual(objs["transaction"].metadata_props, ())
        self.assertEqual(objs["trackpoint"].metadata_props, ())

    def test_ac11_引用未声明属性硬失败(self):
        p = tempfile.mkdtemp()
        f = Path(p) / "objects.json"
        f.write_text(json.dumps({"schema_version": 2, "objects": [{
            "name": "x", "pk": "x_id", "name_property": "raw_name",
            "properties": {"raw_name": "string"},
            "metadata_props": ["ghost"]}]}, ensure_ascii=False),
            encoding="utf-8")
        with self.assertRaises(ValueError):
            _load_objects(f)

    def test_ac12_重复项硬失败(self):
        p = tempfile.mkdtemp()
        f = Path(p) / "objects.json"
        f.write_text(json.dumps({"schema_version": 2, "objects": [{
            "name": "x", "pk": "x_id", "name_property": "raw_name",
            "properties": {"raw_name": "string", "note": "string"},
            "metadata_props": ["note", "note"]}]}, ensure_ascii=False),
            encoding="utf-8")
        with self.assertRaises(ValueError):
            _load_objects(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
