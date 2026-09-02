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
        self.assertEqual(stat["nodes"], 5,
                         msg=f"节点应为主体∪对方去重：宏业建设/A建材/张卫国配偶/张卫国/现金存入，实得{stat}")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
