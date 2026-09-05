"""
tests/test_semantic_search.py
REQ-042 Semantic Search（受控）测试。

覆盖 AC1–AC5：
  AC1: 检索结果受对象/属性策略约束
  AC2: embedding 不得成为确定规则唯一证据
  AC3: 未经授权对象不进索引
  AC4: 向量与原文分区隔离存储
  AC5: 检索记录进审计

测试策略：_embed_texts 用 monkeypatch 注入假向量，不依赖网络/API Key。
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                        # noqa: E402
from core.access import AccessContext                         # noqa: E402
from core.audit import AuditChain                             # noqa: E402
from core.ontology import build_ontology                     # noqa: E402
from core.search import SemanticSearch, _cosine_similarity    # noqa: E402


def _make_store_with_data() -> Store:
    """建一个含 osint_article/tipoff 样例数据的内存 Store 并构建语义层。"""
    s = Store(db_path=":memory:")
    c = s.conn
    # 与 ontology/default bindings 一致的源表 schema
    c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    c.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR, 采集时间 TIMESTAMP, 保留天数 INTEGER)")
    c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    # 样例数据
    c.execute("INSERT INTO 银行流水 VALUES ('张卫国','宏业建设',50000,'2021-10-01')")
    c.execute("INSERT INTO 通话记录 VALUES ('张卫国','李志强','2021-10-02',3)")
    c.execute("INSERT INTO 工商信息 VALUES ('宏业建设','李志强','存续','')")
    c.execute("INSERT INTO 轨迹出行 VALUES ('2021-10-03','张卫国','滨江路')")
    c.execute("INSERT INTO 招投标档案 VALUES ('滨江路改造','宏业建设','2021-09-15','')")
    c.execute("INSERT INTO 公开OSINT VALUES ('宏业建设','宏业建设与滨江路改造项目存在异常关联，涉嫌围标','2024-01-15','财经观察','2024-01-15 08:00:00',3650)")
    c.execute("INSERT INTO 公开OSINT VALUES ('李某','李某通过多个空壳公司转移资金，涉及银行流水异常','2024-02-20','深度调查','2024-02-20 08:00:00',3650)")
    c.execute("INSERT INTO 举报材料 VALUES ('2024-03-01','宏业建设行贿举报','宏业建设','张某某','宏业建设向招标办人员行贿，金额约200万，银行转账记录可查13812345678')")
    build_ontology(c)
    return s


class TestSemanticSearch(unittest.TestCase):

    def setUp(self):
        self.store = _make_store_with_data()
        self.addCleanup(self.store.close)

        # 假向量：文本 hash → 固定向量，使相似度可预测
        def fake_embed(texts, api_key=None):
            result = []
            for t in texts:
                # 用文本前几字符的 ord 生成确定向量
                vals = [float(ord(c) % 100) / 100.0 for c in (t[:64] + "\0" * 64)[:64]]
                result.append(vals)
            return result

        self._patcher = patch("core.search._embed_texts", side_effect=fake_embed)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    # ---- AC3 ----

    def test_ac3_unauthorized_not_indexed(self):
        """AC3: 正兵对 tipoff 无权（需偏将+clearance2），build_index 跳过。"""
        ctx = AccessContext(operator="正兵A", role="正兵", clearance=1)
        ss = SemanticSearch(self.store.conn, access=ctx)
        n = ss.build_index(["osint_article", "tipoff"])
        # osint_article 也需偏将+clearance2，正兵都无权 → 0 条
        self.assertEqual(n, 0)
        self.assertEqual(ss.index_count(), 0)

    def test_ac3_authorized_indexed(self):
        """AC3: 主办(ctx) 有权 → 索引成功。"""
        ctx = AccessContext(operator="主办A", role="主办", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        n = ss.build_index(["osint_article", "tipoff"])
        self.assertEqual(n, 3)  # 2 osint + 1 tipoff
        self.assertEqual(ss.index_count(), 3)

    # ---- AC1 ----

    def test_ac1_results_policy_filtered(self):
        """AC1: 检索结果经属性级遮蔽（content_raw 被 partial mask）。"""
        ctx = AccessContext(operator="偏将A", role="偏将", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        ss.build_index(["osint_article", "tipoff"])
        results = ss.search("宏业建设", top_k=5)
        self.assertTrue(len(results) > 0)
        # 偏将无权读 content_raw（仅主办/human）→ 应被遮蔽
        for r in results:
            if r["object_name"] == "tipoff":
                content = r["row"].get("content_raw", "")
                self.assertIn("*", content)  # 被 mask_partial 或 full

    # ---- AC2 ----

    def test_ac2_solo_evidence_warning(self):
        """AC2: 结果含 evidence_type=embedding 和 solo_evidence_warning。"""
        ctx = AccessContext(operator="主办A", role="主办", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        ss.build_index(["osint_article"])
        results = ss.search("宏业建设", top_k=3)
        self.assertTrue(len(results) > 0)
        for r in results:
            self.assertEqual(r["evidence_type"], "embedding")
            self.assertIn("唯一证据", r["solo_evidence_warning"])
            self.assertIn("交叉验证", r["solo_evidence_warning"])

    # ---- AC4 ----

    def test_ac4_vector_text_separation(self):
        """AC4: semantic_index 表不含原文（content_raw/info_text 等文本列）。"""
        ctx = AccessContext(operator="主办A", role="主办", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        ss.build_index(["osint_article", "tipoff"])
        cols = [r[1] for r in self.store.conn.execute(
            "PRAGMA table_info('semantic_index')").fetchall()]
        for col in cols:
            self.assertNotIn("content", col.lower())
            self.assertNotIn("info_text", col.lower())
            self.assertNotIn("raw_name", col.lower())
        # embedding 列存的是 JSON 数组，不是原文
        row = self.store.conn.execute(
            "SELECT embedding FROM semantic_index LIMIT 1").fetchone()
        emb = json.loads(row[0])
        self.assertIsInstance(emb, list)
        self.assertTrue(all(isinstance(x, float) for x in emb))

    # ---- AC5 ----

    def test_ac5_search_audited(self):
        """AC5: 检索后 audit_chain 多一条，semantic_search_log 有记录。"""
        ctx = AccessContext(operator="主办A", role="主办", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        ss.build_index(["osint_article"])
        chain_before = AuditChain(self.store.conn).count()
        ss.search("宏业建设", top_k=3)
        chain_after = AuditChain(self.store.conn).count()
        self.assertEqual(chain_after, chain_before + 1)

        log_count = self.store.conn.execute(
            "SELECT COUNT(*) FROM semantic_search_log").fetchone()[0]
        self.assertEqual(log_count, 1)

    # ---- 离线降级 ----

    def test_offline_no_apikey_graceful(self):
        """无 API Key（_embed_texts 返回 None）→ build_index 返回 0，search 返回空。"""
        self._patcher.stop()
        self._patcher = patch("core.search._embed_texts", return_value=None)
        self._patcher.start()

        ctx = AccessContext(operator="主办A", role="主办", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        n = ss.build_index(["osint_article"])
        self.assertEqual(n, 0)
        results = ss.search("测试", top_k=3)
        self.assertEqual(results, [])

    # ---- 幂等 ----

    def test_index_idempotent(self):
        """同对象重复 build_index 不产生重复行。"""
        ctx = AccessContext(operator="主办A", role="主办", clearance=2)
        ss = SemanticSearch(self.store.conn, access=ctx)
        n1 = ss.build_index(["osint_article"])
        n2 = ss.build_index(["osint_article"])
        self.assertEqual(n1, 2)   # 首次 2 条
        self.assertEqual(n2, 0)   # 第二次 0 条（text_hash 一致跳过）
        self.assertEqual(ss.index_count(), 2)


if __name__ == "__main__":
    unittest.main()
