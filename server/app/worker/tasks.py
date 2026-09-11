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

from server.app.meta.models import (
    CASE_ACTIVE,
    CASE_ARCHIVED,
    CASE_DRAFT,
    TaskRow,
    VER_RECLAIMED,
)
from server.app.meta.repo import MetaRepo
from server.app.store import StoreFactory

# 任务类型
TASK_BUILD = "BUILD"
TASK_PING = "PING"
TASK_ARCHIVE = "ARCHIVE"  # W-008 AC-5：已封存案件版本压实
TASK_DISPOSE = "DISPOSE"  # W-020：线索处置快速通道（秒级，不产版本文件，写 state.sqlite）
TASK_RESCAN = "RESCAN"   # W-014 AC-6：规则工坊调参/启停后重跑（MVP=复用 BUILD 编排产新版本）
TASK_IMPORT = "IMPORT"   # W-010/012：数据导入（五格式→冷层 parquet→链式 BUILD）
TASK_REVIEW = "REVIEW"   # W-021：实体人审裁决（合并/驳回，落 state 不产版本）
TASK_EXPORT = "EXPORT"   # W-028：案件包导出（压实 + 审计链冻结 + SHA-256 + zip）
TASK_IMPORT_PACKAGE = "IMPORT_PACKAGE"  # W-029：案件包导入（校验 + init_pack + 登记）
TASK_QUALITY = "QUALITY_CHECK"        # W-P-008：数据质量检查（四扫描汇总落 state，不产版本）
TASK_DIAGNOSE = "DIAGNOSE"            # 手动运行诊断：对当前版本落 run_diagnostic（不随 BUILD 自动）
TASK_DE_RECO = "DE_RECOMMEND"         # W-P-007：数据元智能推荐生成（读暂存件采样，落 state）
TASK_DE_DECIDE = "DE_RECO_DECIDE"     # W-P-007：推荐采纳/驳回裁决（只记 state+审计，不改 bindings）


class TaskExecError(RuntimeError):
    """处理器主动抛出的业务失败（带错误码，S4 错误信封用）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


ProgressCb = Callable[[float, str, str, str], None]
BuilderFn = Callable[..., dict[str, Any]]


def _emit_build_warnings(conn, repo: MetaRepo, case_id: str, version: int,
                         result: Any, progress: ProgressCb) -> Any:
    """把 builder 返回的 build_stats 里的降级/跳提升为可见告警。

    落两处：案件 run_diagnostic（随版本文件，健康度/仪表盘可读）+ 平台
    ops_events(kind=build_degraded)（任务面板可读）；同时回写 result["warnings"]，
    由 handle_build 汇总进返回体。诊断落盘失败只 ops 留痕，不回滚已生效构建。
    """
    if not isinstance(result, dict):
        return result
    stats = result.get("build_stats")
    if not isinstance(stats, dict):
        return result
    warnings = (list(stats.get("degraded") or [])
                + list(stats.get("skipped") or []))
    if not warnings:
        return result
    result["warnings"] = warnings
    repo.record_ops("build_degraded", case_id,
                    {"version": version, "degraded": n_deg, "skipped": n_skip,
                     "warnings": warnings[:20]})
    progress(92.0, "degraded", "构建降级留痕",
             f"{len(warnings)} 项降级/跳过已进诊断与运维事件")
    return result


def default_builder(conn, *, pack: str, base_dir: Path,
                    progress: ProgressCb) -> dict[str, Any]:
    """默认 BUILD 构建器：core build_ontology（案件快照包）。"""
    progress(20.0, "load", "装载本体声明", f"pack={pack}（案件快照）")
    stats = build_ontology(conn, pack=pack, base_dir=base_dir)
    n_obj = len(stats.get("objects", {}))
    n_lnk = len(stats.get("links", {}))
    progress(90.0, "compile", "语义层编译完成",
             f"对象 {n_obj} 类 / 链接 {n_lnk} 类")
    # build_stats：BUILD 诊断原料完整快照（只落 artifacts，不写 run_diagnostic——
    # 诊断由用户手动发起 DIAGNOSE 任务，不自动留痕）
    out = {k: stats.get(k) for k in ("objects", "links", "skipped")}
    out["build_stats"] = stats
    return out


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
                 builder: BuilderFn = default_builder,
                 auto_quality_after_build: bool = True) -> dict[str, Any]:
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
        # W-010/012：导入的冷层 parquet 先 CTAS 为同名源表（无导入则 no-op），
        # bindings.source.table 编译期即可命中
        try:
            from server.app.worker.ingest import mount_case_cold
            mounted = mount_case_cold(store.write_conn, repo, factory,
                                      case.id)
            if mounted:
                progress(10.0, "mount", "挂载冷层数据源",
                         "、".join(mounted))
        except Exception as e:
            raise TaskExecError("COLD_MOUNT_FAILED",
                                f"冷层数据源挂载失败：{e}")
        result = builder(
            store.write_conn, pack=case.pack_id,
            base_dir=snapshot_base_for(case.id), progress=progress)
        # W-030：构件降级/跳过不再静默——过去 build_stats 的 degraded/skipped
        # 只落 artifacts 文件，server 侧无消费方，Web 端 BUILD 显示 SUCCEEDED 却
        # 少数据（demoD：银行流水晚到 5 秒 → person 少 8 人，全程无告警）。
        # 这里落案件 run_diagnostic（随版本）+ 平台 ops_events（UI 可见）。
        result = _emit_build_warnings(store.write_conn, repo, case.id, nxt,
                                      result, progress)
    finally:
        store.close()

    # D-M3-2：线索检测 + 报告产物（cases/<cid>/artifacts/clues_v<nxt>.json，
    # 随版本不可变；线索读面/处置均消费它）。检测失败视同 BUILD 失败：
    # 版本指针不前进，v<nxt> 残留由下次重试清理（H3 同语义）。
    try:
        from server.app.worker.detect import run_detection
        det = run_detection(
            version_file=target, case_dir=factory.case_dir(case.id),
            version=nxt, pack=case.pack_id,
            snapshot_base=snapshot_base_for(case.id))
        progress(98.0, "detect", "线索检测完成",
                 f"产物 {det['clues']} 条线索（规则命中 {det['raw_findings']}）")
    except TaskExecError:
        raise
    except Exception as e:
        raise TaskExecError("DETECTION_FAILED", f"线索检测失败：{e}")

    # H3：构建成功才切指针（builder/检测抛异常则指针不动、版本不前进）
    repo.set_version(case.id, nxt, by="worker")

    # 构建成功后自动将「待建案」迁移为「侦查中」（首次 BUILD 生效后立案）
    if case.status == CASE_DRAFT:
        try:
            repo.transition_case(case.id, CASE_ACTIVE, "worker")
        except Exception as e:  # noqa: BLE001
            repo.record_ops("case_activate_failed", case.id,
                            {"target": CASE_ACTIVE, "error": f"{type(e).__name__}: {e}"})

    # B5：构建成功后补录审计链生命周期事件（失败只 ops 留痕，不回滚版本）
    try:
        from server.app.worker.lifecycle_audit import on_build_succeeded
        stats = result if isinstance(result, dict) else {}
        on_build_succeeded(
            case_dir=factory.case_dir(case.id),
            case_id=case.id, operator="system",
            prev_version=cur, new_version=nxt,
            objects=stats.get("objects", 0),
            links=stats.get("links", 0))
    except Exception as e:  # noqa: BLE001
        repo.record_ops("lifecycle_audit_failed", case.id,
                        {"event": "build_succeeded", "version": nxt,
                         "error": f"{type(e).__name__}: {e}"})

    # 持久化 BUILD 诊断原料（artifacts/build_stats_vN.json），供用户手动发起
    # DIAGNOSE 时补落 run_diagnostic；附属产物失败不回滚已生效版本
    try:
        from server.app.build_stats_artifact import save_build_stats
        saved = save_build_stats(factory.case_dir(case.id), nxt,
                                 result.get("build_stats") if isinstance(result, dict) else None)
        if saved is not None:
            progress(99.0, "diagnose", "诊断原料已留存",
                     "BUILD 未自动诊断；可在仪表盘手动「运行诊断」")
    except Exception as e:  # noqa: BLE001
        repo.record_ops("build_stats_persist_failed", case.id,
                        {"version": nxt, "error": f"{type(e).__name__}: {e}"})

    # v1.3 §1-2：BUILD 成功后自动链式触发语义态质检（双阶段质检：接入态 + 语义态）。
    # 语义态质检要求 ver>=1（set_version 已生效），故须在 BUILD 成功后入队；
    # 若放 import 末尾，此时版本指针未前进，handle_quality 会 NO_VERSION 失败。
    # 失败只 ops 留痕，不回滚已生效版本（质检是只读扫描，不产版本）。
    # auto_quality_after_build 开关：生产默认 True；taskqueue 单元测试用 fake
    # builder 不建 obj_* 表，显式设 False 关闭以保持"BUILD 是终点"的测试契约。
    if auto_quality_after_build:
        try:
            quality = enqueue_task(
                repo, case_id=case.id, task_type=TASK_QUALITY,
                params={"triggered_by": "build", "version": nxt,
                        "operator": task.created_by},
                idem_key=f"quality:build:{case.id}:v{nxt}",
                created_by=task.created_by)
            progress(99.5, "quality_queued", "语义态质检已入队",
                     f"v{nxt} 质检任务 {quality.id}")
        except Exception as e:  # noqa: BLE001
            repo.record_ops("quality_enqueue_failed", case.id,
                            {"version": nxt,
                             "error": f"{type(e).__name__}: {e}"})

    progress(100.0, "done", "构建完成", f"v{nxt} 已生效")
    summary = {k: result.get(k) for k in
               ("objects", "links", "skipped", "warnings")} \
        if isinstance(result, dict) else {}
    return {"version": nxt, "stats": summary}


def handle_ping(task: TaskRow, *, repo: MetaRepo, **_: Any) -> dict[str, Any]:
    """心跳/自测任务：不碰案件库，直接成功。"""
    repo.update_progress(task.id, pct=50.0, stage="ping",
                         stage_label="心跳", detail="ping")
    return {"pong": True}


def handle_archive(task: TaskRow, *, repo: MetaRepo,
                   factory: StoreFactory, **_: Any) -> dict[str, Any]:
    """W-008 AC-5：已封存（ARCHIVED）案件版本压实——仅保留最终版本。

    遍历版本历史：非当前版本的文件一律物理删除、状态置 reclaimed
    （历史无记录但状态为 active/pending_reclaim 的行也一并收敛）；
    当前版本文件与 state.sqlite 不触碰（决策 D1：决策不随重建丢失）。
    """
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    if case.status != CASE_ARCHIVED:
        raise TaskExecError(
            "CASE_NOT_ARCHIVED",
            f"案件状态为 {case.status!r}，仅已封存案件可压实（先归档再压实）")
    cur = repo.current_version(case.id)
    removed: list[int] = []
    for row in repo.list_version_history(case.id):
        if row.version == cur:
            continue  # 最终版本保留
        path = factory.version_path(case.id, row.version)
        if path.exists():
            path.unlink()
            removed.append(row.version)
        if row.status != VER_RECLAIMED:
            repo.mark_version_status(case.id, row.version, VER_RECLAIMED,
                                     by="worker:archive")
    repo.record_ops("case_archived", case.id,
                    {"kept_version": cur, "removed_versions": removed})
    return {"kept_version": cur, "removed_versions": removed}


def _dispose_handler(task, **kw):
    # 惰性导入：dispose.py 反向依赖本模块 TaskExecError，模块底导入避免循环
    from server.app.worker.dispose import handle_dispose
    return handle_dispose(task, **kw)


def _rescan_handler(task, **kw):
    # 惰性导入：rescan.py 复用本模块 handle_build，模块底导入避免循环
    from server.app.worker.rescan import handle_rescan
    return handle_rescan(task, **kw)


def _import_handler(task, **kw):
    # 惰性导入：ingest.py 引用本模块常量/入队，模块底导入避免循环
    from server.app.worker.ingest import handle_import
    return handle_import(task, **kw)


def _review_handler(task, **kw):
    # 惰性导入：review.py 引用本模块常量，模块底导入避免循环
    from server.app.worker.review import handle_review
    return handle_review(task, **kw)


def _export_handler(task, **kw):
    from server.app.worker.package import handle_export
    return handle_export(task, **kw)


def _import_package_handler(task, **kw):
    from server.app.worker.package import handle_import_package
    return handle_import_package(task, **kw)


def _quality_handler(task, **kw):
    from server.app.worker.quality import handle_quality
    return handle_quality(task, **kw)


def _diagnose_handler(task, **kw):
    from server.app.worker.diagnose import handle_diagnose
    return handle_diagnose(task, **kw)


def _de_reco_handler(task, **kw):
    from server.app.worker.recommend import handle_de_reco
    return handle_de_reco(task, **kw)


def _de_decide_handler(task, **kw):
    from server.app.worker.recommend import handle_de_decide
    return handle_de_decide(task, **kw)


HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    TASK_BUILD: handle_build,
    TASK_PING: handle_ping,
    TASK_ARCHIVE: handle_archive,
    TASK_DISPOSE: _dispose_handler,  # W-020 处置快速通道
    TASK_RESCAN: _rescan_handler,    # W-014 AC-6 规则变更重跑
    TASK_IMPORT: _import_handler,    # W-010/012 数据导入
    TASK_REVIEW: _review_handler,    # W-021 实体裁决
    TASK_EXPORT: _export_handler,    # W-028 案件包导出
    TASK_IMPORT_PACKAGE: _import_package_handler,  # W-029 案件包导入
    TASK_QUALITY: _quality_handler,         # W-P-008 数据质量检查
    TASK_DIAGNOSE: _diagnose_handler,       # 手动运行诊断（run_diagnostic 留痕）
    TASK_DE_RECO: _de_reco_handler,         # W-P-007 数据元推荐生成
    TASK_DE_DECIDE: _de_decide_handler,     # W-P-007 推荐裁决
}
