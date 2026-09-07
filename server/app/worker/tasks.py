"""
server/app/worker/tasks.py
任务处理器注册表与 BUILD 编排（W-006/W-007）。

BUILD 语义（版本化文件 + 原子切换，H1~H4）：
  1. 目标版本 nxt = current_version + 1，文件 cases/<cid>/v<nxt>.duckdb；
  2. 基线复制：存在当前版 → 复制 vN（保 parquet 相对路径视图/附属表）；
     否则有模板库 → 复制模板；都没有则写连接自动建空库；
  3. write 连接打开新版本文件，跑 builder（默认 build_ontology，
     base_dir 指向案件 pack 快照——W-005 快照隔离）；
  4. **只有构建成功才 set_version 切指针**（H3）：失败时 vN 文件可能残留，
     下次重试先删残留再重建；旧读者始终持有旧版本文件句柄，不受影响（H2/H4）。

本模块不直接 duckdb 开库（grep 门禁）：开库一律走 StoreFactory。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from core.ontology import build_ontology

from server.app.meta.models import TaskRow
from server.app.meta.repo import MetaRepo
from server.app.store import StoreFactory

# 任务类型
TASK_BUILD = "BUILD"
TASK_PING = "PING"


class TaskExecError(RuntimeError):
    """处理器主动抛出的业务失败（带错误码，S4 错误信封用）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


ProgressCb = Callable[[float, str, str, str], None]
BuilderFn = Callable[..., dict[str, Any]]


def default_builder(conn, *, pack: str, base_dir: Path,
                    progress: ProgressCb) -> dict[str, Any]:
    """默认 BUILD 构建器：core build_ontology（案件快照包）。"""
    progress(20.0, "load", "装载本体声明", f"pack={pack}（案件快照）")
    stats = build_ontology(conn, pack=pack, base_dir=base_dir)
    n_obj = len(stats.get("objects", {}))
    n_lnk = len(stats.get("links", {}))
    progress(90.0, "compile", "语义层编译完成",
             f"对象 {n_obj} 类 / 链接 {n_lnk} 类")
    return {k: stats.get(k) for k in ("objects", "links", "skipped")}


def enqueue_task(repo: MetaRepo, *, case_id: str, task_type: str,
                 params: dict[str, Any] | None = None, idem_key: str = "",
                 created_by: str = "", task_id: str | None = None) -> TaskRow:
    """入队（幂等：(case_id, task_type, idem_key) 冲突返回既有行）。"""
    row = TaskRow(
        id=task_id or f"t_{uuid.uuid4().hex[:12]}",
        case_id=case_id, task_type=task_type, params=params or {},
        idem_key=idem_key, created_by=created_by)
    return repo.create_task(row)


def handle_build(task: TaskRow, *, repo: MetaRepo, factory: StoreFactory,
                 snapshot_base_for: Callable[[str], Path],
                 template_db: str | Path | None = None,
                 builder: BuilderFn = default_builder) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")

    cur = repo.current_version(case.id)
    nxt = cur + 1
    target = factory.version_path(case.id, nxt)
    if target.exists():
        target.unlink()  # 上次失败/崩溃残留的孤儿文件，重建前清理

    if cur >= 1:
        prev = factory.version_path(case.id, cur)
        if prev.exists():
            shutil.copy2(prev, target)  # 从当前版派生（视图/附属表沿用）
    elif template_db is not None and Path(template_db).exists():
        shutil.copy2(template_db, target)  # v1 从模板库初始化
    # else：write 打开时自动创建空库文件

    def progress(pct: float, stage: str, label: str, detail: str) -> None:
        repo.update_progress(task.id, pct=pct, stage=stage,
                             stage_label=label, detail=detail)

    progress(5.0, "prepare", "准备构建", f"目标 v{nxt}（基线 v{cur}）")
    store = factory.for_case(case.id, mode="write", version=nxt)
    try:
        result = builder(
            store.write_conn, pack=case.pack_id,
            base_dir=snapshot_base_for(case.id), progress=progress)
    finally:
        store.close()

    # H3：构建成功才切指针（builder 抛异常则指针不动、版本不前进）
    repo.set_version(case.id, nxt, by="worker")
    progress(100.0, "done", "构建完成", f"v{nxt} 已生效")
    return {"version": nxt, "stats": result}


def handle_ping(task: TaskRow, *, repo: MetaRepo, **_: Any) -> dict[str, Any]:
    """心跳/自测任务：不碰案件库，直接成功。"""
    repo.update_progress(task.id, pct=50.0, stage="ping",
                         stage_label="心跳", detail="ping")
    return {"pong": True}


HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    TASK_BUILD: handle_build,
    TASK_PING: handle_ping,
}
