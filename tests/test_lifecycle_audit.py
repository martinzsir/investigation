"""
tests/test_lifecycle_audit.py
B5 生命周期事件补录测试。

覆盖：
  - case_created 事件写入 audit_chain
  - source_imported 事件写入 audit_chain
  - build_succeeded 事件写入 audit_chain
  - 多事件 prev_hash 衔接 + 链完整
  - after_state 含正确 event 类型 + 业务上下文
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.audit import AuditChain                           # noqa: E402
from server.app.store.state_store import StateStore          # noqa: E402
from server.app.worker.lifecycle_audit import (              # noqa: E402
    on_case_created, on_source_imported, on_build_succeeded)


class TestLifecycleAudit(unittest.TestCase):
    """生命周期事件补录测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.case_dir = Path(self.tmp.name)
        self.case_id = "test_case"
        self.state_path = self.case_dir / "state.sqlite"
        self.store = StateStore(self.case_id, self.state_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _audit_rows(self):
        return self.store.conn.execute(
            "SELECT event_id, operator, after_state, ontology_version "
            "FROM audit_chain ORDER BY seq"
        ).fetchall()

    # ---- case_created ----
    def test_case_created_writes_chain(self):
        on_case_created(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="张检察官", pack_id="default")
        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        after = json.loads(rows[0][2])
        self.assertEqual(after["event"], "case_created")
        self.assertEqual(after["by"], "张检察官")
        self.assertEqual(after["pack_id"], "default")
        self.assertEqual(rows[0][1], "张检察官")
        self.assertEqual(rows[0][3], "待建案")

    # ---- source_imported ----
    def test_source_imported_writes_chain(self):
        on_source_imported(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="张检察官", version=0,
            upload_id="u1", table="银行流水", rows=100)
        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        after = json.loads(rows[0][2])
        self.assertEqual(after["event"], "source_imported")
        self.assertEqual(after["table"], "银行流水")
        self.assertEqual(after["rows"], 100)
        self.assertEqual(rows[0][3], "v0")

    # ---- build_succeeded ----
    def test_build_succeeded_writes_chain(self):
        on_build_succeeded(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="system", prev_version=0, new_version=1,
            objects=42, links=10)
        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        after = json.loads(rows[0][2])
        self.assertEqual(after["event"], "build_succeeded")
        self.assertEqual(after["prev_version"], 0)
        self.assertEqual(after["new_version"], 1)
        self.assertEqual(after["objects"], 42)
        self.assertEqual(rows[0][3], "v1")

    # ---- 多事件链衔接 ----
    def test_multiple_events_chain_consistent(self):
        on_case_created(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="张检察官", pack_id="default")
        on_source_imported(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="张检察官", version=0,
            upload_id="u1", table="银行流水", rows=50)
        on_build_succeeded(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="system", prev_version=0, new_version=1,
            objects=10, links=3)
        rows = self._audit_rows()
        self.assertEqual(len(rows), 3)
        events = [json.loads(r[2])["event"] for r in rows]
        self.assertEqual(events, ["case_created", "source_imported",
                                   "build_succeeded"])
        # 链完整
        chain = AuditChain.readonly(self.store.conn, case_id=self.case_id,
                                    backend="sqlite")
        self.assertTrue(chain.chain_verify())

    # ---- ontology_version 锚点 ----
    def test_ontology_version_anchors(self):
        on_case_created(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="张", pack_id="default")
        on_build_succeeded(
            case_dir=self.case_dir, case_id=self.case_id,
            operator="system", prev_version=0, new_version=3,
            objects=5, links=2)
        rows = self._audit_rows()
        # 建案=待建案，构建=v3
        self.assertEqual(rows[0][3], "待建案")
        self.assertEqual(rows[1][3], "v3")


if __name__ == "__main__":
    unittest.main()
