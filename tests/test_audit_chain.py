"""
tests/test_audit_chain.py
REQ-007 事件溯源审计 + SHA-256 哈希链 测试。

覆盖 AC1-AC5：
  AC1: 连续追加 100 条，chain_verify() 为 True
  AC2: 篡改中间任一条 after → chain_verify() 为 False
  AC3: 删除中间一条 → chain_verify() 为 False
  AC4: 重放事件（重复 event_id）→ 被 IGNORE（event_id 唯一约束）
  AC5: root_hash() 在同序列下稳定复现
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                    # noqa: E402
from core.audit import AuditChain                         # noqa: E402
from core.registry import LineageClue                     # noqa: E402


class TestAuditChain(unittest.TestCase):
    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.chain = AuditChain(self.store.conn)

    def test_ac1_chain_verify_true_after_100_appends(self):
        """AC1: 连续追加 100 条，chain_verify() 为 True"""
        for i in range(100):
            self.chain.append(
                operator="test", before={"s": i}, after={"s": i + 1},
                source_row_ids=[], ontology_version="v1")
        self.assertEqual(self.chain.count(), 100)
        self.assertTrue(self.chain.chain_verify())

    def test_ac2_tamper_after_detected(self):
        """AC2: 篡改中间任一条 after → chain_verify() 为 False"""
        for i in range(10):
            self.chain.append(
                operator="test", before={"s": i}, after={"s": i + 1},
                source_row_ids=[], ontology_version="v1")
        # 篡改第 5 条的 after_state
        self.store.execute(
            """UPDATE audit_chain SET after_state='{"s": 999}'
               WHERE event_id=(
                   SELECT event_id FROM audit_chain
                   ORDER BY seq LIMIT 1 OFFSET 5)""")
        self.assertFalse(self.chain.chain_verify())

    def test_ac3_delete_middle_detected(self):
        """AC3: 删除中间一条 → chain_verify() 为 False"""
        for i in range(10):
            self.chain.append(
                operator="test", before=None, after={"s": i},
                source_row_ids=[], ontology_version="v1")
        self.store.execute(
            """DELETE FROM audit_chain WHERE event_id=(
                   SELECT event_id FROM audit_chain
                   ORDER BY seq LIMIT 1 OFFSET 5)""")
        self.assertFalse(self.chain.chain_verify())

    def test_ac4_replay_detected(self):
        """AC4: 重放事件（重复 event_id）→ 被 IGNORE（event_id 唯一约束）"""
        eid = self.chain.append(
            operator="test", before=None, after={"s": 1},
            source_row_ids=[], ontology_version="v1")
        # 手工尝试用相同 event_id 插入应被忽略（INSERT OR IGNORE）
        self.store.execute(
            """INSERT OR IGNORE INTO audit_chain
               (seq, event_id, case_id, ontology_version, operator, prev_hash,
                signature, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [999, eid, "default", "v1", "test", "0"*64, "fake", "2025-01-01"])
        # 只有 1 条（重复被忽略）
        self.assertEqual(self.chain.count(), 1)

    def test_ac5_root_hash_stable(self):
        """AC5: root_hash() 在同序列下稳定复现"""
        for i in range(5):
            self.chain.append(
                operator="test", before=None, after={"s": i},
                source_row_ids=[], ontology_version="v1")
        h1 = self.chain.root_hash()
        # 重新建链读同一表
        chain2 = AuditChain(self.store.conn)
        h2 = chain2.root_hash()
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, "0"*64)  # 非空链


class TestLineageClueWithAuditChain(unittest.TestCase):
    """验证 LineageClue.set_status 与 AuditChain 集成。"""

    def setUp(self):
        self.store = Store(db_path=":memory:")
        self.chain = AuditChain(self.store.conn)
        self.clue = LineageClue(skill_id="test", title="测试线索")

    def test_set_status_writes_chain(self):
        """set_status 传入 audit_chain 时，哈希链同步写入"""
        self.clue.set_status("查证中", operator="张三", audit_chain=self.chain)
        self.assertEqual(self.chain.count(), 1)
        self.assertTrue(self.chain.chain_verify())

    def test_set_status_without_chain_backward_compat(self):
        """set_status 不传 audit_chain 时，仅内存 audit_log（向后兼容）"""
        self.clue.set_status("查证中", operator="张三")
        self.assertEqual(len(self.clue.audit_log), 1)
        self.assertEqual(self.chain.count(), 0)

    def test_set_filed_writes_chain(self):
        """set_filed 传入 audit_chain 时，哈希链同步写入"""
        self.clue.set_status("查证中", operator="张三", audit_chain=self.chain)
        self.clue.set_status("已固证", operator="张三", audit_chain=self.chain)
        self.clue.set_filed("张三", "案号2025-001", audit_chain=self.chain)
        self.assertEqual(self.chain.count(), 3)
        self.assertTrue(self.chain.chain_verify())
        self.assertEqual(self.clue.status, "已立案")


if __name__ == "__main__":
    unittest.main()
