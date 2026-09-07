"""
server/app/worker/reclaim.py
版本延迟回收器（W-008）。

- 只回收 case_version_history 中 status=pending_reclaim 的版本：
  租约校验与状态翻转（claim_version_reclaim）在同一 meta 事务内完成，
  删除文件在事务外——删除失败回滚状态，下周期幂等重试；
- 活跃读者（case_reader_lease 未过期）使该版本推迟回收（AC-2）；
  过期租约（API 进程崩溃的僵尸读者）先经 purge_expired_leases 清理；
- 动作全部落 ops_events（仪表盘 ops 摘要 / W-009 AC-5 同源）；
- dry_run 模式：只登记不删除（孤儿扫描部署提示同款语义）。

崩溃一致性：状态已翻转 reclaimed 但文件未删（进程被杀）→ 残留文件
由孤儿扫描（orphan_scan）收口——白名单只保护 active/pending_reclaim。
"""
from __future__ import annotations

import threading

from server.app.meta.models import (
    VER_PENDING_RECLAIM,
    VER_RECLAIMED,
    VersionStatusRow,
)
from server.app.store import StoreFactory


class VersionReclaimer:
    def __init__(self, repo, factory: StoreFactory, *,
                 interval: float = 60.0, dry_run: bool = False):
        self.repo = repo
        self.factory = factory
        self.interval = float(interval)
        self.dry_run = bool(dry_run)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 单步（确定性，测试/CLI 用）----
    def reclaim_once(self) -> dict:
        """扫描一轮 pending_reclaim 版本并回收；返回统计。"""
        stats = {"scanned": 0, "reclaimed": 0, "deferred": 0, "failed": 0}
        self.repo.purge_expired_leases()
        pending: list[VersionStatusRow] = self.repo.list_versions_by_status(
            VER_PENDING_RECLAIM)
        for row in pending:
            stats["scanned"] += 1
            if self.dry_run:
                self.repo.record_ops("version_reclaim_dryrun", row.case_id,
                                     {"version": row.version})
                continue
            # 原子认定：无活跃租约才翻转状态（AC-2 的守门逻辑在 SQL 事务内）
            if not self.repo.claim_version_reclaim(row.case_id, row.version):
                stats["deferred"] += 1
                continue
            path = self.factory.version_path(row.case_id, row.version)
            try:
                if path.exists():
                    path.unlink()  # AC-4：磁盘空间实际释放
                self.repo.record_ops("version_reclaimed", row.case_id,
                                     {"version": row.version,
                                      "path": str(path)})
                stats["reclaimed"] += 1
            except OSError as e:
                # 删除失败（如 Windows 句柄占用）：回滚状态，下周期重试
                self.repo.mark_version_status(
                    row.case_id, row.version, VER_PENDING_RECLAIM)
                self.repo.record_ops("version_reclaim_failed", row.case_id,
                                     {"version": row.version,
                                      "error": str(e)[:200]})
                stats["failed"] += 1
        return stats

    def pending_count(self) -> int:
        return len(self.repo.list_versions_by_status(VER_PENDING_RECLAIM))

    # ---- 常驻循环 ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, name="sunzi-reclaimer",
                             daemon=True)
        t.start()
        self._thread = t

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reclaim_once()
            except Exception:  # noqa: BLE001 —— 常驻线程不允许因异常退出
                pass
            self._stop.wait(self.interval)
