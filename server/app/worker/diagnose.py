"""
server/app/worker/diagnose.py
手动运行诊断（DIAGNOSE 任务）：把 RunHealth 留痕的发起权交给用户，
BUILD/RESCAN 不自动写 run_diagnostic。

对**当前生效版本**补写诊断（run_diagnostic 是运行期表，CREATE IF NOT EXISTS，
不产新版本文件、不动版本指针）：
  1. BUILD 五类留痕：从 artifacts/build_stats_vN.json 读构建期原料，经
     core.run_health.record_build_* 补落（文件缺失=升级前旧版本，跳过）；
  2. 构建后质量门：合规/敏感列/数据新鲜度/单位四扫描（health 写入，与
     run_all 6.6 同口径）；单扫描异常不阻断，落 quality_gate_failed；
  3. diagnostic_run 印记：每次发起落一条 info，零问题也有留痕，
     看板得以显示「治理健康度：正常」而非「尚无诊断留痕」。

重复发起 = 新 run_id 追加一组，看板默认展示最新 run。
"""
from __future__ import annotations

from typing import Any

from core import compliance, data_freshness, sensitive_scan, unit_scan
from core.gateway import OntologyReadGateway
from core.run_health import (
    RunHealth,
    record_build_degraded,
    record_build_dirty,
    record_build_null_identity,
    record_build_quarantine,
    record_clean_stats,
    record_dedup_conflicts,
)

from server.app.build_stats_artifact import load_build_stats
from server.app.worker.tasks import TaskExecError

# (扫描名, 模块)；单个扫描失败不拖垮其余三类
_QUALITY_SCANS = (
    ("compliance", compliance),
    ("sensitive", sensitive_scan),
    ("freshness", data_freshness),
    ("unit", unit_scan),
)


def handle_diagnose(task, *, repo, factory, snapshot_base_for,
                    **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无版本可诊断")

    def progress(pct: float, stage: str, label: str, detail: str) -> None:
        repo.update_progress(task.id, pct=pct, stage=stage,
                             stage_label=label, detail=detail)

    progress(5.0, "prepare", "准备诊断", f"当前生效版本 v{ver}")

    base = snapshot_base_for(case.id)
    # write 打开当前版本：run_diagnostic 为运行期表，诊断不产新版本
    store = factory.for_case(case.id, mode="write", version=ver)
    build_counts: dict[str, int] = {}
    scan_failed: list[str] = []
    try:
        conn = store.write_conn
        rh = RunHealth(conn)

        # 1) BUILD 六类留痕（原料缺失=旧版本，跳过而非报错）
        stats = load_build_stats(factory.case_dir(case.id), ver)
        if stats is not None:
            progress(20.0, "build_stats", "补落构建期留痕",
                     "脏值/缺列/无身份/隔离/清洗剔除/去重冲突")
            build_counts = {
                "dirty": record_build_dirty(conn, stats, run_id=rh.run_id),
                "degraded": record_build_degraded(conn, stats, run_id=rh.run_id),
                "null_identity": record_build_null_identity(conn, stats, run_id=rh.run_id),
                "quarantine": record_build_quarantine(conn, stats, run_id=rh.run_id),
                "clean_stats": record_clean_stats(conn, stats, run_id=rh.run_id),
                "dedup_conflicts": record_dedup_conflicts(conn, stats, run_id=rh.run_id),
            }
        else:
            progress(20.0, "build_stats", "无构建期原料",
                     "v{} 为升级前旧版本，跳过 BUILD 六类留痕".format(ver))

        # 2) 构建后质量门四扫描（与 run_all 6.6 / QUALITY_CHECK 同扫描，
        #    区别：此处 health 写入 run_diagnostic；STALE 不阻断）
        progress(50.0, "quality_gate", "质量门扫描", "合规/敏感/新鲜度/单位")
        gw = OntologyReadGateway(conn, case.pack_id,
                                 base_dir=base, allow_stale=True)
        for name, mod in _QUALITY_SCANS:
            try:
                mod.scan(gw, health=rh, base_dir=base)
            except Exception as e:  # noqa: BLE001 质量门自身故障不阻断
                scan_failed.append(name)
                rh.record("quality_gate_failed", "warning",
                          source=f"quality_gate:{name}",
                          reason=f"{type(e).__name__}: {e}")

        # 3) 运行印记（零问题也有留痕）
        rh.record("diagnostic_run", "info", source="manual_diagnose",
                  reason=(f"手动运行诊断完成（v{ver}；发起人 "
                          f"{task.created_by or '未知'}）"),
                  version=ver, triggered_by=task.created_by,
                  build_stats_present=stats is not None,
                  build_counts=build_counts, scans_failed=scan_failed)
        section = rh.health_section()
        run_id = rh.run_id
    finally:
        store.close()

    repo.record_ops("case_diagnosed", case.id,
                    {"run_id": run_id, "version": ver,
                     "by": task.created_by,
                     "build_stats_present": stats is not None,
                     "build_counts": build_counts,
                     "scans_failed": scan_failed,
                     "诊断总数": section["诊断总数"],
                     "计数": section["计数"]})
    progress(100.0, "done", "诊断完成",
             f"诊断 {section['诊断总数']} 条（{section['status']}）")
    return {"run_id": run_id, "version": ver,
            "build_stats_present": stats is not None,
            "build_counts": build_counts, "scans_failed": scan_failed,
            "health": section}
