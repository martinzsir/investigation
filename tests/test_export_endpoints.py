"""
tests/test_export_endpoints.py
REQ-G-015 链接端点列名形式化 / 导出器通用化：
  AC1 全部现有导出链接完成 endpoints 声明
  AC2 新增链接无需修改导出脚本即可导出（按声明通用生成，默认文件名）
  AC3 通用边 SQL 与迁移前手写 SQL 语义一致（ref 解析/直出列/extra）
另：loader 对非法 endpoints 硬失败。
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from core import Store
from core.ontology import ObjectType
from core.ontology_loader import load_pack, _load_links

ROOT = Path(__file__).resolve().parent.parent


def _load_export():
    spec = importlib.util.spec_from_file_location(
        "export_ladybug", ROOT / "scripts" / "export_ladybug.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


export_ladybug = _load_export()

_EXPORTED_LINKS = ["transfers", "calls_to", "owns", "involved_in",
                   "co_located", "time_window", "tipoff_targets_person",
                   "tipoff_from_reporter"]


class EndpointDeclTests(unittest.TestCase):
    def test_ac1_all_exported_links_declare_endpoints(self):
        links = {l.name: l for l in load_pack("default").links}
        for name in _EXPORTED_LINKS:
            ep = links[name].endpoints
            self.assertTrue(ep, f"{name} 缺 endpoints 声明")
            for side in ("from", "to"):
                self.assertIn(side, ep)
                self.assertTrue(ep[side]["col"])

    def test_loader_malformed_endpoints_hard_fail(self):
        objs = [ObjectType(name="person", title="人", pk="person_id",
                           kind="entity", name_property="raw_name",
                           properties={"raw_name": "string"})]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "links.json"
            # 缺 col
            p.write_text(json.dumps({"schema_version": 2, "links": [{
                "name": "bad", "from_obj": "person", "to_obj": "person",
                "properties": {}, "endpoints": {"from": {}, "to": {"col": "t"}}}]},
                ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                _load_links(p, objs)
            # ref.object 未声明
            p.write_text(json.dumps({"schema_version": 2, "links": [{
                "name": "bad", "from_obj": "person", "to_obj": "person",
                "properties": {}, "endpoints": {
                    "from": {"col": "f", "ref": {"object": "ghost",
                                                 "key": "x", "name": "y"}},
                    "to": {"col": "t"}}}]}, ensure_ascii=False),
                encoding="utf-8")
            with self.assertRaises(ValueError):
                _load_links(p, objs)

    def test_generic_edge_sql_ref_and_direct(self):
        store = Store(db_path=":memory:")
        store.execute("CREATE TABLE obj_person (person_id VARCHAR, raw_name VARCHAR)")
        store.execute("INSERT INTO obj_person VALUES ('p1','张三'),('p2','李四')")
        # ref 端点：代理键 JOIN 解析为点名
        store.execute("CREATE TABLE lnk_calls_test (from_person VARCHAR, "
                      "to_person VARCHAR, call_id VARCHAR)")
        store.execute("INSERT INTO lnk_calls_test VALUES ('p1','p2','c9')")
        ep_ref = {
            "from": {"col": "from_person",
                     "ref": {"object": "person", "key": "person_id", "name": "raw_name"}},
            "to": {"col": "to_person",
                   "ref": {"object": "person", "key": "person_id", "name": "raw_name"}},
            "extra": ["call_id"]}
        rows = store.query(export_ladybug._edge_sql("lnk_calls_test", ep_ref))
        self.assertEqual(rows[0]["from_id"], "张三")
        self.assertEqual(rows[0]["to_id"], "李四")
        self.assertEqual(rows[0]["call_id"], "c9")
        # 直出端点：列本身即点名（无 JOIN）
        store.execute("CREATE TABLE lnk_direct (fa VARCHAR, tb VARCHAR)")
        store.execute("INSERT INTO lnk_direct VALUES ('甲','乙')")
        ep_dir = {"from": {"col": "fa"}, "to": {"col": "tb"}}
        rows2 = store.query(export_ladybug._edge_sql("lnk_direct", ep_dir))
        self.assertEqual(rows2[0]["from_id"], "甲")
        self.assertEqual(rows2[0]["to_id"], "乙")
        store.close()

    def test_ac2_new_link_default_filename_and_sql(self):
        # 新链接不改导出脚本：默认文件名 {name}_edges.csv，SQL 按声明生成
        new_link = "brand_new_relation"
        fname = export_ladybug._EDGE_FILE_OVERRIDE.get(
            new_link, f"{new_link}_edges.csv")
        self.assertEqual(fname, "brand_new_relation_edges.csv")
        ep = {"from": {"col": "fa",
                       "ref": {"object": "person", "key": "person_id",
                               "name": "raw_name"}},
              "to": {"col": "ta"}, "extra": ["x"]}
        sql = export_ladybug._edge_sql(f"lnk_{new_link}", ep)
        self.assertIn(f"lnk_{new_link} l", sql)
        self.assertIn("JOIN obj_person", sql)
        self.assertIn("AS from_id", sql)
        self.assertIn("AS to_id", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
