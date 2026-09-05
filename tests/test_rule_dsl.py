"""
tests/test_rule_dsl.py
REQ-026 规则 DSL（组合与时序）：
  - AC1：all 需全部命中才产出 finding
  - AC2：any 任一命中即产出
  - AC3：not 正确取反
  - AC4：within_days 时序窗生效（超窗不命中）
  - AC5：AST 深度超限 → 拒绝（防表达式炸弹）
  - AC6：DSL explain() 打印为可读计划
"""
from __future__ import annotations

import unittest

from core import Store
from core.ontology import build_ontology
from core.rule_dsl import parse, compile, evaluate, MAX_DEPTH


def _make_store() -> Store:
    """和 test_rule_overlap 同 baseline 场景。

    REQ-G-022 修复后命中：R1、R4、R5（宏业建设法人=李志强 命中知识包）、R6；
    未命中：R3（单一对端 2 行 < 降级绝对阈值 30）→ 用 R3 作未命中样例。
    """
    s = Store(db_path=":memory:")
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    s.execute(
        "INSERT INTO 银行流水 VALUES ('张卫国','现金存入',100000,'2021-09-28'),"
        "('宏业建设','A建材',4600000,'2021-10-01')")
    s.execute("INSERT INTO 通话记录 VALUES "
              "('张卫国','李志强','2021-10-01',3),"
              "('张卫国','李志强','2021-09-30',5)")
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设','李志强','存续',NULL)")
    s.execute("INSERT INTO 轨迹出行 VALUES "
              "('2021-10-02','张卫国','项目B'),"
              "('2021-09-30','张卫国','项目A'),"
              "('2021-10-01','李志强','项目A')")
    s.execute("INSERT INTO 招投标档案 VALUES ('项目A','宏业建设','2021-10-01','张卫国')")
    s.execute("INSERT INTO 公开OSINT VALUES ('张卫国','分管招投标','2019-03-01','政府官网')")
    s.execute("INSERT INTO 举报材料 VALUES ('2022-01-10','经济类','张卫国','匿名','x')")
    build_ontology(s.conn)
    return s


class RuleDslTests(unittest.TestCase):

    def test_ac1_all_requires_every_hit(self):
        """AC1：all 全部命中才 True；缺任一条 → False。"""
        store = _make_store()
        try:
            # baseline：R1、R4、R5、R6 四个命中（REQ-G-022 后 R5 恢复命中）
            node = parse({"all": [{"rule": "R1"}, {"rule": "R4"},
                                  {"rule": "R5"}, {"rule": "R6"}]})
            out = evaluate(store, node)
            self.assertTrue(out["hit"], f"R1∧R4∧R5∧R6 都命中应通过，实际 note={out.get('degraded_note')}")
            # 加一个未命中的 R3 → 不通过
            node2 = parse({"all": [{"rule": "R1"}, {"rule": "R4"}, {"rule": "R3"}]})
            out2 = evaluate(store, node2)
            self.assertFalse(out2["hit"], "R3 未命中 → all 应假")
        finally:
            store.close()

    def test_ac2_any_single_sufficient(self):
        """AC2：any 任一命中即可；全未命中才假。"""
        store = _make_store()
        try:
            # R5 修复后命中：单独 any 即为真
            node = parse({"any": [{"rule": "R5"}]})
            self.assertTrue(evaluate(store, node)["hit"],
                            "R5（py impl + rows_nonempty）应命中（REQ-G-022 回归）")
            # 全部未命中 → False（R3 在 baseline 中未命中）
            node2 = parse({"any": [{"rule": "R3"}]})
            self.assertFalse(evaluate(store, node2)["hit"],
                             "R3 单独应未命中；all any 应 False")
        finally:
            store.close()

    def test_ac3_not_correctly_inverts(self):
        """AC3：NOT 取反。"""
        store = _make_store()
        try:
            # NOT(R3) → R3 未命中 → True
            self.assertTrue(evaluate(store, parse({"not": {"rule": "R3"}}))["hit"])
            # NOT(R1) → R1 命中 → False
            self.assertFalse(evaluate(store, parse({"not": {"rule": "R1"}}))["hit"])
        finally:
            store.close()

    def test_ac4_within_days_window(self):
        """AC4：within_days 时序窗。

        baseline 中：
          - R4（轨迹同框）日期：2021-09-30（张-项目A）/ 2021-10-01（李-项目A） → 日期范围 9/30~10/01
          - R6（中标-资金邻接）中标公示日 2021-10-01 → 日期 10/01
          两者差 0 或 1 天 → within=1 恰好通过；
          再把 R4 换成 R1（无日期列）→ within=1 保守失败（日期未知不通过 AC4）。
        """
        store = _make_store()
        try:
            # R4 vs R6：日期完全相交 → within=1 通过
            good = parse({"all": [{"rule": "R4", "within_days": 1}, {"rule": "R6"}]})
            out_g = evaluate(store, good)
            self.assertTrue(out_g["hit"],
                            f"R4/R6 同一天应 within=1 通过，note={out_g.get('degraded_note')} plan={out_g['plan']}")
            # R1（缺可用日期列）+ R6：within=1 → 应保守失败
            bad = parse({"all": [{"rule": "R1", "within_days": 1}, {"rule": "R6"}]})
            out_b = evaluate(store, bad)
            self.assertFalse(out_b["hit"],
                             "R1 无可用日期列 → within_days 应保守失败（AC4），"
                             f"degraded_note={out_b.get('degraded_note')}")
            self.assertIn("日期", out_b.get("degraded_note", "") or "",
                          f"应给出日期相关降级说明：{out_b.get('degraded_note')}")
        finally:
            store.close()

    def test_ac5_depth_bomb_rejected(self):
        """AC5：depth > MAX_DEPTH(5) → parse 抛 ValueError。

        RuleRef=1 层；每包一层 NOT 深度 +1。
        """
        # depth = 6：5 次 NOT 包 R1 → 6 > 5 应抛
        ast = {"rule": "R1"}
        for _ in range(5):
            ast = {"not": ast}
        with self.assertRaises(ValueError) as cm:
            parse(ast)
        self.assertIn("深度", str(cm.exception))
        self.assertIn(str(MAX_DEPTH), str(cm.exception))
        # depth = 5：4 次 NOT 包 R1 → 5 不抛
        ast_ok = {"rule": "R1"}
        for _ in range(4):
            ast_ok = {"not": ast_ok}
        parse(ast_ok)  # 不抛

    def test_ac6_explain_readable(self):
        """AC6：DSL explain() 可读计划。"""
        node = parse({
            "all": [
                {"rule": "R1"},
                {"rule": "R6", "within_days": 30},
                {"not": {"rule": "R4"}}
            ]
        })
        plan = compile(node)
        t = plan.explain()
        self.assertIn("AND(", t)
        self.assertIn("WITHIN(R6, 30d)", t)
        self.assertIn("NOT(", t)
        self.assertIn("R1", t)
        self.assertGreater(len(t), 40, f"explain 应非空可读：{t!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
