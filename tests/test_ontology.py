"""
tests/test_ontology.py
语义层（Object/Link/Action）测试：声明式编译 + 代理键策略 + 红线约束。

覆盖：
  1. 编译幂等：同输入两次构建，语义表逐行一致
  2. 代理键策略：实体型按 raw_name 唯一且与插入顺序无关；事件型按行唯一
  3. 清洗规则：obj_person 不含组织/摘要 token
  4. 溯源红线（红线 2）：obj_* 全行 source_rows 非空；事件行带全字段溯源串
  5. Link 物化：calls_to/transfers/co_located/time_window 与声明语义一致
  6. Action 注册表：allowed_from 由状态机反向派生；file 仅已固证、终态、legal_basis 必填
  7. optional_table：clue_disposal_status 缺失时 obj_clue 跳过不崩
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.ontology import build_ontology, get_action          # noqa: E402
from core.ontology_loader import load_pack                    # noqa: E402
from core.registry import ClueStatusMachine, LineageClue      # noqa: E402

EVENT_TABLE_PKS = [("obj_transaction", "txn_id"), ("obj_call", "call_id"),
                   ("obj_trackpoint", "track_id")]


def _create_tables(s: Store) -> None:
    s.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    s.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    s.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    s.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    s.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")


def make_store() -> Store:
    """内存库 + 最小语义样本（不依赖外部 parquet）。"""
    s = Store(db_path=":memory:")
    _create_tables(s)
    s.execute("""
        INSERT INTO 银行流水 VALUES
        ('张卫国', '现金存入', 100000, '2021-09-28'),
        ('宏业建设', 'A建材', 4600000, '2021-10-01')
    """)
    # 同对端两行 → 事件型多行场景
    s.execute("""
        INSERT INTO 通话记录 VALUES
        ('张卫国', '李志强', '2021-10-01', 3),
        ('张卫国', '李志强', '2021-09-30', 5)
    """)
    s.execute("INSERT INTO 工商信息 VALUES ('宏业建设', '李志强', '存续', NULL)")
    # 异人同地点 ±1 天 → co_located 命中；同人异地点 → 不产出
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


class TestBuildIdempotent(unittest.TestCase):
    """编译幂等：同输入多次构建，产物逐行一致。"""

    def setUp(self):
        self.s = make_store()
        self.stats1 = build_ontology(self.s.conn)
        self.snap1 = self._snapshot()
        self.stats2 = build_ontology(self.s.conn)
        self.snap2 = self._snapshot()

    def _snapshot(self) -> dict:
        tables = ([f"obj_{n}" for n in self.stats1["objects"]]
                  + [f"lnk_{n}" for n in self.stats1["links"]])
        return {t: sorted(str(r) for r in self.s.query(f"SELECT * FROM {t}"))
                for t in tables}

    def test_两次构建逐行一致(self):
        self.assertEqual(self.snap1, self.snap2)

    def test_统计行数一致(self):
        self.assertEqual(self.stats1["objects"], self.stats2["objects"])
        self.assertEqual(self.stats1["links"], self.stats2["links"])


class TestProxyKeys(unittest.TestCase):
    """主键策略（选项 B）：实体型按名稳定，事件型按行唯一。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)

    def test_事件型主键逐行唯一(self):
        for t, pk in EVENT_TABLE_PKS:
            rows = self.s.query(f"SELECT {pk} FROM {t}")
            self.assertEqual(len(rows), len({r[pk] for r in rows}), msg=t)

    def test_实体型主键唯一且按名排序分配(self):
        rows = self.s.query("SELECT person_id, raw_name FROM obj_person ORDER BY raw_name")
        # 码点序：张(U+5F20) < 李(U+674E)
        self.assertEqual([r["raw_name"] for r in rows], ["张卫国", "李志强"])
        self.assertEqual([r["person_id"] for r in rows],
                         ["person_0001", "person_0002"])

    def test_代理键与插入顺序无关(self):
        s2 = Store(db_path=":memory:")
        _create_tables(s2)
        # 同一批行，INSERT 顺序整体倒置
        s2.execute("""
            INSERT INTO 银行流水 VALUES
            ('宏业建设', 'A建材', 4600000, '2021-10-01'),
            ('张卫国', '现金存入', 100000, '2021-09-28')
        """)
        s2.execute("""
            INSERT INTO 通话记录 VALUES
            ('张卫国', '李志强', '2021-09-30', 5),
            ('张卫国', '李志强', '2021-10-01', 3)
        """)
        s2.execute("INSERT INTO 工商信息 VALUES ('宏业建设', '李志强', '存续', NULL)")
        s2.execute("""
            INSERT INTO 轨迹出行 VALUES
            ('2021-10-01', '李志强', '项目A'),
            ('2021-09-30', '张卫国', '项目A'),
            ('2021-10-02', '张卫国', '项目B')
        """)
        s2.execute("INSERT INTO 招投标档案 VALUES ('项目A', '宏业建设', '2021-10-01', '张卫国')")
        s2.execute("INSERT INTO 公开OSINT (主体, 公开信息, 发布日期, 来源) VALUES ('张卫国', '分管招投标', '2019-03-01', '政府官网')")
        s2.execute("INSERT INTO 举报材料 VALUES ('2022-01-10', '经济类', '张卫国', '匿名', '反映收受宏业现金约 120 万')")
        build_ontology(s2.conn)

        # 实体型：raw_name → pk 映射一致
        m1 = {r["raw_name"]: r["person_id"] for r in self.s.query("SELECT * FROM obj_person")}
        m2 = {r["raw_name"]: r["person_id"] for r in s2.query("SELECT * FROM obj_person")}
        self.assertEqual(m1, m2)
        # 事件型：全行（含 pk）一致
        for t, pk in EVENT_TABLE_PKS:
            r1 = sorted(str(r) for r in self.s.query(f"SELECT * FROM {t}"))
            r2 = sorted(str(r) for r in s2.query(f"SELECT * FROM {t}"))
            self.assertEqual(r1, r2, msg=t)


class TestCleanRules(unittest.TestCase):
    """清洗规则：人名口径去组织/摘要噪音。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)

    def test_person不含组织与摘要token(self):
        names = {r["raw_name"] for r in self.s.query("SELECT raw_name FROM obj_person")}
        self.assertEqual(names, {"张卫国", "李志强"})
        # 组织留在 obj_org，不因清洗丢失
        orgs = {r["raw_name"] for r in self.s.query("SELECT raw_name FROM obj_org")}
        self.assertIn("宏业建设", orgs)


class TestLineageRedline(unittest.TestCase):
    """红线 2：语义层全行可溯源。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)

    def test_全部对象行溯源非空(self):
        tables = ["obj_person", "obj_org", "obj_account", "obj_transaction",
                  "obj_call", "obj_trackpoint", "obj_bid_project"]
        for t in tables:
            rows = self.s.query(f"SELECT source_rows FROM {t}")
            self.assertTrue(rows, msg=f"{t} 无数据")
            for r in rows:
                self.assertTrue(r["source_rows"], msg=f"{t} 存在空溯源行")

    def test_事件行溯源带全字段(self):
        r = self.s.query("SELECT source_rows FROM obj_transaction LIMIT 1")[0]
        src = json.loads(r["source_rows"])[0]
        self.assertTrue(src.startswith("银行流水:"))
        self.assertIn("from_raw=", src)
        self.assertIn("date=", src)


class TestLinks(unittest.TestCase):
    """Link 物化语义。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)

    def test_transfers与交易行数一致(self):
        n_txn = self.s.query("SELECT COUNT(*) AS n FROM obj_transaction")[0]["n"]
        n_lnk = self.s.query("SELECT COUNT(*) AS n FROM lnk_transfers")[0]["n"]
        self.assertEqual(n_txn, n_lnk)

    def test_calls_to按事件行展开(self):
        n_call = self.s.query("SELECT COUNT(*) AS n FROM obj_call")[0]["n"]
        n_lnk = self.s.query("SELECT COUNT(*) AS n FROM lnk_calls_to")[0]["n"]
        self.assertEqual(n_call, n_lnk)

    def test_同人同框不产出(self):
        n = self.s.query(
            "SELECT COUNT(*) AS n FROM lnk_co_located WHERE person_1 = person_2"
        )[0]["n"]
        self.assertEqual(n, 0)

    def test_异人同地相邻日命中(self):
        # 张卫国 09-30 与 李志强 10-01 同在项目A → 对称展开 2 行
        rows = self.s.query("SELECT person_1, person_2, location FROM lnk_co_located")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["location"] == "项目A" for r in rows))

    def test_time_window过滤语义(self):
        # 整数资金 ±20 天：张卫国 09-28（offset -3）与 宏业建设 10-01（offset 0）
        rows = self.s.query(
            "SELECT owner_raw, offset_days FROM lnk_time_window ORDER BY offset_days"
        )
        self.assertEqual([(r["owner_raw"], r["offset_days"]) for r in rows],
                         [("张卫国", -3), ("宏业建设", 0)])


class TestActionRegistry(unittest.TestCase):
    """Action 注册表（actions.json 装载）：单一事实来源为 ClueStatusMachine。"""

    def setUp(self):
        self.actions = load_pack("default").actions

    def test_注册表完整(self):
        self.assertEqual(set(self.actions),
                         {"verify", "reset", "exclude", "confirm", "file"})

    def test_allowed_from反向派生自状态机(self):
        T = ClueStatusMachine._TRANSITIONS
        for a in self.actions.values():
            expect = tuple(src for src, tgts in T.items() if a.target_status in tgts)
            self.assertEqual(a.allowed_from, expect, msg=a.name)

    def test_file终态与角色约束(self):
        f = self.actions["file"]
        self.assertTrue(f.terminal)
        self.assertEqual(f.allowed_from, ("已固证",))
        self.assertEqual(f.requires_role, "human")
        self.assertIn("create_decision", f.side_effects)
        param_names = {p.name for p in f.parameters}
        self.assertIn("legal_basis", param_names)
        self.assertTrue(all(p.required for p in f.parameters if p.name == "legal_basis"))

    def test_排除必须带理由(self):
        param_names = {p.name for p in self.actions["exclude"].parameters}
        self.assertIn("reason", param_names)


class TestActionExecutor(unittest.TestCase):
    """Action 执行器：角色/参数/状态机/副作用（决策对象）。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)
        from core.disposal import DisposalBoard
        self.clue = LineageClue(skill_id="t", title="测试线索", jian_types=["生间"])
        self.board = DisposalBoard([self.clue], store=self.s)

    def test_占位操作者立案被拒(self):
        with self.assertRaises(ValueError):
            self.board.file(self.clue.clue_id, operator="system", legal_basis="X")

    def test_缺法定依据被拒(self):
        with self.assertRaises(ValueError):
            self.board.file(self.clue.clue_id, operator="王检察官", legal_basis="")

    def test_待查直跳已立案被拒(self):
        with self.assertRaises(ValueError):
            self.board.file(self.clue.clue_id, operator="王检察官",
                            legal_basis="杭检立〔2026〕1号")

    def test_合法立案创建决策对象(self):
        self.board.verify(self.clue.clue_id, operator="王检察官")
        self.board.confirm(self.clue.clue_id, operator="王检察官")
        self.board.file(self.clue.clue_id, operator="王检察官",
                        legal_basis="杭检立〔2026〕1号")
        self.assertEqual(self.clue.status, "已立案")
        dec = self.s.query("SELECT decision_type, legal_basis, operator, clue_id "
                           "FROM obj_decision")
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["legal_basis"], "杭检立〔2026〕1号")
        self.assertEqual(dec[0]["clue_id"], self.clue.clue_id)
        link = self.s.query("SELECT * FROM lnk_decision_for")
        self.assertEqual(len(link), 1)

    def test_排除缺理由被拒(self):
        with self.assertRaises(ValueError):
            self.board.exclude(self.clue.clue_id, operator="王检察官", reason="")

    def test_审计链完整(self):
        self.board.verify(self.clue.clue_id, operator="王检察官", note="已调取流水")
        last = self.clue.audit_log[-1]
        self.assertEqual(last["operator"], "王检察官")
        self.assertEqual(last["to_status"], "查证中")


class TestFunctionLayer(unittest.TestCase):
    """Function 层：目录、只读执行、写操作拦截。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)
        from core.functions import FunctionExecutor
        self.fx = FunctionExecutor(self.s)

    def test_目录10个函数且全部只读(self):
        cat = self.fx.catalog()
        self.assertEqual(len(cat), 10)
        self.assertTrue(all(f["readonly"] for f in cat))
        names = {f["name"] for f in cat}
        self.assertIn("jian_cross_level", names)
        self.assertIn("tipoff_cross_reference", names)   # 新增：内间交叉
        self.assertIn("call_pair_coverage", names)         # 新增：全量对端覆盖诊断

    def test_sql函数返回行(self):
        r = self.fx.invoke("quarter_end_integer_deposits")
        self.assertTrue(r["readonly"])
        self.assertGreaterEqual(len(r["rows"]), 1)

    def test_py函数返回report(self):
        r = self.fx.invoke("call_frequency_spike")
        self.assertIn("hit", r["result"])
        self.assertIn("diagnostics", r["result"])   # 新增：降级诊断字段
        self.assertIn("is_degraded", r["result"]["diagnostics"])
        r2 = self.fx.invoke("jian_cross_level")
        self.assertIn("交叉等级", r2["result"])
        # 举报材料已建模 → 内间不再是缺口
        jian_rows = {x["间"]: x for x in r2["result"]["rows"]}
        self.assertTrue(jian_rows["内间"]["命中"], "举报材料接入后内间应命中")
        self.assertEqual(jian_rows["内间"]["缺口"], [], "内间缺口列表应为空")
        # 新增：tipoff 交叉 & call_pair_coverage 冒烟
        r3 = self.fx.invoke("tipoff_cross_reference")
        self.assertIn("summary", r3["result"])
        self.assertGreaterEqual(r3["result"]["summary"]["total_tipoffs"], 1)
        r4 = self.fx.invoke("call_pair_coverage")
        self.assertIn("summary", r4["result"])
        self.assertIn("degraded_callers", r4["result"]["summary"])

    def test_sql写操作被只读守卫拦截(self):
        from core.functions import _assert_readonly
        for evil in ("INSERT INTO obj_person VALUES ('x')",
                     "DROP TABLE obj_person",
                     "UPDATE obj_call SET times=0"):
            with self.assertRaises(ValueError):
                _assert_readonly(evil, "evil")

    def test_未知名函数报错(self):
        with self.assertRaises(KeyError):
            self.fx.invoke("no_such_function")


class TestOntologyLoader(unittest.TestCase):
    """JSON 案件包装载与校验。"""

    def test_默认包装载计数(self):
        pack = load_pack("default")
        names = [o.name for o in pack.objects]
        self.assertIn("person", names)
        self.assertIn("decision", names)  # runtime 对象也在目录
        self.assertIn("tipoff", names)      # 新增：举报材料（内间）
        self.assertIn("osint_article", names)  # 新增：公开OSINT文章（死间）
        self.assertEqual(len(pack.links), 9)   # + tipoff_targets_person, osint_mentions
        self.assertEqual(len(pack.functions), 10)  # + tipoff_cross_reference, call_pair_coverage

    def test_runtime对象不参与编译(self):
        s = make_store()
        stats = build_ontology(s.conn)
        self.assertNotIn("decision", stats["objects"])

    def test_结构化源编译为SQL(self):
        from core.ontology_loader import _compile_structured_source
        sql, table = _compile_structured_source(
            {"table": "银行流水",
             "columns": {"from_raw": "主体", "amount": "金额"}}, "ctx")
        self.assertEqual(table, "银行流水")
        self.assertIn('"主体" AS from_raw', sql)

    def test_坏包硬失败(self):
        import tempfile, json as _json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "bad"
            d.mkdir()
            (d / "objects.json").write_text(_json.dumps({
                "schema_version": 1,
                "objects": [{"name": "x", "title": "X", "pk": "x_id",
                             "name_col": "raw_name",
                             "clean": ["不存在的清洗规则"]}],
            }), encoding="utf-8")
            (d / "links.json").write_text(_json.dumps(
                {"schema_version": 1, "links": []}), encoding="utf-8")
            (d / "actions.json").write_text(_json.dumps(
                {"schema_version": 1, "actions": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_pack("bad", base_dir=Path(td))

    def test_版本不符硬失败(self):
        import tempfile, json as _json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "oldver"
            d.mkdir()
            (d / "objects.json").write_text(_json.dumps(
                {"schema_version": 99, "objects": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_pack("oldver", base_dir=Path(td))


class TestOptionalSource(unittest.TestCase):
    """optional_table：源表缺失时跳过而非崩溃。"""

    def test_缺clue表时跳过(self):
        s = make_store()   # 未建 clue_disposal_status
        stats = build_ontology(s.conn)
        self.assertTrue(any("obj_clue" in x for x in stats["skipped"]))
        self.assertNotIn("clue", stats["objects"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
