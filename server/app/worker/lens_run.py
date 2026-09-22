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

from core.observation import observation_from_clue
from server.app.clues_artifact import (save_directed_observations,
                                       save_lens_run)
from server.app.snapshot_config import load_lens_overrides
from server.app.worker.tasks import TaskExecError

# 导入即注册五技能到 DEFAULT_REGISTRY（register_all 幂等）
from skills import registry_bootstrap  # noqa: F401


def _lens_label(skill_id: str, pack_id: str) -> str:
    """镜头中文名（本体声明；取不到回落 skill_id，不硬编码中文）。"""
    try:
        m = __import__("server.app.worker.detect",
                       fromlist=["_lens_name_labels"])
        fn = getattr(m, "_lens_name_labels", None)
        if fn:
            return (fn(pack_id, None) or {}).get(skill_id, skill_id)
    except Exception:
        pass
    return skill_id


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

    run_id = f"lensrun_{uuid.uuid4().hex[:12]}"
    # origin 提前解析：观察要携带发起上下文，结果才能回到发起画布。
    # 路由侧只保留已知键并强制 clue_id 为字符串（lenses.py run 端点）。
    origin = p.get("origin") if isinstance(p.get("origin"), dict) else None
    observations: list = []

    det = CoreStore(db_path=str(factory.version_path(case.id, version)))
    try:
        ctx: dict = {}
        # 零填写（auto）/必填缺失：按 auto_from 推导补齐后再跑。
        # 先补齐再落盘，保证 lens_runs 产物与 ops 事件记录的是**实际使用的
        # 参数**（而非空壳），自动推荐了什么依然可审计。
        #
        # 触发补齐的两种情况：
        #   ① 必填参数缺失 → auto_fill_params 推导整组参数
        #   ② 主体参数已填但缺 target_type → 同名主体（如 张卫国 同在
        #      person/account）会让 resolve_subject 抛 ValueError → 镜头被
        #      隔离成 0 线索。此时探测真实类型消歧，不必让用户显式填写。
        try:
            schema = spec.params_schema or {}
            missing = any(
                (ps or {}).get("required") and lens_params.get(k) in (None, "")
                for k, ps in schema.items())
            # 主体→消歧类型 参数对（单主体 / 双主体）
            _PAIRS = (("target_subject", "target_type"),
                      ("subject_a", "target_type_a"),
                      ("subject_b", "target_type_b"))
            needs_disambig = (not missing) and any(
                subj_key in lens_params and lens_params.get(subj_key)
                and type_key in schema and not lens_params.get(type_key)
                for subj_key, type_key in _PAIRS)

            if missing:
                from core.focus import auto_fill_params
                combos, _src = auto_fill_params(spec, det, ctx)
                if combos:
                    filled = {k: v for k, v in combos[0].items()
                              if k != "_param_source"}
                    for k, v in filled.items():
                        lens_params.setdefault(k, v)
            elif needs_disambig:
                from core.focus import _probe_type
                for subj_key, type_key in _PAIRS:
                    if (subj_key in lens_params and lens_params.get(subj_key)
                            and type_key in schema
                            and not lens_params.get(type_key)):
                        t = _probe_type(det, lens_params[subj_key],
                                        pack=spec.pack_id)
                        if t and t != "auto":
                            lens_params[type_key] = t
            clues = skill_invoke(reg, sid, store=det, ctx=ctx,
                                 params=lens_params)
            # 定向产出统一为**观察**，与批量同口径：镜头无常态基线，只摆出
            # 结构、不下异常判断。此前定向产 LineageClue 并线进线索清单，
            # 等于按**触发方式**（而非产出性质）决定它是命题还是证据——
            # 同一镜头批量时是观察、手跑时是线索，模型不自洽。
            observations = [
                observation_from_clue(c, source="directed", origin=origin,
                                      run_id=run_id,
                                      operator=task.created_by,
                                      version=version)
                for c in (clues or [])]
            for o in observations:
                o.lens_name = _lens_label(o.skill_id, spec.pack_id)
        except ValueError as e:
            # _validate_params 双向核对硬失败（未声明参数/必填缺失）
            raise TaskExecError("LENS_PARAM_INVALID", str(e))
    finally:
        det.close()

    # 定向观察**案件级持久化**（artifacts/directed_observations.json，不挂
    # 版本）：正兵显式发起的研判动作，RESCAN 版本前进不得删除——否则他刚
    # 深挖完、一次重扫就没了。批量观察仍随版本重算（可复现、无需留手）。
    if observations:
        save_directed_observations(factory.case_dir(case.id), observations)
    path = save_lens_run(factory.case_dir(case.id), version,
                         run_id=run_id, skill_id=sid, params=lens_params,
                         operator=task.created_by, clues=[],
                         observations=observations, origin=origin)
    degraded = [str(d.get("skill_id")) for d in ctx.get("degraded", [])
                if isinstance(d, dict)]
    repo.record_ops("lens_run", case.id,
                    {"run_id": run_id, "skill_id": sid, "version": version,
                     "params": lens_params, "observations": len(observations),
                     "observation_ids": [o.observation_id
                                         for o in observations],
                     "degraded": degraded, "artifact": str(path),
                     "triggered_by": task.created_by})
    return {"run_id": run_id, "version": version,
            "observations": len(observations),
            "observation_ids": [o.observation_id for o in observations],
            "artifact": str(path), "degraded": degraded}
