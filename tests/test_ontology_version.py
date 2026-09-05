"""
tests/test_ontology_version.py
REQ-001 语义层版本时钟与依赖图 测试。

覆盖 AC1-AC5：
  AC1: build 成功后 state 存在且 source_watermark == ontology_watermark
  AC2: 插入新行后 freshness() 返回 STALE + 差异清单
  AC3: STALE 时 affected_objects 非空，FRESH 时为空
  AC4: 未构建过返回 UNBUILT 而非异常
  AC5: 同输入两次构建 input_hashes 一致（确定性）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                          # noqa: E402
from core.ontology import build_ontology                        # noqa: E402
from core.ontology_version import (                             # noqa: E402
    current_version, freshness, dependency_graph,
)


def _create_tables(s: Store) -> None:
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")


def make_store() -> Store:
    s = Store(db_path=":memory:")
    _create_tables(s)
    s.execute("""
        INSERT INTO 银行流水 VALUES
        ('张卫国', '现金存入', 100000, '2021-09-28'),
        ('宏业建设', 'A建材', 4600000, '2021-10-01')
    """)
    s.execute("""
        INSERT INTO 通话记录 VALUES
        ('张卫国', '李志强', '2021-10-01', 3),
        ('张卫国', '李志强', '2021-09-30', 5)
    """)
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设', '李志强', '存续', NULL)")
    s.execute("""
        INSERT INTO 轨迹出行 VALUES
        ('2021-10-02', '张卫国', '项目B'),
        ('2021-09-30', '张卫国', '项目A'),
        ('2021-10-01', '李志强', '项目A')
    """)
    s.execute("INSERT INTO 招投标档案 VALUES ('项目A', '宏业建设', '2021-10-01', '张卫国')")
    s.execute("INSERT INTO 公开OSINT (主体, 公开信息, 发布日期, 来源) VALUES ('张卫国', '分管招投标', '2019-03-01', '政府官网')")
    s.execute("INSERT INTO 举报材料 VALUES ('2022-01-10', '经济类', '张卫国', '匿名', '反映收受宏业现金约 120 万')")
    return s


class TestOntologyVersion(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        build_ontology(self.store.conn)

    def test_ac1_state_written_and_watermarks_equal(self):
        """AC1: build 成功后 state 存在且 source==ontology"""
        ver = current_version(self.store.conn, "default")
        self.assertIsNotNone(ver)
        self.assertEqual(ver.source_watermark, ver.ontology_watermark)
        self.assertTrue(ver.build_id)
        self.assertTrue(ver.built_at)

    def test_ac2_stale_after_source_insert(self):
        """AC2: 插入新行后 freshness() 返回 STALE + 差异清单"""
        # 插入更新日期的银行流水
        self.store.execute(
            "INSERT INTO 银行流水 VALUES ('测试', '现金', 10000, '2025-01-01')")
        r = freshness(self.store.conn)
        self.assertEqual(r.state, "STALE")
        self.assertIn("transaction", r.affected_objects)

    def test_ac3_affected_objects_empty_when_fresh(self):
        """AC3: FRESH 时 affected_objects 为空"""
        r = freshness(self.store.conn)
        self.assertEqual(r.state, "FRESH")
        self.assertEqual(r.affected_objects, [])

    def test_ac4_unbuilt_when_no_state(self):
        """AC4: 未构建过返回 UNBUILT 而非异常"""
        s = Store(db_path=":memory:")
        _create_tables(s)
        r = freshness(s.conn)
        self.assertEqual(r.state, "UNBUILT")
        self.assertEqual(r.affected_objects, [])

    def test_ac5_input_hashes_deterministic(self):
        """AC5: 同输入两次构建 input_hashes 一致"""
        v1 = current_version(self.store.conn)
        # 再 build 一次（同输入）
        build_ontology(self.store.conn)
        v2 = current_version(self.store.conn)
        self.assertEqual(v1.input_hashes, v2.input_hashes)
        # build_id 应不同（每次新 uuid），但内容 hash 一致
        self.assertNotEqual(v1.build_id, v2.build_id)


class TestDependencyGraph(unittest.TestCase):
    def test_dependency_graph_structure(self):
        """依赖图：object -> [source_table]；link -> [from_obj, to_obj]"""
        g = dependency_graph("default")
        # object 节点
        self.assertIn("obj_person", g)
        self.assertIsInstance(g["obj_person"], list)
        # link 节点
        self.assertIn("lnk_calls_to", g)
        self.assertEqual(len(g["lnk_calls_to"]), 2)


if __name__ == "__main__":
    unittest.main()
