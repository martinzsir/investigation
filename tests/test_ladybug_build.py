"""
tests/test_ladybug_build.py
阶段 0：通用语义层 → LadybugDB 全量建图（声明驱动）。

两层覆盖：
  A. 声明层（无需 ladybug，任何环境可跑）：
     - edge_decls 从 links.json endpoints 推导（owns 方向纠偏 / time_window
       无行键合成 / runtime 边按主键列合成 / 边属性 Kùzu 类型映射）；
     - node_sql / edge_sql 形态；
     - loader _select_output_columns 不被 CAST(x AS DATE) 干扰。
  B. 建图层（未装 ladybug 自动 skip）：
     - REL 表行数 == lnk_* 行数；节点覆盖端点对象；
     - 重建幂等；manifest 陈旧判定（行数对账 / 缺图 / 缺 manifest）；
     - ladybug 不可用 → skipped 不抛。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                            # noqa: E402
from core.ladybug_builder import (                                # noqa: E402
    MANIFEST_SUFFIX, build_case_graph, is_graph_current,
    load_manifest,
)
from core.ontology import build_ontology                          # noqa: E402
from core.ontology_loader import (                               # noqa: E402
    _select_output_columns, load_pack,
)
from core.semantic_graph_export import (                         # noqa: E402
    edge_decls, edge_sql, graph_object_types, node_sql,
)

try:
    import ladybug  # noqa: F401
    HAS_LADYBUG = True
except ImportError:
    HAS_LADYBUG = False

skip_no_graph = unittest.skipUnless(HAS_LADYBUG, "未安装 ladybug（可选依赖）")


def make_semantic_store() -> Store:
    """最小银行流水样本 + 语义层编译（缺源表的对象/链接编译期自动跳过）。"""
    s = Store(db_path=":memory:")
    s.execute(
        "CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("""
        INSERT INTO 银行流水 VALUES
        ('宏业建设', 'A建材', 4600000, '2021-10-01'),
        ('A建材', '张卫国配偶', 1700000, '2021-11-15'),
        ('张卫国', '现金存入', 100000, '2019-06-25')
    """)
    build_ontology(s.conn)
    return s


# ----------------------------------------------------------------------
# A. 声明层
# ----------------------------------------------------------------------
class EdgeDeclTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = load_pack("default")
        cls.decls = {d.link: d for d in edge_decls(cls.pack)}

    def test_compiletime_links_with_refs_present(self):
        for name in ("transfers", "calls_to", "owns", "involved_in",
                     "co_located", "time_window", "tipoff_targets_person",
                     "tipoff_from_reporter", "osint_mentions",
                     "trackpoint_at", "holds"):
            self.assertIn(name, self.decls, f"{name} 应生成 EdgeDecl")
            d = self.decls[name]
            self.assertFalse(d.runtime)
            self.assertTrue(d.src_col and d.dst_col)
            self.assertEqual(d.table, f"lnk_{name}")
            self.assertEqual(d.rel_table, name.upper())

    def test_owns_direction_account_to_person(self):
        # 纠偏后：账户 → 持有自然人（owner_raw 降为 extra，不再是端点）
        d = self.decls["owns"]
        self.assertEqual(d.src_obj, "account")
        self.assertEqual(d.dst_obj, "person")
        self.assertEqual(d.src_col, "account_id")
        self.assertEqual(d.dst_col, "owner_person")
        self.assertEqual(d.edge_key, "account_id")
        self.assertIn("owner_raw", d.attr_cols)

    def test_time_window_synthesized_edge_key_and_types(self):
        d = self.decls["time_window"]
        self.assertEqual(d.src_obj, "bid_project")
        self.assertEqual(d.dst_obj, "transaction")
        self.assertIsNone(d.edge_key)            # 多行无单行键 → src#dst 合成
        self.assertEqual(d.attr_types["amount"], "DOUBLE")
        self.assertEqual(d.attr_types["offset_days"], "INT64")
        sql = edge_sql(d)
        self.assertIn("CONCAT_WS('#',", sql)          # 合成 edge_pk
        self.assertIn('"project_id" AS src_pk', sql)
        self.assertIn('"txn_id" AS dst_pk', sql)

    def test_transfers_attr_mapping(self):
        d = self.decls["transfers"]
        self.assertEqual(d.edge_key, "txn_id")
        self.assertEqual(d.attr_types["amount"], "DOUBLE")
        self.assertEqual(d.attr_types["date"], "DATE")
        # extra 列（from_account/to_account 原始名）透传为 STRING
        self.assertEqual(d.attr_types["from_account"], "STRING")
        self.assertEqual(d.attr_types["to_account"], "STRING")

    def test_runtime_links_synthesized_from_object_keys(self):
        d = self.decls["decision_for"]
        self.assertTrue(d.runtime)
        self.assertEqual(d.src_obj, "decision")
        self.assertEqual(d.dst_obj, "clue")
        self.assertEqual(d.src_col, "decision_id")
        self.assertEqual(d.dst_col, "clue_id")
        self.assertIsNone(d.edge_key)
        self.assertEqual(d.attr_cols, ())
        self.assertEqual(self.decls["image_for_org"].dst_col, "org_id")

    def test_graph_object_types_covers_endpoints(self):
        types = set(graph_object_types(self.pack))
        # 资金/通话/项目/事件/地点/物品/举报/舆情 端点对象都应被引用到
        for t in ("account", "person", "org", "bid_project", "transaction",
                  "trackpoint", "location", "item", "tipoff",
                  "osint_article", "decision", "clue", "image_evidence"):
            self.assertIn(t, types, f"节点对象类型缺 {t}")

    def test_node_and_edge_sql_shape(self):
        d = self.decls["transfers"]
        ns = node_sql("account", self.pack)
        self.assertIn("AS pk", ns)
        self.assertIn("'account' AS type", ns)
        self.assertIn("AS name", ns)
        es = edge_sql(d)
        self.assertIn("AS src_pk", es)
        self.assertIn("AS dst_pk", es)
        self.assertIn('"txn_id" AS edge_pk', es)
        self.assertIn("from_account_id\" IS NOT NULL", es)


class OutputColumnParseTests(unittest.TestCase):
    def test_cast_as_date_not_mistaken_as_alias(self):
        cols = _select_output_columns(
            "SELECT a, CAST(b AS DATE) AS d, TRY_CAST(c AS INTEGER) AS cc "
            "FROM lnk_x l WHERE TRUE")
        self.assertEqual(cols, {"a", "d", "cc"})

    def test_nested_comma_function_output(self):
        cols = _select_output_columns(
            "SELECT CONCAT_WS('#', x, y) AS edge_pk, z FROM lnk_x")
        self.assertEqual(cols, {"edge_pk", "z"})

    def test_bare_column_and_table_qualified(self):
        cols = _select_output_columns(
            "SELECT l.person_1, l.person_2 AS p2 FROM lnk_co_located l")
        self.assertEqual(cols, {"person_1", "p2"})

    def test_non_select_returns_empty(self):
        self.assertEqual(_select_output_columns("DROP TABLE x"), set())


# ----------------------------------------------------------------------
# B. 建图层
# ----------------------------------------------------------------------
@skip_no_graph
class BuildGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lbug_build_test_"))
        self.graph_path = self.tmp / "case.lbug"
        self.pack = load_pack("default")
        self.store = make_semantic_store()

    def tearDown(self):
        self.store.close()

    def _rel_count(self, conn, rel: str) -> int:
        res = conn.execute(f"MATCH ()-[r:{rel}]->() RETURN COUNT(r) AS c")
        return int(res.get_next()[0])

    def _node_rows(self, conn):
        res = conn.execute("MATCH (n:Entity) RETURN n.pk, n.type, n.name")
        rows = []
        while res.has_next():
            rows.append(res.get_next())
        return rows

    def test_build_counts_match_semantic_tables(self):
        r = build_case_graph(self.store, self.pack, self.graph_path)
        self.assertFalse(r.get("skipped"), msg=f"建图不应跳过：{r.get('reason')}")
        n_txn_edges = int(self.store.query(
            "SELECT COUNT(*) AS n FROM lnk_transfers")[0]["n"])
        # 账户 5（主体∪对方去重）+ 流水事件 3；其余端点对象无源表不入图
        self.assertEqual(r["nodes"], 8)
        self.assertEqual(r["edges"], n_txn_edges)
        self.assertEqual(r["links"]["transfers"], n_txn_edges)

        import ladybug as lb
        db = lb.Database(str(self.graph_path))
        conn = lb.Connection(db)
        self.assertEqual(self._rel_count(conn, "TRANSFERS"), n_txn_edges)
        nodes = self._node_rows(conn)
        acct = [x for x in nodes if x[1] == "account"]
        self.assertEqual(len(acct), 5)
        self.assertEqual({x[2] for x in acct},
                         {"宏业建设", "A建材", "张卫国配偶", "张卫国", "现金存入"})
        self.assertTrue(all(x[0] for x in nodes))       # pk 非空
        # 边属性随 REL 可查（amount/date）
        res = conn.execute(
            "MATCH ()-[r:TRANSFERS]->() RETURN r.edge_pk, r.`amount`, "
            "r.`date` ORDER BY r.`amount` DESC LIMIT 1")
        row = res.get_next()
        self.assertTrue(row[0])
        self.assertAlmostEqual(float(row[1]), 4600000.0, places=2)
        try:
            conn.close()
        except Exception:
            pass

    def test_rebuild_idempotent_and_manifest_current(self):
        r1 = build_case_graph(self.store, self.pack, self.graph_path)
        r2 = build_case_graph(self.store, self.pack, self.graph_path)
        self.assertFalse(r1.get("skipped"))
        self.assertEqual((r1["nodes"], r1["edges"]),
                         (r2["nodes"], r2["edges"]))
        manifest = load_manifest(self.graph_path)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["table_counts"]["lnk_transfers"],
                         r2["links"]["transfers"])
        ok, reason = is_graph_current(self.store, self.pack, self.graph_path)
        self.assertTrue(ok, msg=f"重建后应判新鲜：{reason}")

    def test_stale_after_semantic_row_change(self):
        build_case_graph(self.store, self.pack, self.graph_path)
        # runtime 边表插入一行（决策副作用场景）→ manifest 行数清单应对账不符
        self.store.execute(
            "INSERT INTO lnk_decision_for VALUES ('decision_x', 'clue_y')")
        ok, reason = is_graph_current(self.store, self.pack, self.graph_path)
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("table_counts_mismatch"), msg=reason)

    def test_missing_graph_and_manifest(self):
        build_case_graph(self.store, self.pack, self.graph_path)
        missing = self.tmp / "nope.lbug"
        ok, reason = is_graph_current(self.store, self.pack, missing)
        self.assertFalse(ok)
        self.assertEqual(reason, "graph_missing")
        manifest_p = self.graph_path.with_name(
            self.graph_path.name + MANIFEST_SUFFIX)
        manifest_p.unlink()
        ok, reason = is_graph_current(self.store, self.pack, self.graph_path)
        self.assertFalse(ok)
        self.assertEqual(reason, "manifest_missing")


class UnavailableTests(unittest.TestCase):
    def test_skipped_when_ladybug_missing(self):
        store = make_semantic_store()
        try:
            with tempfile.TemporaryDirectory() as td:
                target = Path(td) / "x.lbug"
                with mock.patch(
                        "core.ladybug_builder.ladybug_available", lambda: False):
                    r = build_case_graph(store, load_pack("default"), target)
                self.assertTrue(r["skipped"])
                self.assertIn("ladybug", r["reason"])
                self.assertFalse(target.exists())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
