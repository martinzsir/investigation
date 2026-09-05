"""
tests/test_gateway.py
REQ-002 OntologyReadGateway 语义层唯一读入口 测试。

覆盖 AC1-AC5：
  AC1: 只接受 objects.json/links.json 声明名；中文业务表名 → UnknownObjectError
  AC2: STALE 时禁止返回旧值（StaleOntologyError），allow_stale=True 显式放行
  AC3: explain() 含 ontology_version / source_watermark / applied_policies
  AC4: 网关读取与直查 obj_* 结果等价
  AC5: 无自由 SQL 入口（dir() 无 query/execute 等方法）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.gateway import (                                     # noqa: E402
    OntologyReadGateway, UnknownObjectError, StaleOntologyError,
)
from tests.test_ontology_version import make_store              # noqa: E402


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.conn = self.store.conn
        from core.ontology import build_ontology
        build_ontology(self.conn)
        self.gw = OntologyReadGateway(self.conn)

    def test_ac1_declared_names_only(self):
        """AC1: 中文业务表名/未声明名硬失败，声明名可读。"""
        with self.assertRaises(UnknownObjectError):
            self.gw.objects("银行流水")
        with self.assertRaises(UnknownObjectError):
            self.gw.objects("通话记录")
        with self.assertRaises(UnknownObjectError):
            self.gw.links("银行流水")
        with self.assertRaises(UnknownObjectError):
            self.gw.links("not_declared")

        persons = self.gw.objects("person")
        self.assertEqual(len(persons), 2)  # 张卫国 / 李志强
        self.assertEqual({p["raw_name"] for p in persons}, {"张卫国", "李志强"})
        self.assertTrue(self.gw.links("calls_to"))
        self.assertEqual(self.gw.count("object", "person"), 2)
        self.assertGreaterEqual(self.gw.count("link", "transfers"), 2)

    def test_ac2_stale_blocks_old_values(self):
        """AC2: STALE 抛 StaleOntologyError 不返回数据；allow_stale 显式放行。"""
        self.assertEqual(self.gw.materialization_state(), "FRESH")
        # 源端推进（更晚日期的新流水）
        self.store.execute(
            "INSERT INTO 银行流水 VALUES ('测试人', '现金', 9999, '2025-06-01')")
        self.assertEqual(self.gw.materialization_state(), "STALE")

        with self.assertRaises(StaleOntologyError):
            self.gw.objects("person")
        with self.assertRaises(StaleOntologyError):
            self.gw.links("transfers")
        with self.assertRaises(StaleOntologyError):
            self.gw.count("object", "transaction")

        # 显式 allow_stale（调试留痕通道）可读旧值
        gw_stale = OntologyReadGateway(self.conn, allow_stale=True)
        self.assertEqual(len(gw_stale.objects("person")), 2)

    def test_ac3_explain_keys(self):
        """AC3: explain 三键齐备 + 策略链可审计。"""
        ex = self.gw.explain()
        self.assertIn("ontology_version", ex)
        self.assertIn("source_watermark", ex)
        self.assertIn("applied_policies", ex)
        self.assertTrue(ex["ontology_version"])
        self.assertTrue(ex["source_watermark"])
        self.assertIn("declared_names_only", ex["applied_policies"])
        self.assertIn("stale_block", ex["applied_policies"])
        self.assertIn("no_raw_sql", ex["applied_policies"])
        self.assertFalse(ex["allow_stale"])
        # plan 段含声明清单
        self.assertIn("person", ex["plan"]["declared_objects"])
        self.assertIn("transfers", ex["plan"]["declared_links"])

    def test_ac4_equivalent_to_direct_read(self):
        """AC4: 网关读取与直查 obj_person 结果等价。"""
        got = self.gw.objects("person")
        cur = self.conn.execute("SELECT * FROM obj_person")
        cols = [d[0] for d in cur.description]
        direct = [dict(zip(cols, r)) for r in cur.fetchall()]
        key = lambda r: r["person_id"]
        self.assertEqual(sorted(got, key=key), sorted(direct, key=key))

    def test_ac5_no_raw_sql_entry(self):
        """AC5: 网关不暴露任何自由 SQL 入口。"""
        for m in ("query", "execute", "raw_sql", "sql", "cold_scan"):
            self.assertFalse(
                hasattr(self.gw, m),
                f"网关不得暴露 {m}（自由 SQL 入口）")

    def test_unbuilt_state(self):
        """未构建过语义层：count/objects 不返回假数据（表缺失抛异常而非脏读）。"""
        from core import Store
        s = Store(db_path=":memory:")
        gw = OntologyReadGateway(s.conn)
        self.assertEqual(gw.materialization_state(), "UNBUILT")
        with self.assertRaises(Exception):
            gw.objects("person")


class GatewayProfileTests(unittest.TestCase):
    """REQ-P M3 / P0 网关画像方法（实施计划 §三 P0 + ONTOLOGY_PROFILER 缺陷 3/4）。"""

    def setUp(self):
        self.store = make_store()
        self.conn = self.store.conn
        from core.ontology import build_ontology
        build_ontology(self.conn)
        self.gw = OntologyReadGateway(self.conn)

    def test_ac10_materialized_objects(self):
        """declared ∩ 实表；未物化对象不出现。"""
        got = set(self.gw.materialized_objects())
        self.assertLess({"person", "org", "account", "transaction"},
                        got | {"account"} | got)  # 占位防手滑
        self.assertIn("person", got)
        self.assertIn("transaction", got)
        self.assertNotIn("银行流水", got)

    def test_ac11_materialized_props_覆盖_decimal_date(self):
        """缺陷 3 反向验证：不按值类型过滤，decimal/date 属性同样存在性可判。"""
        m = self.gw.materialized_props()
        self.assertTrue(m["transaction.amount"])   # decimal
        self.assertTrue(m["transaction.date"])     # date
        self.assertTrue(m["call.times"])           # integer
        self.assertTrue(m["person.raw_name"])      # string

    def test_ac12_materialized_props_过滤与未声明键(self):
        m = self.gw.materialized_props(
            ["person.raw_name", "transaction.amount"])
        self.assertEqual(set(m), {"person.raw_name", "transaction.amount"})
        with self.assertRaises(ValueError):
            self.gw.materialized_props(["person.not_declared"])

    def test_ac13_value_profile_样例上限5(self):
        """单 SQL 聚合不取回全量：样例 ≤5 行。"""
        # 造 7 个新组织 → obj_org.raw_name 基数 8
        for i in range(7):
            self.store.execute(
                "INSERT INTO 工商信息 VALUES (?, '某甲', '存续', NULL)",
                [f"测试公司{i}"])
        from core.ontology import build_ontology
        build_ontology(self.conn)
        p = self.gw.value_profile("org", "raw_name")
        self.assertEqual(p["row_count"], 8)
        self.assertEqual(p["distinct"], 8)
        self.assertEqual(len(p["samples"]), 5)      # 硬上限 5
        self.assertEqual(p["null_rate"], 0.0)
        self.assertTrue(p["min"] and p["max"])

    def test_ac14_value_overlap_精确交集(self):
        """INTERSECT 精确计算，不采样：person.raw_name ∩ transaction.from_raw。"""
        r = self.gw.value_overlap(
            "person", "raw_name", "transaction", "from_raw")
        self.assertEqual(r["distinct_a"], 2)        # 张卫国 / 李志强
        self.assertEqual(r["distinct_b"], 2)        # 张卫国 / 宏业建设
        self.assertEqual(r["intersection"], 1)      # 张卫国
        self.assertEqual(r["a_in_b_ratio"], 0.5)
        self.assertEqual(r["b_in_a_ratio"], 0.5)

    def test_ac15_画像只认声明名与声明属性(self):
        with self.assertRaises(Exception):
            self.gw.value_profile("银行流水", "主体")
        with self.assertRaises(ValueError):
            self.gw.value_profile("person", "not_declared")
        with self.assertRaises(Exception):
            self.gw.distinct_values("not_declared", "raw_name")

    def test_ac16_部分物化不崩溃(self):
        """缺陷 4：对象已物化但缺列 → 明确 ValueError 而非 BinderException。"""
        self.conn.execute("ALTER TABLE obj_person DROP COLUMN raw_name")
        # materialized_props 列存在性判定不崩溃 → False
        m = self.gw.materialized_props(["person.raw_name"])
        self.assertFalse(m["person.raw_name"])
        # value_profile fail-loud 提示重建，而非裸 Binder 错误
        with self.assertRaises(ValueError) as ctx:
            self.gw.value_profile("person", "raw_name")
        self.assertIn("无此列", str(ctx.exception))

    def test_ac17_未物化对象明确报错(self):
        conn = self.conn
        conn.execute("DROP TABLE obj_trackpoint")
        with self.assertRaises(ValueError) as ctx:
            self.gw.value_profile("trackpoint", "person_raw")
        self.assertIn("未物化", str(ctx.exception))
        self.assertNotIn("trackpoint", self.gw.materialized_objects())

    def test_ac18_distinct_values与limit(self):
        vals = set(self.gw.distinct_values("transaction", "from_raw"))
        self.assertEqual(vals, {"张卫国", "宏业建设"})
        self.assertEqual(len(self.gw.distinct_values(
            "transaction", "from_raw", limit=1)), 1)

    def test_ac19_画像取数受STALE拦截(self):
        """画像数据面方法与 objects/links 同受 stale_block 策略。"""
        self.store.execute(
            "INSERT INTO 银行流水 VALUES ('测试人', '现金存入', 1, '2025-06-01')")
        with self.assertRaises(StaleOntologyError):
            self.gw.value_profile("person", "raw_name")
        with self.assertRaises(StaleOntologyError):
            self.gw.value_overlap("person", "raw_name",
                                  "transaction", "from_raw")
        with self.assertRaises(StaleOntologyError):
            self.gw.distinct_values("transaction", "from_raw")
        # schema 内省不受 STALE 影响（物化与否不随源端推进变化）
        self.assertTrue(self.gw.materialized_objects())


class EntityLinkExplorerTests(unittest.TestCase):
    """REQ-P M3 / P0 编排器：connectable_props + 变体双轨。"""

    def setUp(self):
        self.store = make_store()
        from core.ontology import build_ontology
        build_ontology(self.store.conn)
        self.gw = OntologyReadGateway(self.store.conn)
        from core.ontology_profile import EntityLinkExplorer
        self.ex = EntityLinkExplorer(self.gw)

    def test_ac20_connectable_props(self):
        """string − metadata_props − runtime 对象；全排除对象不出现。"""
        got = self.ex.connectable_props()
        self.assertEqual(got["person"], ["raw_name"])
        self.assertEqual(got["org"], ["raw_name", "legal_rep", "relation"])  # status 排除
        self.assertEqual(got["transaction"], ["from_raw", "to_raw"])
        self.assertNotIn("clue", got)        # 全列 metadata → 不出现
        self.assertNotIn("decision", got)    # runtime → 不出现

    def test_ac21_规则轨同语言异写(self):
        """规范化后拼音相似 ≥ 阈值 → needs_review 候选，不自动合并。

        简繁异体（李志強/李志强）拼音串全等 sim=1.0；短名单字差异
        （张卫东/张卫国）在拼音空间 sim≈0.67，需调低阈值（AC 固化可配置）。
        """
        got = self.ex.rule_variants(["李志強", "李志强", "张卫东"])
        self.assertEqual(len(got), 1)
        self.assertEqual({got[0]["a"], got[0]["b"]}, {"李志強", "李志强"})
        self.assertTrue(got[0]["needs_review"])

        got2 = self.ex.rule_variants(["张卫东", "张卫国", "李志强"],
                                     threshold=0.6)
        self.assertEqual(len(got2), 1)
        self.assertEqual({got2[0]["a"], got2[0]["b"]}, {"张卫东", "张卫国"})

    def test_ac21b_规范化一致不算变体(self):
        """空白/括号异写经规范化归同 → 不产生候选对（归一交给 normalize）。"""
        got = self.ex.rule_variants(["张卫国", "张 卫 国", "张卫国（董事长）"])
        self.assertEqual(got, [])

    def test_ac22_别名轨命中(self):
        """subject_aliases 共现 → 别名对（显式注入别名表）。"""
        from core.ontology_profile import EntityLinkExplorer
        ex2 = EntityLinkExplorer(self.gw,
                                 aliases={"张卫国": ["张伟国", "张卫国"]})
        got = ex2.alias_variants(["张卫国", "张伟国"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["canonical"], "张卫国")
        self.assertEqual(got[0]["alias"], "张伟国")
        self.assertEqual(got[0]["source"], "explicit")

    def test_ac23_无别名表降级(self):
        """别名表为空 → alias 轨不可用被显式标注（降级可见，非静默）。"""
        from core.ontology_profile import EntityLinkExplorer
        ex2 = EntityLinkExplorer(self.gw, aliases={})
        v = ex2.variants("person", "raw_name")
        self.assertFalse(v["alias_track_available"])
        self.assertEqual(v["alias_variants"], [])

    def test_ac24_变体0不代表干净(self):
        """铁律固化：双轨皆空仍输出警示 note。"""
        v = self.ex.variants("person", "raw_name")
        self.assertEqual(v["rule_variants"], [])
        self.assertEqual(v["alias_variants"], [])
        self.assertIn("不代表干净", v["note"])

    def test_ac25_元数据属性拒绝变体探测(self):
        """REQ-P-009 编排层保证：metadata 属性/runtime 对象不参与变体探测。"""
        with self.assertRaises(ValueError):
            self.ex.variants("org", "status")      # metadata_props
        with self.assertRaises(ValueError):
            self.ex.variants("decision", "note")   # runtime 对象

    def test_ac26_真实包变体探测端到端(self):
        """经 gateway distinct_values 全链路（只读）。"""
        v = self.ex.variants("transaction", "from_raw")
        self.assertEqual(v["obj"], "transaction")
        self.assertIn("prop", v)
        self.assertIn("alias_track_available", v)


if __name__ == "__main__":
    unittest.main()
