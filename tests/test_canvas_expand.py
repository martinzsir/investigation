"""
tests/test_canvas_expand.py
RC-103/104 画布逐层溯源 expand 服务单测（server/app/canvas_expand.py）。

覆盖 PRD 单测点：
  test_expand_dedup_by_ref            幂等：同层重复展开无重复节点/边
  test_row_to_source_file_metadata    row → file：文件元数据来自 ingest 登记；
                                      未登记 dataset 降级文本节点
  test_expand_missing_archive_row     locator 归档落空 → missing 标记不炸
  test_neighbor_readonly_no_parquet   静态契约：只依赖 obj_*/lnk_*/row_uri
  RC-104                              denied 不送明文 / system 旁路全 visible
另含：fact→object 实体/事件匹配、邻居一跳、语义层缺失降级、位置确定性。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import duckdb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext
from core.row_uri import _ensure_archive, snapshot_source_rows

from server.app import canvas_expand as ce
from server.app.canvas_seed import sys_node_id

BID = "0123456789abcdef0123456789abcdef"
PACK = "default"
DS_BANK = "银行流水"
DS_CALL = "通话记录"


def _meta(conn) -> None:
    conn.execute(
        "CREATE TABLE meta_ontology_state ("
        "pack VARCHAR, build_id VARCHAR, built_at VARCHAR, "
        "schema_version INTEGER, ontology_version VARCHAR, "
        "source_watermark VARCHAR, ontology_watermark VARCHAR, "
        "input_hashes VARCHAR, params_hash VARCHAR, is_current BOOLEAN)")
    conn.execute(
        "INSERT INTO meta_ontology_state VALUES "
        f"('{PACK}','{BID}','2026-09-13T00:00:00',2,'2.99.9',"
        "NULL,NULL,NULL,NULL,TRUE)")


def _conn_with_semantic() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    _ensure_archive(conn)
    _meta(conn)
    # ---- obj 表（列名/语义与 default pack 一致的最小子集）----
    conn.execute(
        "CREATE TABLE obj_person (person_id VARCHAR, raw_name VARCHAR, "
        "source_rows VARCHAR)")
    conn.execute(
        "CREATE TABLE obj_account (account_id VARCHAR, account_name VARCHAR, "
        "source_rows VARCHAR)")
    conn.execute(
        "CREATE TABLE obj_transaction (txn_id VARCHAR, amount_label VARCHAR, "
        "source_rows VARCHAR)")
    conn.execute(
        "CREATE TABLE obj_call (call_id VARCHAR, call_label VARCHAR, "
        "source_rows VARCHAR)")
    conn.execute(
        "CREATE TABLE lnk_calls_to (call_id VARCHAR, from_person VARCHAR, "
        "to_person VARCHAR)")
    return conn


# ---- 精简声明（与 build_index(load_pack()) 产物同形）----
PERSON = ce.ObjType("person", "人员", "person_id", "raw_name", "entity",
                    frozenset({"raw_name", "id_card"}))
ACCOUNT = ce.ObjType("account", "账户", "account_id", "account_name",
                     "entity", frozenset({"account_name"}))
TXN = ce.ObjType("transaction", "交易", "txn_id", "amount_label", "event",
                 frozenset({"from_raw", "to_raw", "amount", "currency",
                            "date"}))
CALL = ce.ObjType("call", "通话", "call_id", "call_label", "event",
                  frozenset({"caller_raw", "callee_raw", "date", "times"}))
LNK_CALLS = ce.LnkType("calls_to", "通话", "person", "person",
                       "from_person", "to_person")

CLUE_ROW_URI = f"{DS_BANK}@local#row/aaaaaaaaaaaaaaa1"
FACT_ID = "fact:f1"
ROW_NODE_ID = sys_node_id("source_row", CLUE_ROW_URI)


def _doc_with_fact_row() -> dict:
    eid = f"e:{FACT_ID}--来源行--{ROW_NODE_ID}"
    return {
        "nodes": [
            {"id": FACT_ID, "kind": "fact", "ref": "f1", "label": "事实1",
             "system": True, "pinned": False, "x": 260, "y": 0},
            {"id": ROW_NODE_ID, "kind": "source_row", "ref": CLUE_ROW_URI,
             "label": DS_BANK, "system": True, "pinned": False,
             "x": 720, "y": 0,
             "props": {"row_uri": CLUE_ROW_URI, "source": DS_BANK,
                       "granularity": "", "registered": True,
                       "archived": True, "missing": False}},
        ],
        "edges": [
            {"id": eid, "source": FACT_ID, "target": ROW_NODE_ID,
             "rel": "来源行", "system": True},
        ],
    }


def _clue_rows() -> dict:
    # 生产契约：行集经 run_rules / stamp_row_datasets 按
    # Function.inputs → bindings.source_table 打「数据源」图章；
    # dataset_of_row 不做字段名推断（新增数据源只改声明）。
    return {CLUE_ROW_URI: {
        "数据源": DS_BANK,
        "from_raw": "张卫国", "to_raw": "海州建材有限公司",
        "amount": "50000", "currency": "CNY", "date": "2020-05-01"}}


def _sources() -> dict:
    return {DS_BANK: {
        "upload_id": "up-1", "filename": "银行流水.xlsx", "fmt": "xlsx",
        "rows": 128, "status": "imported", "table_name": DS_BANK,
        "mapping_json": "{}", "created_by": "李侦查员",
        "created_at": "2026-09-10T09:30:00"}}


class ExpandFactTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn_with_semantic()
        snapshot_source_rows(self.conn, BID, [
            ("person", DS_BANK, ["raw_name"], [["张卫国"]]),
        ])
        self.conn.execute(
            "INSERT INTO obj_person VALUES "
            "('person_p1','张卫国','[\"银行流水:raw_name=张卫国\"]'),"
            "('person_p2','李四','[\"银行流水:raw_name=李四\"]')")

    def tearDown(self):
        self.conn.close()

    def _expand(self, **kw):
        objs = {o.name: o for o in (PERSON, ACCOUNT, TXN, CALL)}
        defaults = dict(
            conn=self.conn, objects=objs, links=[LNK_CALLS],
            doc=_doc_with_fact_row(), node_id=FACT_ID,
            clue_rows=_clue_rows(), registered_sources=_sources(),
            access=None, pack_id=PACK, base_dir=ROOT,
            build_id=BID, direction="all")
        defaults.update(kw)
        return ce.expand_layer(**defaults)

    def test_fact_expands_to_matching_entity_only(self):
        r = self._expand()
        obj_ids = [n["id"] for n in r["nodes"] if n["kind"] == "object"]
        self.assertIn(sys_node_id("object", "person:person_p1"), obj_ids)
        self.assertNotIn(sys_node_id("object", "person:person_p2"), obj_ids)
        # fact-[涉及]->object 边
        rels = {(e["source"], e["rel"], e["target"]) for e in r["edges"]}
        self.assertIn(
            (FACT_ID, "涉及",
             sys_node_id("object", "person:person_p1")), rels)
        # 新节点全部系统节点、带列坐标
        n = next(n for n in r["nodes"] if n["kind"] == "object")
        self.assertTrue(n["system"])
        self.assertEqual(n["x"], 480)
        self.assertEqual(n["props"]["type"], "person")

    def test_expand_dedup_by_ref(self):
        """AC-103-2：第二次展开（doc 已含首展节点）不产生重复节点/边。"""
        r1 = self._expand()
        doc = _doc_with_fact_row()
        added_n, added_e = ce.merge_expansion(doc, r1["nodes"], r1["edges"])
        self.assertTrue(added_n and added_e)
        r2 = self._expand(doc=doc)
        added2_n, added2_e = ce.merge_expansion(
            doc, r2["nodes"], r2["edges"])
        self.assertEqual(added2_n, [])
        self.assertEqual(added2_e, [])
        # 图中对象节点仍唯一
        ids = [n["id"] for n in doc["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_merge_is_idempotent_and_positioned(self):
        r = self._expand()
        doc = _doc_with_fact_row()
        ce.merge_expansion(doc, r["nodes"], r["edges"])
        ce.merge_expansion(doc, r["nodes"], r["edges"])
        self.assertEqual(
            len(doc["nodes"]), len({n["id"] for n in doc["nodes"]}))
        for n in r["nodes"]:
            self.assertIn("x", n)
            self.assertIn("y", n)


class ExpandObjectRowsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn_with_semantic()

    def tearDown(self):
        self.conn.close()

    def _objs(self):
        return {o.name: o for o in (PERSON, ACCOUNT, TXN, CALL)}

    def test_object_row_claims_clue_seed_row(self):
        """对象 locator 命中本线索种子行：认领既有伪 URI 节点（不新建归档行）。"""
        self.conn.execute(
            "INSERT INTO obj_person VALUES "
            "('person_p1','张卫国','[\"银行流水:raw_name=张卫国\"]')")
        obj_id = sys_node_id("object", "person:person_p1")
        doc = _doc_with_fact_row()
        doc["nodes"].append({
            "id": obj_id, "kind": "object", "ref": "person:person_p1",
            "label": "张卫国", "system": True, "pinned": False,
            "x": 480, "y": 0,
            "props": {"type": "person", "pk": "person_p1"}})
        r = ce.expand_layer(
            conn=self.conn, objects=self._objs(), links=[], doc=doc,
            node_id=obj_id, clue_rows=_clue_rows(),
            registered_sources=_sources(), access=None, pack_id=PACK,
            base_dir=ROOT, build_id=BID, direction="source")
        # 无新行节点（种子行已在图），但有 object→seed_row 边 + 字段详情
        self.assertEqual([n for n in r["nodes"]], [])
        rels = {(e["source"], e["rel"], e["target"]) for e in r["edges"]}
        self.assertIn((obj_id, "来源行", ROW_NODE_ID), rels)
        detail = r["details"][ROW_NODE_ID]
        self.assertTrue(detail["archived"])
        self.assertTrue(any(f["raw"] == "from_raw"
                            and f["value"] == "张卫国"
                            for f in detail["fields"]))

    def test_object_row_resolves_archive_and_denied_no_plaintext(self):
        """事件 locator 归档命中 → 真实 URI 新节点；RC-104 denied 不送明文。"""
        snapshot_source_rows(self.conn, BID, [(
            "transaction", DS_BANK,
            ["from_raw", "to_raw", "amount", "currency", "date"],
            [["张卫国", "海州建材有限公司", "50000", "CNY", "2020-05-01"]])])
        locator = ("银行流水:from_raw=张卫国,to_raw=海州建材有限公司,"
                   "amount=50000,currency=CNY,date=2020-05-01")
        self.conn.execute(
            "INSERT INTO obj_transaction VALUES ('txn_1','50000', ?)",
            [f'["{locator}"]'])
        obj_id = sys_node_id("object", "transaction:txn_1")
        doc = {"nodes": [{
            "id": obj_id, "kind": "object", "ref": "transaction:txn_1",
            "label": "50000", "system": True, "pinned": False,
            "x": 480, "y": 0,
            "props": {"type": "transaction", "pk": "txn_1"}}],
            "edges": []}

        denied_access = AccessContext(operator="见习甲", role="见习",
                                      clearance=0)
        with mock.patch(
                "server.app.source_row_dto.PolicyEngine.property_rule",
                return_value={"mask": "full"}), mock.patch(
                "server.app.source_row_dto.PolicyEngine.can_read_property",
                return_value=False):
            r = ce.expand_layer(
                conn=self.conn, objects=self._objs(), links=[], doc=doc,
                node_id=obj_id, clue_rows={},
                registered_sources=_sources(), access=denied_access,
                pack_id=PACK, base_dir=ROOT, build_id=BID,
                direction="source")
        row_nodes = [n for n in r["nodes"] if n["kind"] == "source_row"]
        self.assertEqual(len(row_nodes), 1)
        rn = row_nodes[0]
        self.assertTrue(rn["props"]["archived"])
        self.assertIn(f"{BID}#", rn["ref"])
        detail = r["details"][rn["id"]]
        # denied：每个字段 policy=denied 且 value 一律空串（明文不外送）
        self.assertTrue(detail["fields"])
        for f in detail["fields"]:
            self.assertEqual(f["policy"], "denied")
            self.assertEqual(f["value"], "")

    def test_expand_missing_archive_row(self):
        """locator 在归档中无任何匹配（多对象投影/类型差异）→ missing 标记。"""
        # 通话记录只归档了 person 投影（raw_name），obj_call locator 是全列
        snapshot_source_rows(self.conn, BID, [(
            "person", DS_CALL, ["raw_name"], [["张卫国"]])])
        locator = ("通话记录:caller_raw=  张卫国  ,callee_raw=李志强,"
                   "date=2020-08-01,times=2")
        self.conn.execute(
            "INSERT INTO obj_call VALUES ('call_1','张卫国→李志强', ?)",
            [f'["{locator}"]'])
        obj_id = sys_node_id("object", "call:call_1")
        doc = {"nodes": [{
            "id": obj_id, "kind": "object", "ref": "call:call_1",
            "label": "通话1", "system": True, "pinned": False,
            "x": 480, "y": 0,
            "props": {"type": "call", "pk": "call_1"}}],
            "edges": []}
        r = ce.expand_layer(
            conn=self.conn, objects=self._objs(), links=[], doc=doc,
            node_id=obj_id, clue_rows={}, registered_sources={},
            access=None, pack_id=PACK, base_dir=ROOT,
            build_id=BID, direction="source")
        row_nodes = [n for n in r["nodes"] if n["kind"] == "source_row"]
        self.assertEqual(len(row_nodes), 1)
        rn = row_nodes[0]
        self.assertTrue(rn["props"]["missing"])
        self.assertFalse(rn["props"]["archived"])
        detail = r["details"][rn["id"]]
        self.assertTrue(detail["missing"])
        # locator 字段仍回传（system 旁路可见），供抽屉展示尝试的 URI
        self.assertIn("caller_raw", {f["raw"] for f in detail["fields"]})

    def test_system_access_sees_all_fields(self):
        """access=None（system 旁路）：归档行字段全部 visible。"""
        snapshot_source_rows(self.conn, BID, [(
            "transaction", DS_BANK,
            ["from_raw", "to_raw", "amount"],
            [["张卫国", "李四", "50000"]])])
        self.conn.execute(
            "INSERT INTO obj_transaction VALUES "
            "('txn_9','50000',"
            "'[\"银行流水:from_raw=张卫国,to_raw=李四,amount=50000\"]')")
        obj_id = sys_node_id("object", "transaction:txn_9")
        doc = {"nodes": [{"id": obj_id, "kind": "object",
                          "ref": "transaction:txn_9", "label": "t",
                          "system": True, "pinned": False, "x": 480,
                          "y": 0, "props": {"type": "transaction",
                                            "pk": "txn_9"}}],
               "edges": []}
        r = ce.expand_layer(
            conn=self.conn, objects=self._objs(), links=[], doc=doc,
            node_id=obj_id, clue_rows={}, registered_sources={},
            access=None, pack_id=PACK, base_dir=ROOT,
            build_id=BID, direction="source")
        rn = next(n for n in r["nodes"] if n["kind"] == "source_row")
        detail = r["details"][rn["id"]]
        self.assertTrue(all(f["policy"] == "visible"
                            for f in detail["fields"]))
        self.assertIn("张卫国", {f["value"] for f in detail["fields"]})


class ExpandNeighborsTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn_with_semantic()
        self.conn.execute(
            "INSERT INTO obj_person VALUES "
            "('person_p1','张卫国','[\"银行流水:raw_name=张卫国\"]'),"
            "('person_p2','李志强','[\"银行流水:raw_name=李志强\"]')")
        self.conn.execute(
            "INSERT INTO lnk_calls_to VALUES "
            "('call_1','person_p1','person_p2')")

    def tearDown(self):
        self.conn.close()

    def test_one_hop_neighbor_with_rel_title(self):
        obj_id = sys_node_id("object", "person:person_p1")
        doc = {"nodes": [{"id": obj_id, "kind": "object",
                          "ref": "person:person_p1", "label": "张卫国",
                          "system": True, "pinned": False, "x": 480,
                          "y": 0, "props": {"type": "person",
                                            "pk": "person_p1"}}],
               "edges": []}
        r = ce.expand_layer(
            conn=self.conn,
            objects={o.name: o for o in (PERSON, ACCOUNT, TXN, CALL)},
            links=[LNK_CALLS], doc=doc, node_id=obj_id,
            clue_rows={}, registered_sources={}, access=None,
            pack_id=PACK, base_dir=ROOT, build_id=BID,
            direction="neighbors")
        nbr = sys_node_id("object", "person:person_p2")
        self.assertIn(nbr, [n["id"] for n in r["nodes"]])
        edge = next(e for e in r["edges"]
                    if e["source"] == obj_id and e["target"] == nbr)
        self.assertEqual(edge["rel"], "通话")  # links.title，非技术表名
        self.assertTrue(edge["system"])
        # 邻居节点标签来自 name_property
        self.assertEqual(
            next(n for n in r["nodes"] if n["id"] == nbr)["label"],
            "李志强")

    def test_neighbor_readonly_no_parquet(self):
        """静态契约：expand 只读模块不允许出现 Parquet/自由 SQL 直读。"""
        src = (ROOT / "server" / "app" / "canvas_expand.py").read_text(
            encoding="utf-8")
        forbidden = ["read_parquet", "parquet", "openpyxl", "pyarrow",
                     "glob(", "pd.read", "duckdb.connect"]
        for token in forbidden:
            self.assertNotIn(token, src,
                             f"expand 服务出现禁止依赖：{token}")
        # 只允许 obj_/lnk_/row_archive/row_build_index/information_schema
        for allowed in ("obj_", "lnk_", "row_archive", "row_build_index"):
            self.assertIn(allowed, src)


class ExpandRowToFileTest(unittest.TestCase):
    def setUp(self):
        self.conn = _conn_with_semantic()

    def tearDown(self):
        self.conn.close()

    def _row_doc(self, uri: str, dataset: str) -> dict:
        nid = sys_node_id("source_row", uri)
        return {"nodes": [{"id": nid, "kind": "source_row", "ref": uri,
                           "label": dataset, "system": True,
                           "pinned": False, "x": 720, "y": 0,
                           "props": {"row_uri": uri, "source": dataset,
                                     "granularity": "", "registered": True,
                                     "archived": True, "missing": False}}],
                "edges": []}, nid

    def test_row_to_source_file_metadata(self):
        doc, nid = self._row_doc(CLUE_ROW_URI, DS_BANK)
        r = ce.expand_layer(
            conn=self.conn,
            objects={o.name: o for o in (PERSON, TXN)}, links=[], doc=doc,
            node_id=nid, clue_rows=_clue_rows(),
            registered_sources=_sources(), access=None, pack_id=PACK,
            base_dir=ROOT, build_id=BID, direction="source")
        files = [n for n in r["nodes"] if n["kind"] == "source_file"]
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertEqual(f["ref"], "up-1")
        self.assertEqual(f["label"], "银行流水.xlsx")
        self.assertTrue(f["props"]["registered"])
        card = r["details"][f["id"]]["file"]
        self.assertEqual(card["rows"], 128)
        self.assertEqual(card["uploaded_by"], "李侦查员")
        self.assertTrue(any(e["rel"] == "所属文件" and e["source"] == nid
                            for e in r["edges"]))

    def test_unregistered_dataset_degrades_to_text_node(self):
        uri = "轨迹出行@local#row/bbbbbbbbbbbbbbbb"
        doc, nid = self._row_doc(uri, "轨迹出行")
        r = ce.expand_layer(
            conn=self.conn, objects={}, links=[], doc=doc, node_id=nid,
            clue_rows={}, registered_sources={}, access=None,
            pack_id=PACK, base_dir=ROOT, build_id=BID, direction="source")
        f = next(n for n in r["nodes"] if n["kind"] == "source_file")
        self.assertFalse(f["props"]["registered"])
        self.assertIn("未登记数据源", f["label"])
        self.assertFalse(r["details"][f["id"]]["registered"])

    def test_file_node_is_leaf(self):
        fid = sys_node_id("source_file", "up-1")
        doc = {"nodes": [{"id": fid, "kind": "source_file", "ref": "up-1",
                          "label": "银行流水.xlsx", "system": True,
                          "pinned": False, "x": 960, "y": 0,
                          "props": {"registered": True}}], "edges": []}
        r = ce.expand_layer(
            conn=self.conn, objects={}, links=[], doc=doc, node_id=fid,
            clue_rows={}, registered_sources=_sources(), access=None,
            pack_id=PACK, base_dir=ROOT, build_id=BID)
        self.assertTrue(r["leaf"])
        self.assertIn("leaf", r["notices"])
        self.assertEqual(r["nodes"], [])


class SemanticUnavailableTest(unittest.TestCase):
    def test_fact_without_semantic_layer_notice(self):
        r = ce.expand_layer(
            conn=None,
            objects={PERSON.name: PERSON}, links=[],
            doc=_doc_with_fact_row(), node_id=FACT_ID,
            clue_rows=_clue_rows(), registered_sources=_sources(),
            access=None, pack_id=PACK, base_dir=ROOT, build_id=None)
        self.assertIn("semantic_unavailable", r["notices"])
        self.assertEqual(r["nodes"], [])

    def test_unknown_node_not_found_notice(self):
        r = ce.expand_layer(
            conn=None, objects={}, links=[],
            doc=_doc_with_fact_row(), node_id="fact:ghost",
            clue_rows={}, registered_sources={}, access=None,
            pack_id=PACK, base_dir=ROOT, build_id=None)
        self.assertIn("node_not_found", r["notices"])


class LocatorParseTest(unittest.TestCase):
    def test_comma_in_value_rejoined_by_known_cols(self):
        ds, pairs = ce.parse_locator(
            "举报材料:content_raw=受贿,细节如下,reporter_raw=王某",
            known_cols={"content_raw", "reporter_raw"})
        self.assertEqual(ds, "举报材料")
        self.assertEqual(pairs, [
            ("content_raw", "受贿,细节如下"),
            ("reporter_raw", "王某"),
        ])


if __name__ == "__main__":
    unittest.main()
