"""
tests/test_graph.py
L4 图库层测试（Q2 过桥 Cypher 化 + 双轨一致性）。

覆盖：
  1. 建图正确性（节点数 / 边数）
  2. Cypher 两跳过桥能找出预期路径
  3. SQL 自连接对照结果一致
  4. 一致性比对器：一致 / 不一致两种情形
  5. 变长跳邻域
  6. 降级：ladybug 不可用时，SQL 轨仍可用且不崩溃

注：ladybug 为可选依赖，未安装时图库相关用例自动 skip（不判失败）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                          # noqa: E402
from core.graph import (                                        # noqa: E402
    GraphBackend, OverpassPath, overpass_two_hop_sql, compare_engines,
)

try:
    import ladybug  # noqa: F401
    HAS_LADYBUG = True
except ImportError:
    HAS_LADYBUG = False

skip_no_graph = unittest.skipUnless(HAS_LADYBUG, "未安装 ladybug（可选依赖）")


def make_store() -> Store:
    """
    内存库 + 最小过桥样本，避免依赖外部 parquet。

    注意：Store 签名是 (root, db_path)，内存库必须写成 db_path=":memory:"；
    写成 Store(":memory:") 会落到 root 参数上，实际仍打开同名的文件库 → 表冲突。
    """
    s = Store(db_path=":memory:")
    s.execute("""
        CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)
    """)
    s.execute("""
        INSERT INTO 银行流水 VALUES
        ('宏业建设', 'A建材', 4600000, '2021-10-01'),
        ('A建材', '张卫国配偶', 1700000, '2021-11-15'),
        ('张卫国', '现金存入', 100000, '2019-06-25')
    """)
    # 通用图只消费 obj_*/lnk_* 语义表（不再从裸流表直建）：缺源表的对象/链接
    # 由编译器按 optional/编译失败跳过，最小样本仍产出 obj_account + lnk_transfers。
    from core.ontology import build_ontology
    build_ontology(s.conn)
    return s


class TestSQLTrack(unittest.TestCase):
    """SQL 轨不依赖图库，必须始终可用。"""

    def test_sql_finds_overpass(self):
        s = make_store()
        paths = overpass_two_hop_sql(s)
        keys = {p.key() for p in paths}
        self.assertIn(("宏业建设", "A建材", "张卫国配偶"), keys,
                      msg=f"SQL 应找出过桥链，实得={keys}")

    def test_sql_excludes_self_loop(self):
        s = make_store()
        paths = overpass_two_hop_sql(s)
        for p in paths:
            self.assertEqual(len({p.source, p.bridge, p.dest}), 3,
                             msg=f"路径三节点应互不相同：{p.key()}")

    def test_single_hop_not_counted(self):
        """张卫国→现金存入 只有一跳，不应被判为过桥。"""
        s = make_store()
        paths = overpass_two_hop_sql(s)
        for p in paths:
            self.assertNotIn("现金存入", (p.bridge, p.dest),
                             msg="单跳链不应出现在两跳过桥结果中")


class TestCompare(unittest.TestCase):
    def test_consistent_when_identical(self):
        a = [OverpassPath("X", "M", "Y", 100, 80, "cypher")]
        b = [OverpassPath("X", "M", "Y", 100, 80, "sql")]
        r = compare_engines(a, b)
        self.assertTrue(r["consistent"])
        self.assertEqual(r["matched"], 1)

    def test_inconsistent_detected(self):
        a = [OverpassPath("X", "M", "Y", 100, 80, "cypher")]
        b = [OverpassPath("X", "M", "Z", 100, 80, "sql")]
        r = compare_engines(a, b)
        self.assertFalse(r["consistent"])
        self.assertEqual(r["only_in_cypher"], [["X", "M", "Y"]])
        self.assertEqual(r["only_in_sql"], [["X", "M", "Z"]])

    def test_amount_mismatch_flagged(self):
        """金额不同但路径相同 → 比对只按路径键判断一致，金额差异在 detail 中体现。"""
        a = [OverpassPath("X", "M", "Y", 100, 80, "cypher")]
        b = [OverpassPath("X", "M", "Y", 999, 80, "sql")]
        r = compare_engines(a, b)
        self.assertTrue(r["consistent"])
        self.assertEqual(r["detail"][0]["cypher_amount"], [100, 80])
        self.assertEqual(r["detail"][0]["sql_amount"], [999, 80])


@skip_no_graph
class TestGraphTrack(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="lbug_test_"))
        self.g = GraphBackend(str(self.tmp / "t.lbug"))
        self.store = make_store()

    def tearDown(self):
        self.g.close()
        self.store.close()

    def test_build_graph_counts(self):
        stat = self.g.build_from_duckdb(self.store)
        # 通用图节点 = 边端点引用到且已物化的对象：5 个账户（主体∪对方去重）
        # + 3 条流水事件（obj_transaction 为 time_window 端点对象）；
        # 人/组织等无源表不入图。边 = lnk_transfers 3 行。
        self.assertEqual(stat["nodes"], 8, msg=f"实得{stat}")
        self.assertEqual(stat["edges"], 3)

    def test_cypher_overpass(self):
        self.g.build_from_duckdb(self.store)
        paths = self.g.overpass_two_hop()
        keys = {p.key() for p in paths}
        self.assertIn(("宏业建设", "A建材", "张卫国配偶"), keys,
                      msg=f"Cypher 应找出过桥链，实得={keys}")

    def test_cypher_sql_consistent(self):
        self.g.build_from_duckdb(self.store)
        c = self.g.overpass_two_hop()
        s = overpass_two_hop_sql(self.store)
        r = compare_engines(c, s)
        self.assertTrue(r["consistent"], msg=f"双轨应一致：{r}")

    def test_neighbors_within_hops(self):
        self.g.build_from_duckdb(self.store)
        nb = self.g.neighbors_within("宏业建设", max_hops=2)
        self.assertIn("A建材", nb)
        self.assertIn("张卫国配偶", nb)


class TestDegradation(unittest.TestCase):
    """图库不可用时的降级行为。"""

    def test_backend_flags_unavailable(self):
        g = GraphBackend("/tmp/_never_used.lbug")
        # available 由 ladybug 是否可导入决定；此处只断言不抛异常
        self.assertIsInstance(g.available, bool)

    def test_sql_still_works_without_graph(self):
        """即使图库缺失，SQL 轨仍能独立产出结果。"""
        s = make_store()
        paths = overpass_two_hop_sql(s)
        self.assertGreaterEqual(len(paths), 1)
        s.close()


def _edge_sig_set(first_path: dict) -> set:
    """BFS 首达链 → 物理边签名集合 (edge, edge_pk)。"""
    sigs = set()
    for chain in first_path.values():
        for e in chain:
            sigs.add((e.edge, e.edge_pk))
    return sigs


def _path_sig_set(chains: list) -> set:
    """简单路径 → (节点 pk 序列, [(edge, edge_pk)…]) 签名集合。"""
    out = set()
    for chain in chains:
        nodes = tuple([chain[0].src] + [e.dst for e in chain])
        edges = tuple((e.edge, e.edge_pk) for e in chain)
        out.add((nodes, edges))
    return out


@skip_no_graph
class TestGatewayDualTrack(unittest.TestCase):
    """阶段 1：GraphGateway Cypher 主轨 vs 语义轨强制对拍。

    夹具为无平行边、唯一前驱的确定拓扑（tests.test_relation_fixtures），
    故不仅节点集，连 BFS 树边、共同邻居代表边、路径边集都逐一对拍。
    """

    def setUp(self):
        import shutil
        import tempfile
        from core.graph import GraphGateway
        from core.ladybug_builder import build_case_graph
        from core.ontology_loader import load_pack
        from tests.test_relation_functions import make_graph_build_store

        self._shutil = shutil
        self.store = make_graph_build_store()
        self.tmp = tempfile.mkdtemp(prefix="lbug_gw_")
        self.graph_path = Path(self.tmp) / "case.lbug"
        r = build_case_graph(self.store, load_pack("default"),
                             self.graph_path)
        self.assertFalse(r.get("skipped"), msg=f"建图失败：{r.get('reason')}")
        self.gw_lb = GraphGateway(self.store, graph_path=str(self.graph_path))
        # :memory: 库推导不出图路径 → 纯语义轨
        self.gw_sm = GraphGateway(self.store)

    def tearDown(self):
        self.gw_lb.close()
        self.gw_sm.close()
        self.store.close()
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def test_engine_selection(self):
        g = self.gw_lb.view("all")
        self.assertEqual(self.gw_lb.engine, "ladybug")
        self.assertEqual(set(g.loaded_links),
                         {"transfers", "calls_to", "owns",
                          "involved_in", "co_located"})
        self.assertEqual(g.gaps, [])
        d = self.gw_lb.diagnostic(g)
        self.assertEqual(d["engine"], "ladybug")
        self.assertTrue(d["graph_path"].endswith("case.lbug"))

    def test_semantic_fallback_when_no_path(self):
        g = self.gw_sm.view("all")
        self.assertEqual(self.gw_sm.engine, "semantic")
        self.assertIn("no_graph_path",
                      self.gw_sm.fallback_reason)
        self.assertEqual(len(g.loaded_links), 5)

    def test_semantic_fallback_when_missing_file(self):
        from core.graph import GraphGateway
        gw = GraphGateway(self.store,
                          graph_path=str(Path(self.tmp) / "missing.lbug"))
        g = gw.view("all")
        self.assertEqual(gw.engine, "semantic")
        self.assertTrue(gw.fallback_reason.startswith("graph_missing"))
        # 图缺失不算关系数据缺口：不产生 gaps/degraded
        self.assertEqual(g.gaps, [])
        gw.close()

    def test_semantic_fallback_when_stale(self):
        """语义层行数变化 → manifest 对账失败 → stale 回落。"""
        from core.graph import GraphGateway
        self.store.conn.execute(
            "INSERT INTO lnk_transfers "
            "(txn_id, from_account_id, to_account_id, amount, date) "
            "VALUES ('txn_999', 'account_hy', 'account_zwp', 1, "
            "'2021-12-01')")
        gw = GraphGateway(self.store, graph_path=str(self.graph_path))
        gw.view("all")
        self.assertEqual(gw.engine, "semantic")
        self.assertTrue(gw.fallback_reason.startswith("graph_stale"),
                        msg=gw.fallback_reason)
        gw.close()

    def test_neighborhood_dual_track(self):
        for start, depth in (("account_hy", 2), ("person_zhang", 2),
                             ("person_li", 3)):
            h_l, p_l = self.gw_lb.view("all").neighborhood(start, depth)
            h_s, p_s = self.gw_sm.view("all").neighborhood(start, depth)
            self.assertEqual(h_l, h_s, msg=f"hops 不一致 start={start}")
            self.assertEqual(_edge_sig_set(p_l), _edge_sig_set(p_s),
                             msg=f"BFS 树边不一致 start={start}")

    def test_common_neighbors_dual_track(self):
        g_l, g_s = self.gw_lb.view("all"), self.gw_sm.view("all")
        c_l = g_l.common_neighbors("person_zhang", "person_li")
        c_s = g_s.common_neighbors("person_zhang", "person_li")
        self.assertEqual(set(c_l), set(c_s))
        self.assertEqual(set(c_l), {"person_wang"})
        for pk, (ea, eb) in c_l.items():
            sa, sb = c_s[pk]
            self.assertEqual((ea.edge, ea.edge_pk), (sa.edge, sa.edge_pk))
            self.assertEqual((eb.edge, eb.edge_pk), (sb.edge, sb.edge_pk))
            self.assertEqual(ea.ref()["ref"], f"lnk_calls_to#{ea.edge_pk}")

    def test_paths_dual_track(self):
        g_l, g_s = self.gw_lb.view("all"), self.gw_sm.view("all")
        c_l = g_l.paths("person_zhang", "account_zwp", 3, 100)
        c_s = g_s.paths("person_zhang", "account_zwp", 3, 100)
        self.assertEqual(_path_sig_set(c_l), _path_sig_set(c_s))
        # 张三—持有反向—宏业建设—转账—A建材—转账—配偶（3 跳唯一链）
        self.assertEqual(len(c_l), 1)
        chain = c_l[0]
        self.assertEqual(
            [e.edge for e in chain], ["owns", "transfers", "transfers"])
        self.assertTrue(chain[0].reversed_traversal)  # owns 反向遍历

    def test_edge_kinds_filter_dual_track(self):
        g_l = self.gw_lb.view("fund")
        g_s = self.gw_sm.view("fund")
        self.assertEqual(g_l.loaded_links, ["transfers"])
        self.assertEqual(g_s.loaded_links, ["transfers"])
        h_l, _ = g_l.neighborhood("account_hy", 2)
        h_s, _ = g_s.neighborhood("account_hy", 2)
        self.assertEqual(h_l, h_s)
        self.assertNotIn("person_zhang", h_l)

    def test_missing_rel_table_gap_parity(self):
        """图中缺 REL 与语义层缺 lnk 表的 gaps 结构对齐。"""
        from core.graph import GraphGateway
        from core.ladybug_builder import build_case_graph
        from core.ontology_loader import load_pack
        from tests.test_relation_functions import make_graph_build_store
        # 独立 store（不删共享夹具表，避免 manifest 陈旧影响其他用例）
        store2 = make_graph_build_store()
        try:
            store2.conn.execute("DROP TABLE lnk_co_located")
            p2 = Path(self.tmp) / "case2.lbug"
            r = build_case_graph(store2, load_pack("default"), p2)
            self.assertFalse(r.get("skipped"), msg=r.get("reason"))
            gw_l = GraphGateway(store2, graph_path=str(p2))
            gw_s = GraphGateway(store2)
            gl, gs = gw_l.view("all"), gw_s.view("all")
            self.assertEqual(gw_l.engine, "ladybug")
            self.assertEqual({x["link"] for x in gl.gaps}, {"co_located"})
            self.assertEqual({x["link"] for x in gs.gaps}, {"co_located"})
            gw_l.close()
            gw_s.close()
        finally:
            store2.close()

    def test_cross_check_consistent_keeps_ladybug(self):
        from core.graph import GraphGateway
        gw = GraphGateway(self.store, graph_path=str(self.graph_path),
                          cross_check=True)
        gw.view("all")
        self.assertEqual(gw.engine, "ladybug")
        self.assertIsNone(gw.mismatch)
        gw.close()

    def test_cross_check_mismatch_falls_back_to_semantic(self):
        """图内注入语义层不存在的假边：对拍检出 engine_mismatch，
        弃图轨并以 semantic 轨为准（假边不得进结果）。"""
        import ladybug as lb
        from core.graph import GraphGateway
        # 在图副本上注入假边（ladybug 0.20 .lbug 为单文件库），不污染共享夹具
        tampered = Path(self.tmp) / "tampered.lbug"
        self._shutil.copy2(self.graph_path, tampered)
        # 连 manifest 一起复制，Gateway 开图前需行数对账
        from core.ladybug_builder import MANIFEST_SUFFIX
        mf = Path(str(self.graph_path) + MANIFEST_SUFFIX)
        if mf.exists():
            self._shutil.copy2(mf, Path(str(tampered) + MANIFEST_SUFFIX))
        wdb = lb.Database(str(tampered), read_only=False)
        wconn = lb.Connection(wdb)
        wconn.execute(
            "MATCH (a:Entity {pk: 'account_hy'}), "
            "(b:Entity {pk: 'account_zwp'}) "
            "CREATE (a)-[:TRANSFERS {edge_pk: 'txn_fake_99'}]->(b)")
        wconn.close()
        if hasattr(wdb, "close"):
            wdb.close()

        gw = GraphGateway(self.store, graph_path=str(tampered),
                          cross_check=True)
        g = gw.view("all")
        self.assertEqual(gw.engine, "semantic")
        self.assertTrue(gw.fallback_reason.startswith("engine_mismatch"),
                        msg=gw.fallback_reason)
        self.assertIn(("transfers", "txn_fake_99"),
                      gw.mismatch["ladybug_only"])
        d = gw.diagnostic(g)
        self.assertIn("engine_mismatch", d)
        # semantic 轨为准：account_hy 一跳不含 account_zwp（假边 1 跳被剔除，
        # 真实 2 跳链 account_hy→account_ajc→account_zwp 保留）
        hops, _ = g.neighborhood("account_hy", 1)
        self.assertNotIn("account_zwp", hops)
        hops2, _ = g.neighborhood("account_hy", 2)
        self.assertIn("account_zwp", hops2)
        gw.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
