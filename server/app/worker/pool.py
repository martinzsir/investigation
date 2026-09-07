"""
server/app/worker/pool.py
Worker 池（W-006）：两级队列 + 失败重试 + 进度心跳。

- 全局并发：max_workers 个守护线程，信号量等效（线程数即上限）；
- 案件级串行：list_leaseable 只返回"所属案件当前无 RUNNING"的 PENDING，
  同案件任务 FIFO 不并行；
- 失败重试：retry_count < max_retries 时按指数退避重新排队
  （退避时刻仅存内存，Worker 重启退避归零，M1 可接受）；
  耗尽则 FAILED 终态，错误码/消息落 tasks 表；
- run_next() 单步执行一条任务（确定性，测试用）；start()/stop() 跑常驻循环。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from server.app.meta.models import TaskRow
from server.app.meta.repo import MetaRepo
from server.app.worker.tasks import HANDLERS, TaskExecError


class WorkerPool:
    def __init__(self, repo: MetaRepo, handler_ctx: dict[str, Any], *,
                 handlers: dict[str, Callable[..., dict[str, Any]]] | None = None,
                 max_workers: int = 2, poll_interval: float = 0.1,
                 backoff_base: float = 0.2):
        self.repo = repo
        self.handler_ctx = handler_ctx
        self.handlers = handlers or HANDLERS
        self.max_workers = max(1, int(max_workers))
        self.poll_interval = poll_interval
        self.backoff_base = backoff_base
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._backoff_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_heartbeat: str = ""

    # ---- 单步（确定性，测试/CLI --once 用）----
    def run_next(self) -> bool:
        """认领并执行一条可认领任务；无任务返回 False。"""
        now = time.monotonic()
        for task in self.repo.list_leaseable():
            with self._lock:
                until = self._backoff_until.get(task.id, 0.0)
            if until > now:
                continue  # 退避未到
            if not self.repo.claim_task(task.id):
                continue  # 被别的 Worker 抢走
            self._execute(task)
            return True
        return False

    def run_until_drained(self, max_idle_rounds: int = 25) -> int:
        """跑到连续 max_idle_rounds 轮无任务为止（测试用，同步）。"""
        done = 0
        idle = 0
        while idle < max_idle_rounds:
            if self.run_next():
                done += 1
                idle = 0
            else:
                idle += 1
                time.sleep(self.poll_interval)
        return done

    # ---- 常驻循环 ----
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self.max_workers):
            t = threading.Thread(target=self._loop, name=f"sunzi-worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.run_next():
                self._stop.wait(self.poll_interval)

    # ---- 执行与重试 ----
    def _execute(self, task: TaskRow) -> None:
        handler = self.handlers.get(task.task_type)
        if handler is None:
            self._fail(task, "UNKNOWN_TASK_TYPE",
                       f"未知任务类型：{task.task_type}")
            return
        try:
            handler(task, repo=self.repo, **self.handler_ctx)
        except TaskExecError as e:
            self._fail(task, e.code, e.message)
        except Exception as e:  # noqa: BLE001 —— Worker 不允许因任务异常退出
            self._fail(task, "TASK_EXEC_ERROR",
                       f"{type(e).__name__}: {e}")
        else:
            self.repo.complete_task(task.id)

    def _fail(self, task: TaskRow, code: str, message: str) -> None:
        fresh = self.repo.get_task(task.id)
        retries = fresh.retry_count if fresh else task.retry_count
        cap = fresh.max_retries if fresh else task.max_retries
        if retries < cap:
            # Worker 内部重试不耗幂等键：沿用同一 task_id，仅递增 retry_count
            self.repo.requeue_task(task.id, retries + 1)
            with self._lock:
                self._backoff_until[task.id] = (
                    time.monotonic() + self.backoff_base * (2 ** retries))
        else:
            self.repo.fail_task(task.id, error_code=code, error_message=message)
