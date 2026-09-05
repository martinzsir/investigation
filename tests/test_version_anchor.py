"""
tests/test_version_anchor.py
REQ-G-007 版本锚定留痕 + REQ-G-001 缓存失效令牌：
  - AuditChain 取不到 ontology 版本 → 回退 "unknown" 但落 version_anchor_missing（warning）
  - append 写入 version=unknown 时 after_state 内标 anchor_status=missing，链校验仍通过
  - derived._source_version_set 锚点失败时返回一次性随机令牌（两次不等），强制重算，
    并落 version_anchor_missing（不再返回常量 "unknown::0" 复用陈旧缓存）
"""
from __future__ import annotations

import json
import unittest

import duckdb

from core import derived as D
from core.audit import AuditChain
from core.run_health import RunHealth


class AuditVersionAnchorTests(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.h = RunHealth(self.con)

    def tearDown(self):
        self.con.close()

    def test_unknown_version_is_recorded(self):
        chain = AuditChain(self.con, health=self.h)
        v = chain.current_ontology_version()
        self.assertEqual(v, "unknown")
        diags = [r for r in self.h.rows() if r["kind"] == "version_anchor_missing"]
        self.assertGreaterEqual(len(diags), 1)
        self.assertEqual(diags[0]["severity"], "warning")

    def test_append_marks_anchor_missing_and_chain_verifies(self):
        chain = AuditChain(self.con, health=self.h)
        chain.append(operator="王检察官", before=None, after={"status": "查证中"},
                     source_row_ids=["row:1"], ontology_version="unknown")
        row = self.con.execute(
            "SELECT after_state FROM audit_chain ORDER BY seq DESC LIMIT 1").fetchone()
        after = json.loads(row[0])
        self.assertEqual(after.get("anchor_status"), "missing")
        # 标注在签名前完成 → 链仍自洽
        self.assertTrue(chain.chain_verify())


class DerivedCacheTokenTests(unittest.TestCase):
    def test_failure_token_is_one_shot_and_forces_recompute(self):
        from core import Store
        s = Store(db_path=":memory:")  # 不建语义层 → 版本锚点与行数均取不到
        h = RunHealth(s.conn)
        try:
            t1 = D._source_version_set(s, "ghost_type", health=h)
            t2 = D._source_version_set(s, "ghost_type", health=h)
            diags = [r for r in h.rows() if r["kind"] == "version_anchor_missing"]
        finally:
            s.close()
        self.assertTrue(t1.startswith("unknown::ghost_type::"), t1)
        self.assertNotEqual(t1, t2, "失败令牌须一次性，强制 miss 重算")
        self.assertGreaterEqual(len(diags), 1)

    def test_failure_token_does_not_cross_isolate_pk(self):
        # 不同 obj_pks 的 params_hash 本就不同；此处只确认令牌含 obj_type 维度
        from core import Store
        s = Store(db_path=":memory:")
        try:
            ta = D._source_version_set(s, "aaa", health=RunHealth(s.conn))
            tb = D._source_version_set(s, "bbb", health=RunHealth(s.conn))
        finally:
            s.close()
        self.assertIn("::aaa::", ta)
        self.assertIn("::bbb::", tb)


if __name__ == "__main__":
    unittest.main(verbosity=2)
