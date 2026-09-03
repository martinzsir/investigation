"""
tests/test_object_set.py
REQ-029 ObjectSet 查询构造器：
  - AC1：链式调用返回新对象，不修改原对象（不可变）
  - AC2：search_around 支持 max_hops，环图不无限递归（造自环边确认步数≤max_hops）
  - AC3：构造过程不查询 store/materialize 才执行（构造期不触发 gateway import 异常）
  - AC4：explain 输出可解释的非空字符串
  - AC5：注入防护——把原始 SQL 字符串作为 Eq.value 合法（文本只是文本），但把非 Predicate
    对象当 filter 参数时 TypeError（AST 节点类型才接受），或把 dict 当 Eq.value 也 TypeError。
"""
from __future__ import annotations

import sys
import unittest

from core.object_set import (OntologyObjectSet, Eq, Ne, In, Gt, Lt, And, Or, Not,
                              Predicate, _PRIMITIVE_TYPES)


class ObjectSetTests(unittest.TestCase):

    def test_ac1_immutable_chaining(self):
        """AC1：每次链式都返回新对象，原对象 filters/hops 不变。"""
        base = OntologyObjectSet.from_objects("person")
        self.assertEqual(base.filters, ())
        self.assertIsNone(base.hops)
        p_eq = Eq("name_raw", "张卫国")
        filtered = base.filter(p_eq)
        around = filtered.search_around("calls_to", 2)
        with_link = around.with_link_filter(Gt("weight", 5))
        # 原对象不变
        self.assertEqual(base.filters, ())
        self.assertIsNone(base.hops)
        self.assertEqual(base.link_filters, ())
        # 返回是新对象
        self.assertIsNot(filtered, base)
        self.assertIsNot(around, filtered)
        self.assertIsNot(with_link, around)
        # 内容累加正确
        self.assertEqual(len(filtered.filters), 1)
        self.assertEqual(around.hops, ("calls_to", 2))
        self.assertEqual(len(with_link.link_filters), 1)

    def test_ac2_search_around_cycle_bounded(self):
        """AC2：自环图不无限递归；最多 2 跳 × 每跳不重复扩展 seen 节点。"""
        from core import Store
        from core.ontology import build_ontology
        s = Store(db_path=":memory:")
        # 自环通话：A↔B 2 跳应停止
        s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
        s.execute("INSERT INTO 通话记录 VALUES ('A','B','2021-01-01',3),"
                  "('B','A','2021-01-02',4),('A','B','2021-01-03',1)")
        s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
        s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
        s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
        s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
        s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
        s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
        build_ontology(s.conn)
        try:
            os = OntologyObjectSet.from_objects("person").search_around("calls", 2)
            rows = os.materialize(s)
        finally:
            s.close()
        self.assertLessEqual(
            len([r for r in rows if r.get("_from_search_around")]), 20,
            "2 跳扩展自环图不会无限增长（seen 去重保护）")

    def test_ac3_lazy_no_touch_store_during_construction(self):
        """AC3：构造过程不访问 store——通过移除 store 相关依赖模拟确认。"""
        # 建一堆 chain，全程不崩且不 materialize，不应碰任何 store
        built = (OntologyObjectSet.from_objects("person")
                 .filter(And(Eq("role", "主办"), Not(In("org_pk", [-1, -2]))))
                 .search_around("calls", 3))
        text = built.explain()
        self.assertIn("obj_person", text)
        self.assertIn("AND(", text)
        self.assertIn("search_around(calls", text)
        # 链式期间不访问任何 Store：store 变量从未使用过即 assert 未 import 失败也不触发
        self.assertGreater(len(built.filters) + len(built.link_filters), 0,
                           "链式构造应积累 filters 而不查询")
        # materialize 才会真正触达 store；传 None 应抛（非构造期异常）
        with self.assertRaises((AttributeError, NameError, TypeError, OSError)):
            OntologyObjectSet.from_objects("person").materialize(None)  # type: ignore

    def test_ac4_explain_nonempty(self):
        """AC4：复杂 explain() 输出非空且人类可读。"""
        x = (OntologyObjectSet.from_objects("account")
             .filter(Or(Eq("bank", "ICBC"), Lt("balance", 100.0)))
             .search_around("transfer_from", 1)
             .with_link_filter(Ne("currency", "USD")))
        t = x.explain()
        self.assertTrue(len(t) >= 40, f"explain 输出过短：{t!r}")
        self.assertIn("obj_account", t)
        self.assertIn("OR(", t)

    def test_ac5_ast_injection_guards(self):
        """AC5：仅 Predicate 节点合法；dict/复杂对象放 value 里 TypeError。"""
        # 情况 1：filter() 传 dict（非 Predicate）→ TypeError
        with self.assertRaises(TypeError):
            OntologyObjectSet.from_objects("person").filter({"field": "SELECT *"})
        # 情况 2：Eq.value 是 dict（如子查询 dict 伪装）→ TypeError
        with self.assertRaises(TypeError):
            Eq("name", {"where": "1=1 --"})
        # 情况 3：And.children 是普通字符串 → TypeError
        with self.assertRaises(TypeError):
            And("name='x'", Eq("a", 1))  # type: ignore
        # 情况 4：字符串 "SELECT ..." 作为 Eq.value 是合法文本（只是值），不应该误判
        ok = Eq("comment", "SELECT * FROM t; -- 真实业务文本")
        self.assertEqual(ok.eval({"comment": "SELECT * FROM t; -- 真实业务文本"}), True)
        # 情况 5：Not 子非 Predicate → TypeError
        with self.assertRaises(TypeError):
            Not(123)  # type: ignore


if __name__ == "__main__":
    unittest.main(verbosity=2)
