"""
tests/test_version_reclaim.py
M2 W-008：版本延迟回收（读者租约 + 归档压实）。

验收点：
  - AC-1 版本切换后旧版本标记 pending_reclaim 而非立即删除；
  - AC-2 有活跃读者租约时旧版本不删除（推迟回收）；
  - AC-3 租约清零/过期后 N 个周期内回收器完成删除；
  - AC-4 删除是物理性的（文件不存在，磁盘空间释放）；
  - AC-5 ARCHIVED 案件触发版本压实，仅保留最终版本；
  - 读者租约随 StoreFactory open/close 获取/释放（跨进程事实源）；
  - meta 无租约能力（本地桩）时工厂优雅降级；
  - 删除失败（OSError）状态回滚 pending_reclaim，下周期重试。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.meta.models import (
    CASE_ACTIVE,
    CASE_ARCHIVED,
    CaseRecord,
    VER_ACTIVE,
    VER_PENDING_RECLAIM,
    VER_RECLAIMED,
)
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool
from server.app.worker.reclaim import VersionReclaimer
from server.app.worker.tasks import TASK_ARCHIVE, enqueue_task


def _builder(conn, *, pack: str, base_dir: Path, progress) -> dict:
    """极简构建器：建一张表即成功（不依赖 ontology 包）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    progress(90.0, "compile", "完成", "stub")
    return {"objects": {}, "links": {}}


class ReclaimBase(unittest.TestCase):
    """公共装配：案件 + 两轮 BUILD（v1/v2）+ Worker。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        from server.app.worker.tasks import TASK_BUILD, handle_build
        for _ in range(2):  # v1 → v2
            t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                             idem_key=f"b{_}")
            handle_build(t, repo=self.repo, factory=self.factory,
                         snapshot_base_for=lambda cid: self.tmp / "snap",
                         builder=_builder)
        self.assertEqual(self.repo.current_version("c1"), 2)
        self.v1 = self.factory.version_path("c1", 1)
        self.v2 = self.factory.version_path("c1", 2)
        self.assertTrue(self.v1.exists() and self.v2.exists())
        self.reclaimer = VersionReclaimer(self.repo, self.factory,
                                          interval=3600.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPendingReclaim(ReclaimBase):
    """AC-1：切换后旧版本标 pending_reclaim 而非立即删除。"""

    def test_switch_marks_pending_reclaim_not_delete(self):
        self.assertEqual(self.repo.version_status("c1", 1),
                         VER_PENDING_RECLAIM)
        self.assertEqual(self.repo.version_status("c1", 2), VER_ACTIVE)
        self.assertTrue(self.v1.exists(), "旧版本文件不应立即删除")

    def test_set_version_idempotent_keeps_active(self):
        # 重复 set 同一版本不产生新的 pending_reclaim（幂等）
        self.repo.set_version("c1", 2, by="u")
        self.assertEqual(self.repo.version_status("c1", 1),
                         VER_PENDING_RECLAIM)
        self.assertEqual(self.repo.version_status("c1", 2), VER_ACTIVE)


class TestLeaseDeferredReclaim(ReclaimBase):
    """AC-2/AC-3：活跃读者推迟回收，租约清零后回收完成。"""

    def test_active_lease_defers_reclaim(self):
        lease = self.repo.acquire_lease("c1", 1, lease_id="L1", ttl_seconds=60)
        try:
            stats = self.reclaimer.reclaim_once()
            self.assertEqual(stats["deferred"], 1)
            self.assertEqual(stats["reclaimed"], 0)
            self.assertTrue(self.v1.exists(), "有活跃租约不得删除")
            self.assertEqual(self.repo.version_status("c1", 1),
                             VER_PENDING_RECLAIM)
        finally:
            self.repo.release_lease(lease.lease_id)

    def test_expired_lease_purged_then_reclaimed(self):
        self.repo.acquire_lease("c1", 1, lease_id="L2", ttl_seconds=-1)
        stats = self.reclaimer.reclaim_once()  # 过期租约先清理再回收
        self.assertEqual(stats["reclaimed"], 1)
        self.assertEqual(self.repo.active_lease_count("c1", 1), 0)

    def test_reclaim_after_release(self):
        lease = self.repo.acquire_lease("c1", 1, lease_id="L3")
        self.repo.release_lease(lease.lease_id)
        stats = self.reclaimer.reclaim_once()
        self.assertEqual(stats["reclaimed"], 1)


class TestPhysicalReclaim(ReclaimBase):
    """AC-4：删除是物理性的（文件不存在=磁盘释放）。"""

    def test_file_physically_removed(self):
        self.reclaimer.reclaim_once()
        self.assertFalse(self.v1.exists(), "AC-4：v1 文件应实际删除")
        self.assertEqual(self.repo.version_status("c1", 1), VER_RECLAIMED)
        # 磁盘释放的目录级佐证：cases/c1 下不再有 v1.duckdb
        listing = [p.name for p in self.v1.parent.iterdir()]
        self.assertNotIn("v1.duckdb", listing)
        self.assertIn("v2.duckdb", listing)

    def test_reclaim_idempotent(self):
        self.reclaimer.reclaim_once()
        stats = self.reclaimer.reclaim_once()  # 二次扫描无事可做
        self.assertEqual(stats["scanned"], 0)

    def test_ops_event_recorded(self):
        self.reclaimer.reclaim_once()
        ops = self.repo.list_ops(kind="version_reclaimed")
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["case_id"], "c1")


class TestStoreFactoryLease(unittest.TestCase):
    """读者租约随 for_case/close 获取/释放；无租约 meta 优雅降级。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))
        self.repo.set_version("c1", 1, by="u")
        s = self.factory.for_case("c1", mode="write", version=1)
        s.write_conn.execute("CREATE TABLE t (x INTEGER)")
        s.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_read_acquires_lease_close_releases(self):
        store = self.factory.for_case("c1")
        self.assertIsNotNone(store.lease_id)
        self.assertEqual(self.repo.active_lease_count("c1", 1), 1)
        store.close()
        self.assertEqual(self.repo.active_lease_count("c1", 1), 0)

    def test_fake_meta_without_lease_degrades(self):
        class _FakeMeta:
            def current_version(self, case_id: str) -> int:
                return 1

        f = StoreFactory(cases_root=self.tmp / "cases", meta=_FakeMeta())
        store = f.for_case("c1")
        self.assertIsNone(store.lease_id)  # 优雅降级：无租约不炸
        store.close()

    def test_delete_failure_rolls_back_status(self):
        # 模拟删除失败：回收前把文件换成目录（unlink 会抛 OSError）
        self.repo.set_version("c1", 2, by="u")  # v1 → pending_reclaim
        self.v1 = self.factory.version_path("c1", 1)
        self.v1.unlink()
        self.v1.mkdir()  # 同名目录：path.exists()=True 但 unlink 失败
        try:
            stats = self.reclaimer_once()
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(self.repo.version_status("c1", 1),
                             VER_PENDING_RECLAIM, "删除失败应回滚状态")
        finally:
            shutil.rmtree(self.v1, ignore_errors=True)

    def reclaimer_once(self):
        return VersionReclaimer(self.repo, self.factory).reclaim_once()


class TestArchiveCompaction(ReclaimBase):
    """AC-5：ARCHIVED 案件压实——仅保留最终版本。"""

    def test_archive_requires_archived_status(self):
        from server.app.worker.tasks import handle_archive
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_ARCHIVE,
                         idem_key="a0")
        with self.assertRaises(Exception):  # 非 ARCHIVED 状态硬拒
            handle_archive(t, repo=self.repo, factory=self.factory)
        self.assertTrue(self.v1.exists(), "拒绝压实不删文件")

    def test_archived_case_compacts_to_final_version(self):
        self.repo.transition_case("c1", CASE_ARCHIVED, by="u")
        from server.app.worker.tasks import handle_archive
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_ARCHIVE,
                         idem_key="a1")
        result = handle_archive(t, repo=self.repo, factory=self.factory)
        self.assertEqual(result["kept_version"], 2)
        self.assertEqual(result["removed_versions"], [1])
        self.assertFalse(self.v1.exists())
        self.assertTrue(self.v2.exists(), "最终版本必须保留")
        self.assertEqual(self.repo.version_status("c1", 1), VER_RECLAIMED)
        self.assertEqual(self.repo.version_status("c1", 2), VER_ACTIVE)
        ops = self.repo.list_ops(kind="case_archived")
        self.assertEqual(len(ops), 1)


if __name__ == "__main__":
    unittest.main()
