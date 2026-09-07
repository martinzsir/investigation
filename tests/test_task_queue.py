"""
tests/test_task_queue.py
M1 阶段 D/E：版本化文件原子切换（H1~H4）+ 任务队列/Worker（两级并发、
失败重试、幂等入队）。

H1 构建成功版本前进一步且新读者可见；
H2 旧读者持有旧版本文件，切换后新读者读新版、旧读者继续读旧版；
H3 构建失败不切指针，重试耗尽任务 FAILED 且带错误码；
H4 旧版读者与新版构建并发互不阻塞（不同文件 = 不同锁域）。
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

from server.app.cases import CaseService
from server.app.meta.models import (
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    TaskRow,
)
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool
from server.app.worker.tasks import TASK_BUILD, TASK_PING, enqueue_task


def _fake_builder(marker: int, *, fail: bool = False, gate=None):
    """假构建器：在新版本文件里打 build_marker 戳；可注入失败/同步闸门。"""
    def builder(conn, *, pack, base_dir, progress):
        progress(15.0, "fake", "假构建", f"marker={marker}")
        if gate is not None and not gate.wait(120):
            raise TimeoutError("测试闸门 120s 未释放（测试自身异常）")
        if fail:
            raise RuntimeError(f"boom marker={marker}")
        conn.execute("CREATE OR REPLACE TABLE build_marker(v INTEGER)")
        conn.execute("INSERT INTO build_marker VALUES (?)", [marker])
        progress(80.0, "fake", "打戳完成", f"v={marker}")
        return {"marker": marker}
    return builder


class TaskQueueTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.svc.create_case(case_id="c1", name="一号案", created_by="u")
        self.ctx = {
            "factory": self.factory,
            "snapshot_base_for": self.svc.snapshot_ontology_root,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pool(self, **kw):
        return WorkerPool(self.repo, self.ctx, poll_interval=0.02,
                          backoff_base=0.05, **kw)

    def marker(self, case_id="c1", version=None):
        store = self.factory.for_case(case_id, mode="read", version=version)
        try:
            return store.query("SELECT v FROM build_marker")[0]["v"]
        finally:
            store.close()


class TestVersionSwitch(TaskQueueTestBase):
    """H1~H3：版本指针语义。"""

    def test_h1_success_advances_version(self):
        self.ctx["builder"] = _fake_builder(1)
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                         idem_key="b1", created_by="u")
        self.assertTrue(self.pool().run_next())
        done = self.repo.get_task(t.id)
        self.assertEqual(done.status, TASK_SUCCEEDED)
        self.assertEqual(self.repo.current_version("c1"), 1)
        self.assertEqual(self.marker(), 1)
        # 无任务时 run_next 返回 False
        self.assertFalse(self.pool().run_next())

    def test_h2_old_reader_stays_on_old_version(self):
        self.ctx["builder"] = _fake_builder(1)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="b1")
        self.pool().run_next()
        old = self.factory.for_case("c1", mode="read")  # v1 读者
        self.assertEqual(old.version, 1)

        self.ctx["builder"] = _fake_builder(2)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="b2")
        self.pool().run_next()
        self.assertEqual(self.repo.current_version("c1"), 2)

        self.assertEqual(self.marker(version=2), 2)   # 新读者读 v2
        self.assertEqual(old.query("SELECT v FROM build_marker")[0]["v"], 1)
        self.assertEqual(self.factory.reader_count("c1", 1), 1)
        old.close()
        self.assertEqual(self.factory.reader_count("c1", 1), 0)

    def test_h3_failure_keeps_pointer_and_retries(self):
        self.ctx["builder"] = _fake_builder(1, fail=True)
        t = TaskRow(id="t_fail", case_id="c1", task_type=TASK_BUILD,
                    max_retries=1, idem_key="bfail")
        self.repo.create_task(t)
        # 2 次尝试（首次 + 1 次重试）后轮空：退避 0.05s
        self.pool().run_until_drained()
        row = self.repo.get_task("t_fail")
        self.assertEqual(row.status, TASK_FAILED)
        self.assertEqual(row.retry_count, 1)
        self.assertEqual(row.error_code, "TASK_EXEC_ERROR")
        self.assertIn("boom", row.error_message)
        self.assertEqual(self.repo.current_version("c1"), 0)  # 指针不动
        # 失败后恢复：换成功构建器重新入队（新幂等键）
        self.ctx["builder"] = _fake_builder(9)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="bok")
        self.pool().run_until_drained()
        self.assertEqual(self.repo.current_version("c1"), 1)
        self.assertEqual(self.marker(version=1), 9)

    def test_h4_concurrent_readers_vs_build(self):
        self.ctx["builder"] = _fake_builder(1)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="b1")
        self.pool().run_next()

        errors: list[Exception] = []
        seen: list[int] = []

        def reader():
            try:
                store = self.factory.for_case("c1", mode="read")
                seen.append(store.query("SELECT v FROM build_marker")[0]["v"])
                store.close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        # 3 个线程持有 v1，同时第 4 个线程跑 v2 构建
        held = [self.factory.for_case("c1", mode="read") for _ in range(3)]
        build_t = threading.Thread(
            target=lambda: (
                self.ctx.__setitem__("builder", _fake_builder(2)),
                enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                             idem_key="b2"),
                self.pool().run_next()))
        build_t.start()
        build_t.join(10)
        for s in held:
            seen.append(s.query("SELECT v FROM build_marker")[0]["v"])
            s.close()
        for _ in range(3):
            threading.Thread(target=reader).start()
        for th in threading.enumerate():
            if th is not threading.current_thread():
                th.join(5)

        self.assertEqual(errors, [])
        self.assertEqual(self.repo.current_version("c1"), 2)
        self.assertIn(1, seen)  # 旧读者确实读到 v1

    def test_v2_derived_from_v1(self):
        """v2 从 v1 复制派生：v1 的附属表在 v2 仍在（模板/视图沿用语义）。"""
        self.ctx["builder"] = _fake_builder(1)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="b1")
        self.pool().run_next()
        # 直接在 v1 文件加一张附属表（模拟视图/物化表沿用）
        w = self.factory.for_case("c1", mode="write", version=1)
        w.write_conn.execute("CREATE TABLE aux(x INTEGER)")
        w.close()
        self.ctx["builder"] = _fake_builder(2)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="b2")
        self.pool().run_next()
        self.assertEqual(self.marker(version=2), 2)
        r = self.factory.for_case("c1", mode="read", version=2)
        try:
            self.assertEqual(r.query("SELECT COUNT(*) n FROM aux")[0]["n"], 0)
        finally:
            r.close()


class TestQueueSemantics(TaskQueueTestBase):
    def test_ping_task_no_db(self):
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_PING,
                         idem_key="p1")
        self.pool().run_next()
        self.assertEqual(self.repo.get_task(t.id).status, TASK_SUCCEEDED)

    def test_idempotent_enqueue(self):
        t1 = enqueue_task(self.repo, case_id="c1", task_type=TASK_PING,
                          idem_key="same", task_id="t_a")
        t2 = enqueue_task(self.repo, case_id="c1", task_type=TASK_PING,
                          idem_key="same", task_id="t_b")
        self.assertEqual(t1.id, t2.id)
        rows = self.repo.list_tasks(case_id="c1")
        self.assertEqual(len(rows), 1)

    def test_case_level_fifo_and_global_concurrency(self):
        """同案任务串行（案件无 RUNNING 才可认领），异案可并行。"""
        self.svc.create_case(case_id="c2", name="二号案", created_by="u")
        gate = threading.Event()
        self.ctx["builder"] = _fake_builder(1, gate=gate)
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="a1", task_id="ta1")
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="a2", task_id="ta2")  # 同案第二
        enqueue_task(self.repo, case_id="c2", task_type=TASK_BUILD,
                     idem_key="b1", task_id="tb1")  # 异案

        pool = self.pool(max_workers=2)
        pool.start()
        try:
            # 等两条异案任务都被认领（卡在闸门）——用状态轮询替代固定 sleep；
            # 窗口给足 30s（高负载 CI 上 Worker 线程可能迟迟得不到调度）
            deadline = 0
            while (deadline < 600 and not (
                    self.repo.get_task("ta1").status == TASK_RUNNING
                    and self.repo.get_task("tb1").status == TASK_RUNNING)):
                threading.Event().wait(0.05)
                deadline += 1
            self.assertEqual(self.repo.get_task("ta1").status, TASK_RUNNING)
            self.assertEqual(self.repo.get_task("tb1").status, TASK_RUNNING)
            # 同案第二任务必须排队（案件级 FIFO：c1 已有 RUNNING）
            self.assertEqual(self.repo.get_task("ta2").status, TASK_PENDING)
            gate.set()
            drained = 0
            while drained < 600 and not (
                    self.repo.get_task("ta2").status == TASK_SUCCEEDED
                    and self.repo.current_version("c1") == 2
                    and self.repo.current_version("c2") == 1):
                threading.Event().wait(0.05)
                drained += 1
        finally:
            pool.stop()
        self.assertEqual(self.repo.get_task("ta2").status, TASK_SUCCEEDED)
        self.assertEqual(self.repo.current_version("c1"), 2)
        self.assertEqual(self.repo.current_version("c2"), 1)


class TestRealBuildSmoke(TaskQueueTestBase):
    """真实 build_ontology + 模板库冒烟（模板缺失则跳过）。"""

    def test_real_build_from_template(self):
        template = ROOT / "investigation.duckdb"
        if not template.exists():
            self.skipTest(f"模板库不存在：{template}")
        self.ctx["template_db"] = template
        enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                     idem_key="real1")
        # 不注入 builder → 默认 build_ontology（案件快照包）
        self.assertTrue(WorkerPool(self.repo, self.ctx).run_next())
        self.assertEqual(self.repo.current_version("c1"), 1)
        r = self.factory.for_case("c1", mode="read", version=1)
        try:
            tables = {row["table_name"] for row in r.query(
                "SELECT table_name FROM information_schema.tables")}
            self.assertTrue(any(t.startswith("obj_") for t in tables))
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
