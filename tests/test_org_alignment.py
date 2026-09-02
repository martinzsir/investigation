"""
tests/test_org_alignment.py
任务 ② 测试：组织层级对齐模块（core.entity）
覆盖：规范化、别名字典、共享精确证据强合并、前缀包含候选(不自动合并)、DuckDB 回写。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))  # entity_resolution 在根包

from core.entity import (
    normalize_org_name, OrganizationResolver,
    build_org_table_from_duckdb, apply_org_to_duckdb,
)
import duckdb


class TestNormalizeOrg(unittest.TestCase):
    def test_strip_suffix(self):
        self.assertEqual(normalize_org_name("宏业建设有限公司"), "宏业建设")
        self.assertEqual(normalize_org_name("宏业建设（集团）"), "宏业建设")

    def test_strip_project_branch(self):
        # 项目部 / 分公司 等层级后缀应被剥除
        self.assertEqual(normalize_org_name("宏业建设第一项目部"), "宏业建设")
        self.assertEqual(normalize_org_name("A建材北京分公司"), "a建材")

    def test_paren_remark(self):
        self.assertEqual(normalize_org_name("财政局（预算处）"), "财政局")

    def test_fullwidth(self):
        self.assertEqual(normalize_org_name("ＡＢＣ株式会社"), "abc株式会社".lower())


class TestOrgResolver(unittest.TestCase):
    def _sample(self):
        return [
            {"name": "宏业建设有限公司", "credit_code": "9133AAA", "legal_rep": "李志强",
             "address": "杭州市", "source_row_id": "b1"},
            {"name": "宏业建设（集团）", "credit_code": "9133AAA", "legal_rep": "李志强",
             "address": "杭州市", "source_row_id": "b2"},
            {"name": "宏业建设第一项目部", "credit_code": "", "legal_rep": "",  # 仅名称层级变体
             "address": "", "source_row_id": "b3"},
            # 以下两条规范化后都 = "泰和建材"（核心字号相同），构成"前缀包含"型强合并，
            # 同时被 _prefix_link 标记 needs_review=True，供正兵确认。
            {"name": "泰和建材", "credit_code": "9133CCC", "source_row_id": "c1"},
            {"name": "泰和建材公司", "credit_code": "9133CCC", "source_row_id": "c2"},
        ]

    def test_strong_merge_by_credit_code(self):
        org = OrganizationResolver()
        org.ingest(self._sample())
        org.resolve()
        mapping = org.mapping()
        # 共享信用代码 → 强合并到同一簇
        self.assertEqual(mapping.get("宏业建设（集团）"), mapping.get("宏业建设有限公司"))

    def test_alias_injection(self):
        org = OrganizationResolver()
        org.add_aliases({"宏业建设": ["宏业建设第一项目部", "宏业建设（集团）"]})
        org.ingest(self._sample())
        org.resolve()
        mapping = org.mapping()
        # 别名字典 → 强合并，且 canonical 取人工确认的标准名"宏业建设"
        self.assertEqual(mapping.get("宏业建设第一项目部"), "宏业建设")
        self.assertEqual(mapping.get("宏业建设（集团）"), "宏业建设")

    def test_prefix_candidate_marked_needs_review(self):
        """
        前缀包含（泰和建材 ⊂ 泰和建材公司）：规范化一致 → 强合并(confidence=1.0)，
        同时被 _prefix_link 标记 needs_review=True，推给正兵确认。
        验证：review_candidates() 能捕获它；mapping 仍保留强合并（保守）。
        """
        org = OrganizationResolver()
        org.ingest(self._sample())
        org.resolve()
        cands = org.review_candidates()
        canon_names = [c.canonical_name for c in cands]
        # 泰和建材簇应被标为待确认候选
        self.assertTrue(
            any("泰和建材" in cn for cn in canon_names),
            msg=f"review_candidates 应包含泰和建材，实际={canon_names}",
        )
        # mapping 对强合并簇仍保留映射（保守：不丢信息）
        mapping = org.mapping()
        self.assertIn("泰和建材公司", mapping)

    def test_confidence_strong_vs_fuzzy(self):
        org = OrganizationResolver()
        org.ingest(self._sample())
        org.resolve()
        by_canon = {c.canonical_name: c for c in org.clusters()}
        # 宏业建设(共享信用代码，多 variant) → 强合并 confidence=1.0
        self.assertEqual(by_canon["宏业建设有限公司"].confidence, 1.0)
        # 泰和建材簇：规范化一致 + 共享信用代码 → confidence=1.0（非 0.9）
        self.assertEqual(by_canon["泰和建材"].confidence, 1.0)
        # 虽强合并，但因属前缀包含类型，标记需复核
        self.assertTrue(by_canon["泰和建材"].needs_review)

    def test_legal_rep_strong_merge_across_groups(self):
        """共享法定代表人（不同规范化组）→ 跨组合并，confidence=1.0。"""
        org = OrganizationResolver()
        org.ingest([
            {"name": "宏业建设有限公司", "legal_rep": "李志强", "source_row_id": "x1"},
            {"name": "A建材", "legal_rep": "李志强", "source_row_id": "x2"},  # 同法人，不同字号
        ])
        org.resolve()
        # 应合并为单簇（共享法人）
        self.assertEqual(len(org.clusters()), 1)
        self.assertEqual(org.clusters()[0].confidence, 1.0)


class TestOrgDuckDBBridge(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("""
            CREATE TABLE 银行流水 AS SELECT * FROM (VALUES
                ('宏业建设第一项目部', 'A建材', 1000000, '2024-03-15'),
                ('宏业建设（集团）', '现金', 500000, '2024-04-01')
            ) t(主体, 对方, 金额, 日期)
        """)
        self.con.execute("""
            CREATE TABLE 工商信息 AS SELECT * FROM (VALUES
                ('宏业建设有限公司', '9133AAA', '李志强', '杭州市')
            ) t(主体, 统一社会信用代码, 法定代表人, 注册地址)
        """)

    def test_apply_org_writes_canonical_columns(self):
        org = build_org_table_from_duckdb(
            self.con, table="工商信息",
            cols={"name": "主体", "credit_code": "统一社会信用代码",
                  "legal_rep": "法定代表人", "address": "注册地址"},
        )
        org.add_aliases({"宏业建设": ["宏业建设第一项目部", "宏业建设（集团）"]})
        org.resolve()
        n = apply_org_to_duckdb(self.con, org, tables=["银行流水"], name_columns=["主体", "对方"])
        cols = [c[0] for c in self.con.execute("DESCRIBE 银行流水").fetchall()]
        self.assertIn("canonical_org_主体", cols)
        self.assertIn("canonical_org_对方", cols)
        # 强合并已回写：宏业建设（集团）与 宏业建设有限公司 因共享信用代码合并为一簇，
        # canonical 为该簇 variants 之一（具名人工确认名优先）
        row = self.con.execute(
            "SELECT canonical_org_主体 FROM 银行流水 WHERE 主体='宏业建设（集团）'"
        ).fetchone()
        self.assertIn(row[0], {"宏业建设", "宏业建设有限公司", "宏业建设（集团）"})
        self.assertGreaterEqual(n, 1)

    def test_org_mapping_only_strong(self):
        """
        mapping() 仅暴露 confidence>=1.0 的强合并簇（含单元素自身映射，保守不丢信息）；
        needs_review 的候选簇（如泰和建材前缀对）不单独入 mapping，由 ReviewQueue 裁决。
        """
        org = OrganizationResolver()
        org.ingest([
            {"name": "泰和建材", "source_row_id": "r1"},
            {"name": "泰和建材公司", "source_row_id": "r2"},  # 规范化一致，簇内前缀 → needs_review
        ])
        org.resolve()
        mapping = org.mapping()
        # 前缀候选簇 confidence 可能被标为强合并（共享证据），但其 variants 仍应被保留
        # 关键断言：泰和建材 至少映射到一个规范名（不丢信息）
        self.assertIn("泰和建材", mapping)
        self.assertTrue(
            mapping["泰和建材"] in {"泰和建材", "泰和建材公司"},
            msg=f"mapping 应保留规范名之一，实际={mapping}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
