"""
tests/test_meta_store.py
M1 阶段 B：元数据层 SQLite-WAL（W-004）。

验收点：
  - WAL 并发：多线程读 + 单写互不阻塞、无锁错误；
  - 案件状态机：合法迁移通过、非法迁移硬拒；
  - 任务幂等：同 (case_id, task_type, idem_key) 重复入队返回既有行；
  - 原子认领：两线程抢同一任务仅一方成功；案件级 FIFO（有 RUNNING 的
    案件 PENDING 不被 leaseable）；
  - 版本指针：set_version upsert 并同步 cases.current_version。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.meta.models import (
    CASE_ACTIVE,
    CASE_ARCHIVED,
    CASE_CLOSED,
    CASE_DRAFT,
    CaseRecord,
    IllegalTransition,
    PackSnapshot,
    Session,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    TaskRow,
    User,
)
from server.app.meta.repo_sqlite import SqliteMetaRepo


def _task(tid: str, case: str = "c1", ttype: str = "BUILD",
          key: str = "", by: str = "tester") -> TaskRow:
    return TaskRow(id=tid, case_id=case, task_type=ttype, idem_key=key,
                   created_by=by)


class TestWALConcurrency(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))
        for i in range(20):
            self.repo.create_task(_task(f"t{i:02d}"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_readers_with_writer(self):
        errors: list[Exception] = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    rows = self.repo.list_tasks()
                    self.assertGreaterEqual(len(rows), 20)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def writer():
            try:
                for i in range(30):
                    self.repo.update_progress(
                        "t00", pct=float(i), stage="run",
                        stage_label="构建中", detail=f"step {i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads.append(threading.Thread(target=writer))
        for t in threads:
            t.start()
        writer_thread = threads[-1]
        writer_thread.join()
        stop.set()
        for t in threads[:-1]:
            t.join()
        self.assertEqual(errors, [], f"WAL 并发错误：{errors}")
        self.assertEqual(self.repo.get_task("t00").progress_pct, 29.0)


class TestCaseStateMachine(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legal_transitions(self):
        c = self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        self.assertEqual(c.status, CASE_ACTIVE)
        c = self.repo.transition_case("c1", CASE_CLOSED, by="u")
        self.assertEqual(c.status, CASE_CLOSED)
        c = self.repo.transition_case("c1", CASE_ARCHIVED, by="u")
        self.assertEqual(c.status, CASE_ARCHIVED)

    def test_illegal_transitions_rejected(self):
        # 待建案 → 已结案（越级）非法
        with self.assertRaises(IllegalTransition):
            self.repo.transition_case("c1", CASE_CLOSED, by="u")
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        # 侦查中 → 待建案（回退）非法
        with self.assertRaises(IllegalTransition):
            self.repo.transition_case("c1", CASE_DRAFT, by="u")
        # 未知状态非法
        with self.assertRaises(IllegalTransition):
            self.repo.transition_case("c1", "不存在状态", by="u")
        # 已封存是吸收态：封存后再迁移非法
        self.repo.transition_case("c1", CASE_CLOSED, by="u")
        self.repo.transition_case("c1", CASE_ARCHIVED, by="u")
        with self.assertRaises(IllegalTransition):
            self.repo.transition_case("c1", CASE_ACTIVE, by="u")

    def test_transition_missing_case(self):
        with self.assertRaises(FileNotFoundError):
            self.repo.transition_case("ghost", CASE_ACTIVE, by="u")

    def test_tenant_isolation_in_list(self):
        self.repo.create_case(CaseRecord(id="c2", tenant_id="t2", name="乙"))
        t1 = self.repo.list_cases("t1")
        self.assertEqual([c.id for c in t1], ["c1"])
        self.assertEqual(
            sorted(c.id for c in self.repo.list_cases()), ["c1", "c2"])


class TestUsersAndSessions(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_crud(self):
        u = User(operator="王检", password_hash="h", salt="s", role="主办",
                 clearance=3, tenant_id="t1")
        self.repo.create_user(u)
        got = self.repo.get_user("王检")
        self.assertEqual(got.role, "主办")
        self.repo.set_user_status("王检", "disabled")
        self.assertEqual(self.repo.get_user("王检").status, "disabled")
        with self.assertRaises(Exception):
            self.repo.create_user(u)  # 主键冲突

    def test_session_lifecycle(self):
        s = Session(token="tok-1", operator="王检",
                     created_at="2026-09-07T10:00:00",
                     expires_at="2026-09-07T18:00:00")
        self.repo.create_session(s)
        self.assertEqual(self.repo.get_session("tok-1").operator, "王检")
        self.repo.revoke_session("tok-1")
        self.assertEqual(self.repo.get_session("tok-1").revoked, 1)
        n = self.repo.purge_expired_sessions("2026-09-07T12:00:00")
        self.assertEqual(n, 1)
        self.assertIsNone(self.repo.get_session("tok-1"))


class TestTaskLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))
        self.repo.create_case(CaseRecord(id="c2", tenant_id="t1", name="乙"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotent_enqueue(self):
        t1 = self.repo.create_task(_task("t1", key="key-1"))
        t2 = self.repo.create_task(_task("t2", key="key-1"))  # 同键
        self.assertEqual(t1.id, t2.id)
        self.assertEqual(len(self.repo.list_tasks(case_id="c1")), 1)
        # 不同键可共存
        t3 = self.repo.create_task(_task("t3", key="key-2"))
        self.assertNotEqual(t1.id, t3.id)
        # 无键任务不冲突
        self.repo.create_task(_task("t4", key=""))
        self.repo.create_task(_task("t5", key=""))

    def test_claim_atomic(self):
        self.repo.create_task(_task("t1"))
        results: list[bool] = []

        def claim():
            results.append(self.repo.claim_task("t1"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.repo.get_task("t1").status, TASK_RUNNING)

    def test_leaseable_case_fifo(self):
        # c1 有 RUNNING → c1 的 PENDING 不被 leaseable；c2 的优先
        self.repo.create_task(_task("a1", case="c1"))
        self.repo.create_task(_task("a2", case="c1"))
        self.assertTrue(self.repo.claim_task("a1"))  # c1 进入 RUNNING
        self.repo.create_task(_task("b1", case="c2"))
        lease = [t.id for t in self.repo.list_leaseable()]
        self.assertEqual(lease, ["b1"])  # c1 的 a2 被案件级 FIFO 压后
        # c1 任务完成后 a2 才可认领
        self.repo.complete_task("a1")
        lease = [t.id for t in self.repo.list_leaseable()]
        self.assertEqual(lease, ["a2", "b1"])  # 同 FIFO：a2 创建早于 b1

    def test_progress_complete_fail_requeue(self):
        self.repo.create_task(_task("t1"))
        self.repo.claim_task("t1")
        self.repo.update_progress("t1", pct=50, stage="build",
                                  stage_label="构建语义层", detail="obj 12/20")
        t = self.repo.get_task("t1")
        self.assertEqual(t.progress_pct, 50.0)
        self.repo.fail_task("t1", error_code="BUILD_FAILED",
                            error_message="boom")
        self.assertEqual(self.repo.get_task("t1").status, TASK_FAILED)
        self.repo.requeue_task("t1", retry_count=1)
        t = self.repo.get_task("t1")
        self.assertEqual(t.status, TASK_PENDING)
        self.assertEqual(t.retry_count, 1)
        self.assertEqual(t.error_code, "")
        self.repo.claim_task("t1")
        self.repo.complete_task("t1")
        self.assertEqual(self.repo.get_task("t1").status, TASK_SUCCEEDED)
        self.assertEqual(self.repo.get_task("t1").progress_pct, 100.0)


class TestVersionPointer(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_version_default_zero(self):
        self.assertEqual(self.repo.current_version("c1"), 0)
        self.assertEqual(self.repo.get_case("c1").current_version, 0)

    def test_set_version_upsert_and_sync(self):
        self.repo.set_version("c1", 1, by="worker")
        self.assertEqual(self.repo.current_version("c1"), 1)
        self.assertEqual(self.repo.get_case("c1").current_version, 1)
        self.repo.set_version("c1", 2, by="worker")
        self.assertEqual(self.repo.current_version("c1"), 2)
        self.assertEqual(self.repo.get_case("c1").current_version, 2)

    def test_pack_snapshot(self):
        snap = PackSnapshot(case_id="c1", pack_id="default",
                            version="2026.9.0-46req",
                            snapshot_path="cases/c1/pack_snapshot")
        self.repo.create_pack_snapshot(snap)
        got = self.repo.get_pack_snapshot("c1")
        self.assertEqual(got.pack_id, "default")
        self.assertTrue(got.locked_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
