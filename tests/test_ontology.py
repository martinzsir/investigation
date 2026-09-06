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


def _write_v2_pack(d, objects, links=None, object_bindings=None,
                   link_bindings=None, actions=None, functions=None,
                   rules=None) -> None:
    """在目录 d 下写一个最小 v2 案件包（functions.json/rules.json 可选不写）。"""
    import json as _json
    (d / "objects.json").write_text(_json.dumps(
        {"schema_version": 2, "objects": objects}, ensure_ascii=False), encoding="utf-8")
    (d / "links.json").write_text(_json.dumps(
        {"schema_version": 2, "links": links or []}, ensure_ascii=False), encoding="utf-8")
    (d / "bindings.json").write_text(_json.dumps(
        {"schema_version": 2,
         "object_bindings": object_bindings or [],
         "link_bindings": link_bindings or []}, ensure_ascii=False), encoding="utf-8")
    (d / "actions.json").write_text(_json.dumps(
        {"schema_version": 2, "actions": actions or []}, ensure_ascii=False), encoding="utf-8")
    if functions is not None:
        (d / "functions.json").write_text(_json.dumps(
            {"schema_version": 2, "functions": functions}, ensure_ascii=False),
            encoding="utf-8")
    if rules is not None:
        (d / "rules.json").write_text(_json.dumps(
            {"schema_version": 2, "rules": rules}, ensure_ascii=False), encoding="utf-8")


# v2 分层校验用的最小合法对象/绑定
_X_OBJ = {"name": "x", "title": "X", "pk": "x_id", "kind": "entity",
          "name_property": "raw_name", "properties": {"raw_name": "string"}}
_X_BIND = {"object": "x", "source_sql": "SELECT 'a' AS raw_name FROM 工商信息"}


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

    def test_实体型主键唯一且内容稳定(self):
        import re
        rows = self.s.query("SELECT person_id, raw_name FROM obj_person ORDER BY raw_name")
        self.assertEqual([r["raw_name"] for r in rows], ["张卫国", "李志强"])
        ids = [r["person_id"] for r in rows]
        self.assertEqual(len(set(ids)), 2)
        # 内容哈希键：person_<12位十六进制>（同输入同键，新增名字不改键——增量重建前提）
        for i in ids:
            self.assertTrue(re.fullmatch(r"person_[0-9a-f]{12}", i),
                            f"代理键格式不符：{i}")

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

    def test_time_window时间邻接链接(self):
        # 链接层只表达时间邻接关系（中标公示 ±20 天有资金交易）；
        # 整数资金/排除公司等检测判据已上移规则层（rules.json R6 → function）
        # fixture：项目A 公示 10-01，张卫国 09-28（offset -3）、宏业建设 10-01（offset 0）
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

    def test_目录11个函数且全部只读(self):
        cat = self.fx.catalog()
        self.assertEqual(len(cat), 11)
        self.assertTrue(all(f["readonly"] for f in cat))
        names = {f["name"] for f in cat}
        self.assertIn("jian_cross_level", names)
        self.assertIn("tipoff_cross_reference", names)   # 新增：内间交叉
        self.assertIn("call_pair_coverage", names)         # 新增：全量对端覆盖诊断
        self.assertIn("location_colocated", names)         # REQ-G-021：地点同框

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
        self.assertEqual(len(pack.links), 10)  # + tipoff_targets_person, osint_mentions, tipoff_from_reporter(REQ-P-032)
        self.assertEqual(len(pack.functions), 11)  # + tipoff_cross_reference, call_pair_coverage, location_colocated(REQ-G-021)
        self.assertEqual(len(pack.rules), 6)   # 规则手册 R1-R6（rules.json 第六段）

    def test_runtime对象不参与编译(self):
        s = make_store()
        stats = build_ontology(s.conn)
        self.assertNotIn("decision", stats["objects"])

    def test_结构化源编译为SQL(self):
        from core.ontology_loader import _compile_structured_source
        from core.ontology import ObjectType
        otype = ObjectType(
            name="transaction", title="交易", pk="txn_id", kind="event",
            name_property="from_raw",
            properties={"from_raw": "string", "amount": "decimal", "date": "date"})
        sql, table, typed_raw = _compile_structured_source(
            {"table": "银行流水",
             "columns": {"from_raw": "主体", "amount": "金额", "date": "日期"}},
            otype, "ctx")
        self.assertEqual(table, "银行流水")
        self.assertIn('"主体" AS from_raw', sql)          # string 不 CAST
        self.assertIn('TRY_CAST("金额" AS DOUBLE) AS amount', sql)   # decimal → DOUBLE
        self.assertIn('TRY_CAST("日期" AS DATE) AS date', sql)      # date → DATE（脏值降级 NULL）
        self.assertEqual(sorted(typed_raw),
                         [("amount", "金额", "decimal"), ("date", "日期", "date")])

    def test_坏包硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "bad"
            d.mkdir()
            # v2：清洗规则属于管道层（bindings.json），未注册规则名装载即硬失败
            _write_v2_pack(
                d,
                objects=[{"name": "x", "title": "X", "pk": "x_id",
                          "kind": "entity", "name_property": "raw_name",
                          "properties": {"raw_name": "string"}}],
                object_bindings=[{"object": "x",
                                  "source_sql": "SELECT 'a' AS raw_name",
                                  "clean": ["不存在的清洗规则"]}])
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


class TestV2Layering(unittest.TestCase):
    """v2 分层校验：类型层（objects/links）与管道层（bindings）交叉引用硬失败。"""

    @staticmethod
    def _pack_dir(td, name="p"):
        from pathlib import Path
        d = Path(td) / name
        d.mkdir()
        return d

    def test_非runtime对象缺binding硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            _write_v2_pack(self._pack_dir(td), objects=[_X_OBJ])  # 无 binding
            with self.assertRaises(ValueError):
                load_pack("p", base_dir=Path(td))

    def test_runtime对象不得有binding(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            obj = dict(_X_OBJ, runtime=True)
            _write_v2_pack(self._pack_dir(td), objects=[obj],
                           object_bindings=[_X_BIND])
            with self.assertRaises(ValueError):
                load_pack("p", base_dir=Path(td))

    def test_未知值类型硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            obj = dict(_X_OBJ, properties={"raw_name": "money"})
            _write_v2_pack(self._pack_dir(td), objects=[obj],
                           object_bindings=[_X_BIND])
            with self.assertRaises(ValueError):
                load_pack("p", base_dir=Path(td))

    def test_binding别名不在属性集硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            bind = {"object": "x",
                    "source": {"table": "工商信息",
                               "columns": {"raw_name": "主体", "bogus": "法人"}}}
            _write_v2_pack(self._pack_dir(td), objects=[_X_OBJ],
                           object_bindings=[bind])
            with self.assertRaises(ValueError):
                load_pack("p", base_dir=Path(td))

    def test_links_json含build_sql硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            link = {"name": "l", "from_obj": "x", "to_obj": "x",
                    "build_sql": "SELECT 1"}
            _write_v2_pack(self._pack_dir(td), objects=[_X_OBJ],
                           object_bindings=[_X_BIND], links=[link],
                           link_bindings=[{"link": "l", "build_sql": "SELECT 1"}])
            with self.assertRaises(ValueError):
                load_pack("p", base_dir=Path(td))

    def test_非runtime链接缺binding硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            link = {"name": "l", "from_obj": "x", "to_obj": "x", "properties": {}}
            _write_v2_pack(self._pack_dir(td), objects=[_X_OBJ],
                           object_bindings=[_X_BIND], links=[link])
            with self.assertRaises(ValueError):
                load_pack("p", base_dir=Path(td))

    def test_链接边属性不在build输出硬失败(self):
        import tempfile
        from pathlib import Path
        import core.ontology_loader as ol
        with tempfile.TemporaryDirectory() as td:
            d = self._pack_dir(td, name="badlink")
            link = {"name": "l", "from_obj": "x", "to_obj": "x",
                    "properties": {"missing_col": "string"}}
            lb = {"link": "l",
                  "build_sql": "SELECT x_id AS from_x, x_id AS to_x FROM obj_x"}
            _write_v2_pack(d, objects=[_X_OBJ], object_bindings=[_X_BIND],
                           links=[link], link_bindings=[lb])
            s = make_store()
            orig = ol.PACK_ROOT
            ol.PACK_ROOT = Path(td)
            try:
                with self.assertRaises(ValueError):
                    build_ontology(s.conn, pack="badlink")
            finally:
                ol.PACK_ROOT = orig


class TestTypedMaterialization(unittest.TestCase):
    """值类型驱动物化：obj_* 列类型由 properties 声明决定；runtime 表 DDL 同口径。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)

    def _col_types(self, table: str) -> dict:
        return {r["column_name"]: r["data_type"]
                for r in self.s.query(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    f"WHERE table_name = '{table}' ORDER BY ordinal_position")}

    def test_金额日期次数列类型化(self):
        t = self._col_types("obj_transaction")
        self.assertEqual(t["amount"], "DOUBLE")
        self.assertEqual(t["date"], "DATE")
        self.assertEqual(t["from_raw"], "VARCHAR")
        c = self._col_types("obj_call")
        self.assertEqual(c["times"], "BIGINT")
        self.assertEqual(c["date"], "DATE")
        p = self._col_types("obj_bid_project")
        self.assertEqual(p["pub_date"], "DATE")

    def test_runtime表按类型声明建列(self):
        d = self._col_types("obj_decision")
        self.assertEqual(list(d),
                         ["decision_id", "decision_type", "clue_id", "legal_basis",
                          "operator", "note", "created_at", "metadata", "source_rows"])
        self.assertTrue(all(t == "VARCHAR" for t in d.values()))
        l = self._col_types("lnk_decision_for")
        self.assertEqual(list(l), ["decision_id", "clue_id"])

    def test_类型化后链接语义不变(self):
        rows = self.s.query(
            "SELECT owner_raw, offset_days FROM lnk_time_window ORDER BY offset_days")
        self.assertEqual([(r["owner_raw"], r["offset_days"]) for r in rows],
                         [("张卫国", -3), ("宏业建设", 0)])


# 规则手册装载校验用的最小合法 SQL 函数/规则（基于 _X_OBJ → obj_x）
_RULE_TEXT = ("这是一条用于测试装载校验的自然语言判据文本，"
              "必须超过三十个字的最小长度要求，写明模式与排除边界。")
_F1 = {"name": "f1", "title": "F1", "output_type": "rows", "impl": "sql",
       "inputs": ["obj_x"],
       "parameters": {"unit": {"type": "integer", "default": 100}},
       "sql": "SELECT raw_name FROM obj_x WHERE {{unit}} > 0"}
_R1 = {"id": "R1", "stage": "xu_shi", "title": "测试规则",
       "dimension": "资金", "jian_types": ["生间"],
       "rule_text": _RULE_TEXT, "function": "f1",
       "params": {"unit": 100}, "hit_when": "rows_nonempty"}


class TestRulebook(unittest.TestCase):
    """自然语言规则手册（rules.json）：装载校验 + 确定性执行轨 + SQL 参数注入。"""

    def setUp(self):
        self.s = make_store()
        build_ontology(self.s.conn)

    def test_默认包规则装载(self):
        pack = load_pack("default")
        self.assertEqual(set(pack.rules), {"R1", "R2", "R3", "R4", "R5", "R6"})
        r1 = pack.rules["R1"]
        self.assertEqual(r1.function, "quarter_end_integer_deposits")
        self.assertEqual(r1.params["quarter_end_window_days"], 15)
        self.assertEqual(r1.hit_when, "rows_nonempty")
        self.assertEqual(pack.rules["R3"].hit_when, "result_hit")
        self.assertEqual(pack.rules["R6"].stage, "qi_zheng")
        for r in pack.rules.values():
            self.assertGreaterEqual(len(r.rule_text), 30)

    def test_规则驱动虚实扫描(self):
        from core.rules import run_rules
        findings = run_rules(self.s, stage="xu_shi")
        ids = [f["rule_id"] for f in findings]
        # fixture：R1（现金 09-28 距季末 2 天，窗口 15 命中）、
        #   [REQ-025 起] R1 作为 integer_amount 组的 primary_rule，命中后抑制同组 R2（非 primary），
        #   R2 进入 suppressed_log（任何保留 finding 都有该字段）；
        #   R4（异人同地命中）、R5（工商关联 LIKE 命中）；R3 仅 2 次通话 < 30 不命中。
        self.assertIn("R1", ids)
        # REQ-025：R2 同组被 R1 抑制 → 主 finding 列表不再出现 R2
        self.assertNotIn("R2", ids)
        # REQ-025：每条保留 finding 都应携带 suppressed_log 审计字段（可追溯被抑制项）
        for f in findings:
            self.assertIn("suppressed_log", f)
        suppressed = [x for f in findings for x in f.get("suppressed_log", [])]
        r2_suppressed = [x for x in suppressed if x["rule_id"] == "R2"]
        self.assertTrue(r2_suppressed, "R2 应出现在 suppressed_log（被 R1 抑制）")
        self.assertIn("primary=R1 命中", r2_suppressed[0]["reason"])
        self.assertNotIn("R3", ids)
        self.assertNotIn("R6", ids)  # stage 过滤：R6 属 qi_zheng
        for f in findings:
            self.assertGreaterEqual(len(f["rule_text"]), 30)
            self.assertTrue(f["source_rows"])

    def test_R1窗口参数注入(self):
        from core.functions import invoke_function
        # 默认窗口 15 天：现金存入 09-28（距季末 09-30 为 2 天）→ 1 个季度桶
        r = invoke_function(self.s, "quarter_end_integer_deposits")
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["rows"][0]["cnt"], 1)
        # 窗口 1 天：距季末 2 天 → 不命中
        r_tight = invoke_function(self.s, "quarter_end_integer_deposits",
                                  {"quarter_end_window_days": 1})
        self.assertEqual(len(r_tight["rows"]), 0)
        # 窗口 0 = 关闭日期谓词（现金摘要分流仍生效）→ 命中
        r_off = invoke_function(self.s, "quarter_end_integer_deposits",
                                {"quarter_end_window_days": 0})
        self.assertEqual(len(r_off["rows"]), 1)
        # 对公转账（460 万）不混入现金规则：桶金额 = 10 万
        self.assertEqual(r["rows"][0]["amt"], 100000.0)

    def test_R6承接链接上移判据(self):
        from core.functions import invoke_function
        rows = invoke_function(self.s, "time_window_collision")["rows"]
        # 链接为邻接边；整数资金+排除公司过滤后仍为 张卫国 -3 / 宏业建设 0
        self.assertEqual([(r["资金主体"], r["偏移天数"]) for r in rows],
                         [("张卫国", -3), ("宏业建设", 0)])

    def test_非enum字符串参数硬失败(self):
        from core.functions import invoke_function
        with self.assertRaises(ValueError):
            invoke_function(self.s, "quarter_end_integer_deposits",
                            {"cash_summary_tokens": "现金存入' OR 1=1 --"})

    def test_线索携带规则原文(self):
        """xu_shi adapter：规则 rule_id/rule_text 落进线索 detail（可回放）。"""
        from core.registry import skill_invoke, get_registry
        import skills.registry_bootstrap  # noqa: F401  注册子技能
        clues = skill_invoke(get_registry(), "xu_shi", store=self.s, ctx={})
        ruled = [c for c in clues if c.detail.get("rule_id")]
        self.assertTrue(ruled)
        self.assertTrue(all(len(c.detail["rule_text"]) >= 30 for c in ruled))

    # ---- loader 硬失败（临时包）----
    @staticmethod
    def _bad_pack(td, *, functions=None, rules=None):
        import tempfile
        from pathlib import Path
        d = Path(td) / "badrule"
        d.mkdir()
        _write_v2_pack(d, objects=[_X_OBJ], object_bindings=[_X_BIND],
                       functions=functions, rules=rules)
        return d

    def test_规则绑定未注册函数硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            self._bad_pack(td, functions=[_F1],
                           rules=[dict(_R1, function="不存在")])
            with self.assertRaises(ValueError):
                load_pack("badrule", base_dir=Path(td))

    def test_规则参数越界硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            self._bad_pack(td, functions=[_F1],
                           rules=[dict(_R1, params={"unit": 100, "extra": 1})])
            with self.assertRaises(ValueError):
                load_pack("badrule", base_dir=Path(td))

    def test_规则文本过短硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            self._bad_pack(td, functions=[_F1],
                           rules=[dict(_R1, rule_text="太短了")])
            with self.assertRaises(ValueError):
                load_pack("badrule", base_dir=Path(td))

    def test_规则维度枚举非法硬失败(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            self._bad_pack(td, functions=[_F1],
                           rules=[dict(_R1, dimension="玄学")])
            with self.assertRaises(ValueError):
                load_pack("badrule", base_dir=Path(td))

    def test_SQL占位符未声明硬失败(self):
        import tempfile
        from pathlib import Path
        bad_f = dict(_F1, sql="SELECT raw_name FROM obj_x WHERE {{unknown}} > 0")
        with tempfile.TemporaryDirectory() as td:
            self._bad_pack(td, functions=[bad_f], rules=[_R1])
            with self.assertRaises(ValueError):
                load_pack("badrule", base_dir=Path(td))

    def test_string参数无enum硬失败(self):
        import tempfile
        from pathlib import Path
        bad_f = {"name": "f2", "title": "F2", "output_type": "rows", "impl": "sql",
                 "inputs": ["obj_x"],
                 "parameters": {"tok": {"type": "string", "default": "自由文本"}},
                 "sql": "SELECT raw_name FROM obj_x WHERE raw_name = {{tok}}"}
        with tempfile.TemporaryDirectory() as td:
            self._bad_pack(td, functions=[bad_f],
                           rules=[dict(_R1, function="f2", params={"tok": "x"})])
            with self.assertRaises(ValueError):
                load_pack("badrule", base_dir=Path(td))


if __name__ == "__main__":
    unittest.main(verbosity=2)
