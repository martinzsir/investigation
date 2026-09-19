"""
server/app/worker/lens_run.py
TASK_LENS_RUN：画布定向镜头带参调度（启停文件 requires_params 预留的定向入口）。

  路由 POST /cases/{cid}/lenses/{skill_id}/run 202 入队 → 本处理器对当前
  生效版本库跑单个定向镜头（requires_params，如 relation_neighborhood 的
  target_subject / timeline_cross_collision 的 project）：

    - 只读语义：镜头编排走 skill_invoke（scope_reads + policies 属性遮蔽），
      开库与 detect.py 同径（core Store 懒连接，消费 obj_*/lnk_* 语义层），
      不写语义层、不产版本文件、不推进版本指针；
    - 参数核对：skill_invoke._validate_params 双向核对（未声明/必填缺失
      ValueError 硬失败 → LENS_PARAM_INVALID；路由侧已同步预检，此处纵深
      兜底），镜头运行期数据异常由 skill_invoke 隔离（degraded 留痕不炸任务）；
    - 产出：LineageClue 列表落 cases/<cid>/artifacts/lens_runs/v{N}/
      {run_id}.json 补充产物（主产物 D-M3-2 不可变），线索读面
      （clues_view._load_raw）自动并线；定向线索挂产生它的版本，
      RESCAN 版本前进后随旧版本自然失效（按需重跑）；
    - 案件启停（lenses.json）对定向调度同样生效：案件停用的镜头拒绝
      运行（与 detect 批量过滤同一份快照口径，case_batch_lens_ids）。
"""
from __future__ import annotations

import uuid
from typing import Any

from core.pack_loader import BUILTIN_PACK_ID
from core.registry import get_registry, skill_invoke
from core.store import Store as CoreStore

from server.app.clues_artifact import save_lens_run
from server.app.snapshot_config import load_lens_overrides
from server.app.worker.tasks import TaskExecError

# 导入即注册五技能到 DEFAULT_REGISTRY（register_all 幂等）
from skills import registry_bootstrap  # noqa: F401


def handle_lens_run(task, *, repo, factory, **_: Any) -> dict:
    """对当前生效版本跑单个定向镜头并落补充产物。"""
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    version = repo.current_version(case.id)
    if version < 1:
        raise TaskExecError("NO_VERSION",
                            "案件尚无生效版本（先运行 BUILD/RESCAN）")

    p = task.params or {}
    sid = str(p.get("skill_id") or "")
    lens_params = p.get("params") if isinstance(p.get("params"), dict) else {}
    if not sid:
        raise TaskExecError("LENS_PARAM_INVALID", "缺少 skill_id")

    reg = get_registry()
    if sid not in reg:
        raise TaskExecError("LENS_NOT_FOUND", f"镜头不存在：{sid}")
    spec = reg.skill(sid)
    if spec.pack_id == BUILTIN_PACK_ID:
        raise TaskExecError("LENS_BUILTIN_FORBIDDEN",
                            f"镜头 {sid} 为内置核心技能，不接受定向调度")
    if not spec.enabled:
        raise TaskExecError("LENS_DISABLED", f"镜头 {sid} 包级已停用")
    if spec.mode != "deterministic":
        raise TaskExecError(
            "LENS_MODE_UNSUPPORTED",
            f"镜头 {sid} mode={spec.mode}，草案产出须人验，不支持直接调度")
    if load_lens_overrides(factory.case_dir(case.id)).get(sid) is False:
        raise TaskExecError("LENS_CASE_DISABLED",
                            f"镜头 {sid} 已在本案件停用（启停面板）")

    det = CoreStore(db_path=str(factory.version_path(case.id, version)))
    try:
        ctx: dict = {}
        try:
            clues = skill_invoke(reg, sid, store=det, ctx=ctx,
                                 params=lens_params)
        except ValueError as e:
            # _validate_params 双向核对硬失败（未声明参数/必填缺失）
            raise TaskExecError("LENS_PARAM_INVALID", str(e))
    finally:
        det.close()

    run_id = f"lensrun_{uuid.uuid4().hex[:12]}"
    path = save_lens_run(factory.case_dir(case.id), version,
                         run_id=run_id, skill_id=sid, params=lens_params,
                         operator=task.created_by, clues=clues)
    degraded = [str(d.get("skill_id")) for d in ctx.get("degraded", [])
                if isinstance(d, dict)]
    repo.record_ops("lens_run", case.id,
                    {"run_id": run_id, "skill_id": sid, "version": version,
                     "params": lens_params, "clues": len(clues),
                     "degraded": degraded, "artifact": str(path),
                     "triggered_by": task.created_by})
    return {"run_id": run_id, "version": version, "clues": len(clues),
            "artifact": str(path), "degraded": degraded}
