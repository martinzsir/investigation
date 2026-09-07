"""
tests/test_orphan_scan.py
M2 W-009：孤儿版本文件扫描 + 隔离区 TTL。

验收点：
  - AC-1 强杀场景（写文件无版本历史记录）→ 扫描发现孤儿；
  - AC-2 孤儿移入 .quarantine/ 隔离区（非直删），白名单（active/
    pending_reclaim/当前指针）文件永不移动；
  - AC-3 隔离区超过 TTL 的文件被删除；
  - AC-4 扫描不打开任何 DuckDB 文件（只读目录结构，正常读写不受影响）；
  - AC-5 扫描/隔离/清理结果落 ops_events；
  - dry_run 只记事件不移动；崩溃残留（reclaimed 状态+文件在）同样收口。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.meta.models import (
    CASE_ACTIVE,
    CaseRecord,
    VER_RECLAIMED,
)
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory
from server.app.worker.orphan_scan import (
    OPS_ORPHAN_CLEANED,
    OPS_ORPHAN_QUARANTINED,
    QUARANTINE_DIR,
    scan_once,
)


def _mk_version_file(path: Path) -> None:
    """造一个极小 duckdb 文件（不打开它做任何业务读写）。"""
    import duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.close()


class OrphanScanBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        self.case_dir = self.factory.case_dir("c1")
        # 当前指针 v2：v1 pending_reclaim、v2 active
        _mk_version_file(self.factory.version_path("c1", 1))
        _mk_version_file(self.factory.version_path("c1", 2))
        self.repo.set_version("c1", 1, by="u")
        self.repo.set_version("c1", 2, by="u")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestOrphanDetection(OrphanScanBase):

    def test_orphan_file_detected_and_quarantined(self):
        # AC-1：模拟强杀——写 v9.duckdb 但无版本历史记录
        orphan = self.factory.version_path("c1", 9)
        _mk_version_file(orphan)
        stats = scan_once(self.repo, self.factory)
        self.assertEqual(len(stats["orphans"]), 1)
        self.assertEqual(stats["orphans"][0]["version"], 9)
        self.assertEqual(stats["orphans"][0]["reason"], "no_version_history")
        # AC-2：移入隔离区而非直删
        self.assertFalse(orphan.exists())
        q = self.case_dir / QUARANTINE_DIR / "v9.duckdb"
        self.assertTrue(q.exists(), "孤儿应进入隔离区保留人工捞回窗口")
        self.assertEqual(stats["quarantined"], 1)

    def test_whitelisted_versions_never_moved(self):
        # v1=pending_reclaim（回收器管辖）、v2=当前指针——都不得动
        stats = scan_once(self.repo, self.factory)
        self.assertEqual(stats["orphans"], [])
        self.assertTrue(self.factory.version_path("c1", 1).exists())
        self.assertTrue(self.factory.version_path("c1", 2).exists())
        self.assertFalse((self.case_dir / QUARANTINE_DIR).exists())

    def test_crash_leftover_of_reclaimed_version_collected(self):
        # 回收器崩溃残留：状态已 reclaimed 但文件还在 → 孤儿扫描收口
        self.repo.mark_version_status("c1", 1, VER_RECLAIMED, by="test")
        stats = scan_once(self.repo, self.factory)
        versions = [o["version"] for o in stats["orphans"]]
        self.assertIn(1, versions)
        self.assertFalse(self.factory.version_path("c1", 1).exists())
        self.assertTrue((self.case_dir / QUARANTINE_DIR / "v1.duckdb")
                        .exists())

    def test_non_version_files_ignored(self):
        (self.case_dir / "state.sqlite").write_bytes(b"x")
        (self.case_dir / "notes.txt").write_text("hello")
        stats = scan_once(self.repo, self.factory)
        self.assertEqual(stats["orphans"], [])
        self.assertTrue((self.case_dir / "state.sqlite").exists())

    def test_quarantine_dir_not_rescanned(self):
        # 隔离区里的文件不参与判定（防二次移动）
        (self.case_dir / QUARANTINE_DIR).mkdir()
        _mk_version_file(self.case_dir / QUARANTINE_DIR / "v9.duckdb")
        stats = scan_once(self.repo, self.factory)
        self.assertEqual(stats["orphans"], [])
        self.assertTrue(
            (self.case_dir / QUARANTINE_DIR / "v9.duckdb").exists())


class TestOrphanTTL(OrphanScanBase):
    """AC-3：隔离区超期文件删除。"""

    def test_expired_quarantine_file_cleaned(self):
        qdir = self.case_dir / QUARANTINE_DIR
        qdir.mkdir(parents=True)
        old = qdir / "v7.duckdb"
        fresh = qdir / "v8.duckdb"
        _mk_version_file(old)
        _mk_version_file(fresh)
        # 伪造 mtime：old 8 天前，fresh 刚刚
        eight_days_ago = time.time() - 8 * 86400
        os.utime(old, (eight_days_ago, eight_days_ago))
        stats = scan_once(self.repo, self.factory, ttl_days=7.0)
        self.assertEqual(stats["cleaned"], 1)
        self.assertFalse(old.exists(), "超期隔离文件应删除")
        self.assertTrue(fresh.exists(), "未超期文件保留")
        self.assertEqual(
            len(self.repo.list_ops(kind=OPS_ORPHAN_CLEANED)), 1)


class TestOrphanSafetyAndAudit(OrphanScanBase):
    """AC-4/AC-5 + dry_run。"""

    def test_scan_does_not_touch_case_dbs(self):
        # AC-4：扫描前后白名单文件字节一致（未被打开/改写）
        v2 = self.factory.version_path("c1", 2)
        before = v2.read_bytes()
        scan_once(self.repo, self.factory)
        self.assertEqual(v2.read_bytes(), before)

    def test_ops_events_recorded(self):
        _mk_version_file(self.factory.version_path("c1", 9))
        scan_once(self.repo, self.factory)
        self.assertEqual(
            len(self.repo.list_ops(kind=OPS_ORPHAN_QUARANTINED)), 1)
        self.assertEqual(len(self.repo.list_ops(kind="orphan_scan")), 1)

    def test_dry_run_records_but_never_moves(self):
        _mk_version_file(self.factory.version_path("c1", 9))
        stats = scan_once(self.repo, self.factory, dry_run=True)
        self.assertEqual(len(stats["orphans"]), 1)  # 发现但不移动
        self.assertEqual(stats["quarantined"], 0)
        self.assertTrue(self.factory.version_path("c1", 9).exists())

    def test_missing_cases_root_is_noop(self):
        f2 = StoreFactory(cases_root=self.tmp / "no_such_root",
                          meta=self.repo)
        stats = scan_once(self.repo, f2)
        self.assertEqual(stats["scanned_cases"], 0)


if __name__ == "__main__":
    unittest.main()
