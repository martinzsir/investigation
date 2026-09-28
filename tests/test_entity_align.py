"""导入期人名实体对齐：强证据采集与静默失效防护（回归守护）。

背景（真实失效）：registry 采集器把强证据源表名写死为「人员表」，而库里实际
表名为「人员信息」→ 证号/手机号整条采集静默跳过。后果是红线 R-1 从未被触发：
两个「张卫国」被按名合并成一个实体，且**没有任何标志**提示这里并了两个人。
（表内重复至少可见，哈希合并无声——这是更危险的失效形态。）

守护三条：
  1. 强证据全缺 → 落 warning 诊断（不管源表叫什么名，比逐表检查更本质）
  2. 有强证据 → 不报该诊断（真实库口径）
  3. 关系型指代（张卫国配偶）不占人审队列，但保留可见
  4. 同义表名（人员表/人员信息）两种写法都能采集，不得因改名再次失效
"""
import unittest

from core.registry import _resolve_person_from_store, _table_exists


class _Conn:
    """极简 store 适配器：_resolve_person_from_store 只取 .conn"""

    def __init__(self, conn):
        self.conn = conn


class TestStrongEvidenceCollection(unittest.TestCase):
    def _store(self, conn):
        from core.run_health import RunHealth
        return _Conn(conn), RunHealth(conn)

    def test_warns_when_no_strong_evidence_at_all(self):
        """裸名字库：一条证号/手机都没有 → 必须报 warning，不得静默。

        此时对齐器退化为「只按名合并」，同名异人被无声并为一个实体。
        """
        from core import Store
        from core.run_health import RunHealth
        s = Store(db_path=":memory:")
        s.execute('CREATE TABLE "通话记录" ("主体" VARCHAR, "对端" VARCHAR)')
        s.execute('INSERT INTO "通话记录" VALUES (?, ?)', ["张三", "李四"])
        health = RunHealth(s.conn)
        _resolve_person_from_store(s, health=health)
        rows = s.conn.execute(
            "SELECT severity, reason FROM run_diagnostic "
            "WHERE kind='entity_table_skipped' AND source='person_align'").fetchall()
        self.assertEqual(len(rows), 1, "强证据全缺必须报警，不得静默")
        self.assertEqual(rows[0][0], "warning")
        self.assertIn("只按名合并", rows[0][1])

    def test_no_warning_when_strong_evidence_present(self):
        """有证号/手机 → 不报该诊断（避免噪声淹没真实告警）。"""
        from core import Store
        from core.run_health import RunHealth
        s = Store(db_path=":memory:")
        s.execute('CREATE TABLE "人员信息" ("姓名" VARCHAR, "身份证号" VARCHAR)')
        s.execute('INSERT INTO "人员信息" VALUES (?, ?)', ["王五", "330100199001011234"])
        health = RunHealth(s.conn)
        _resolve_person_from_store(s, health=health)
        n = s.conn.execute(
            "SELECT count(*) FROM run_diagnostic "
            "WHERE kind='entity_table_skipped' AND source='person_align'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_synonym_table_names_both_work(self):
        """同义表名「人员表」与「人员信息」都要能采到——改名不得再次失效。"""
        from core import Store
        for table in ("人员表", "人员信息"):
            with self.subTest(table=table):
                s = Store(db_path=":memory:")
                s.execute(f'CREATE TABLE "{table}" ("姓名" VARCHAR, "身份证号" VARCHAR)')
                s.execute(f'INSERT INTO "{table}" VALUES (?, ?)', ["赵六", "id1"])
                s.execute(f'INSERT INTO "{table}" VALUES (?, ?)', ["赵六", "id2"])
                r = _resolve_person_from_store(s)
                r.resolve()
                zhao = [c for c in r.clusters() if c.canonical_name == "赵六"]
                self.assertEqual(len(zhao), 2, f"{table} 应采到证号并触发红线 R-1 拆簇")
                self.assertTrue(all(c.needs_review for c in zhao))


class TestRelationalRef(unittest.TestCase):
    def test_relational_ref_excluded_from_review(self):
        """「张卫国配偶」是关系型指代：不进人审队列，但保留可见。

        它与「张卫国」字面重叠，但既非同一人亦非同名异人。若进队列，
        会稀释真正需要裁决的同名歧义；若被字面相似判为别名，则错误合并。
        """
        from core.entity import _load_person_resolver
        ER = _load_person_resolver()
        er = ER()
        er.ingest([
            {"name": "张卫国", "phone": "13800000001", "source_row_id": "人员信息.姓名"},
            {"name": "张卫国配偶", "source_row_id": "银行流水.对方"},
        ])
        er.resolve()
        review = [c.canonical_name for c in er.review_candidates()]
        self.assertNotIn("张卫国配偶", review, "关系型指代不得占人审队列")
        rel = [c.canonical_name for c in er.relational_refs()]
        self.assertIn("张卫国配偶", rel, "排除须可见，不得静默丢弃")

    def test_report_exposes_relational_refs(self):
        """report 中须能查到关系型指代（供正兵复核排除是否误伤）。"""
        from core.entity import _load_person_resolver
        ER = _load_person_resolver()
        er = ER()
        er.ingest([{"name": "李志强家属", "source_row_id": "x"}])
        er.resolve()
        rep = er.report()
        self.assertIn("relational_refs", rep)
        self.assertIn("李志强家属",
                      [x["canonical"] for x in rep["relational_refs"]])


class TestDeclaredSourceCrossCheck(unittest.TestCase):
    def test_declared_sources_nonempty(self):
        """本体声明的 person 摄入源能被解析出来（交叉校验的前置）。"""
        from core.registry import _declared_person_sources
        src = _declared_person_sources()
        self.assertTrue(src, "本体 bindings 未解析出 person 源表，交叉校验会空转")
        self.assertIn("通话记录", src)

    def test_table_exists_helper(self):
        from core import Store
        s = Store(db_path=":memory:")
        s.execute('CREATE TABLE "存在表" (c VARCHAR)')
        self.assertTrue(_table_exists(s.conn, "存在表"))
        self.assertFalse(_table_exists(s.conn, "不存在表"))


if __name__ == "__main__":
    unittest.main()
