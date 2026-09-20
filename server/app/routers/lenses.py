"""
server/app/routers/lenses.py
案件级镜头启停（类比规则工坊 W-014 范式）。

读：GET /cases/{cid}/lenses —— 注册表镜头清单 + 案件生效状态
    （pack_enabled ∧ 案件覆盖；requires_params 标记定向镜头）。
写：PUT /cases/{cid}/lenses/{skill_id} —— enabled 启停开关，落案件快照
    lenses.json（cases/<cid>/lenses.json，原子写）；启停变更入队 RESCAN，
    detect 侧经 case_batch_lens_ids 按快照过滤批量镜头。

边界：
  - 内置五技能（xu_shi/qi_zheng/yong_jian 等，pack_id="_builtin"）不受
    案件启停——它们是 detect 固定编排的核心阶段，非插件镜头（400 拒绝）；
  - 启停文件是案件级运行配置而非本体声明：不进本体指纹/归档，
    审计走 ops_events + 案件审计链（record_config_audit，指纹不变即
    不产生版本沿革行）；
  - 权限同规则工坊：GET 登录即可；PUT 需偏将及以上（clearance>=2）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.pack_loader import BUILTIN_PACK_ID

from server.app.deps import WebContext, get_ctx, get_principal, task_dto
from server.app.envelope import (
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.snapshot_config import (
    atomic_write_json,
    lens_overrides_path,
    load_lens_overrides,
    record_config_audit,
    require_analyst,
)
from server.app.worker.tasks import (
    TASK_LENS_RUN,
    TASK_RESCAN,
    enqueue_task,
)

router = APIRouter(tags=["lenses"])


class LensSwitchIn(BaseModel):
    enabled: bool
    reason: str | None = None  # 变更理由（FE-T-012：落审计链 note）


class LensRunIn(BaseModel):
    params: dict[str, Any] = {}  # 与 params_schema 双向核对（路由预检 + Worker 纵深）
    reason: str | None = None
    auto: bool = False  # 零填写提交：必填参数由 skill_invoke 按 auto_from 推导


def _registry_specs() -> list[Any]:
    """当前注册表全部镜头规格（发现失败回落空清单，读面降级为缺口）。"""
    try:
        from core.registry import get_registry
        return get_registry().all_specs()
    except Exception:
        return []


def _ontology_labels(pack: str = "default") -> dict[str, str]:
    """本体类型名 → 中文标题（objects.json/links.json 的 title）。

    镜头声明的 consumes_objects/produces_dims 都是机器名（person/transfers…），
    直接显示在启停面板上用户看不懂。中文名一律**从本体声明读取**——不硬编码，
    换本体（金融领域 fund/listing…）自动跟随其 title，保证通用性。
    装载失败回落空映射（调用方直接用机器名，不阻断）。
    """
    try:
        from core.ontology_loader import load_pack
        spec = load_pack(pack)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for o in getattr(spec, "objects", None) or []:
        if getattr(o, "name", None) and getattr(o, "title", None):
            out[str(o.name)] = str(o.title)
    for l in getattr(spec, "links", None) or []:
        if getattr(l, "name", None) and getattr(l, "title", None):
            out[str(l.name)] = str(l.title)
    return out


def _dimension_labels(pack: str = "default") -> dict[str, str]:
    """维度标识 → 中文展示名。

    修复：此前维度翻译**复用了 objects/links 的 title 映射**——维度不在
    objects/links 里，必然查不到而回落原名。写中文 name 时碰巧显示正确
    （"关系"→"关系"），但维度 code 化后写 code 会直接露出英文
    （"relation"→"relation"），换本体同理（"trade"→"trade"）。

    维度必须查 dimensions.json 的声明。

    双向建索引（code→name 且 name→name）：兼容存量包里仍写中文 name 的
    produces_dims，两者都能正确显示，不丢信息。
    """
    try:
        from core.ontology_loader import load_dimension_declarations
        decls = load_dimension_declarations(pack)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for d in decls or []:
        code = str(d.get("code") or "").strip()
        name = str(d.get("name") or "").strip()
        if code and name:
            out[code] = name
            out[name] = name   # 兼容旧声明：直接写 name 也能翻译
    return out


def _labels_for(names, labels: dict[str, str]) -> list[str]:
    """机器名 → 中文标题（缺失回落原名，不丢信息）。"""
    return [labels.get(str(n)) or str(n) for n in (names or [])]


@router.get("/cases/{case_id}/lenses")
def list_lenses(case_id: str,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """镜头清单 + 案件生效状态（启停面板数据源）。"""
    _get_owned_case(case_id, p, ctx.cases)
    overrides = load_lens_overrides(ctx.factory.case_dir(case_id))
    labels = _ontology_labels(getattr(ctx, "pack", "default"))
    # 维度翻译须查 dimensions.json，不能复用 objects/links 的 title 映射
    dim_labels = _dimension_labels(getattr(ctx, "pack", "default"))
    items = []
    for spec in sorted(_registry_specs(), key=lambda s: s.skill_id):
        if spec.pack_id == BUILTIN_PACK_ID:
            continue  # 内置五技能是 detect 固定编排，不进启停面板
        case_enabled = overrides.get(spec.skill_id)  # None=未覆盖
        has_required = any(
            pk.get("required") for pk in spec.params_schema.values())
        # 依赖/产出的中文名一律从本体 title 取（不硬编码，换本体自动跟随）
        consumes = list(getattr(spec, "consumes_objects", None) or [])
        dims = list(getattr(spec, "produces_dims", None) or [])
        items.append({
            "skill_id": spec.skill_id,
            "name": spec.name,
            "stage": spec.stage,
            "mode": spec.mode,
            "pack_id": spec.pack_id,
            "pack_enabled": spec.enabled,
            "case_override": case_enabled,
            # 生效值：案件覆盖优先；未覆盖回落包声明
            "enabled": spec.enabled if case_enabled is None else case_enabled,
            "requires_params": has_required,
            # ---- 启停决策所需说明（此前缺失，用户只能凭中文名盲开关）----
            # 用途说明（pack.json 声明；缺失由 UI 按维度/依赖兜底描述）
            "description": getattr(spec, "description", "") or "",
            # 产出维度（停用即丢失这些维度的线索）
            "produces_dims": dims,
            "produces_dims_labels": _labels_for(dims, dim_labels),
            # 数据依赖（缺数据则跑空，供用户判断"该不该开"）
            "consumes_objects": consumes,
            "consumes_labels": _labels_for(consumes, labels),
            # 参数声明（定向调度弹窗表单数据源；与 Function 目录同级的公开元数据）
            "params_schema": spec.params_schema,
        })
    return ok({
        "lenses": items,
        "overrides_file": str(lens_overrides_path(
            ctx.factory.case_dir(case_id))),
    }, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/lenses/{skill_id}")
def switch_lens(case_id: str, skill_id: str, body: LensSwitchIn,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """案件级镜头启停（落 lenses.json + 入队 RESCAN）。"""
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)

    spec = next((s for s in _registry_specs()
                 if s.skill_id == skill_id), None)
    if spec is None:
        raise APIError(ERR_NOT_FOUND, f"镜头不存在：{skill_id}", 404)
    if spec.pack_id == BUILTIN_PACK_ID:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {skill_id} 为内置核心技能，不受案件级启停",
                       400)

    path = lens_overrides_path(ctx.factory.case_dir(case_id))
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}  # 损坏文件重建（读侧容错口径一致）
    lenses = data.get("lenses") if isinstance(data.get("lenses"), dict) else {}
    entry = lenses.get(skill_id)
    already = isinstance(entry, dict) and entry.get("enabled") == bool(
        body.enabled)
    lenses[skill_id] = {"enabled": bool(body.enabled)}
    data["schema_version"] = 1
    data["lenses"] = lenses
    if not already:
        atomic_write_json(path, data)

    ctx.repo.record_ops("lens_switch", case_id,
                        {"skill_id": skill_id, "enabled": bool(body.enabled),
                         "by": p.operator})
    record_config_audit(ctx, case_id, p, "lens_switch",
                        filename="lenses.json", reason=body.reason,
                        summary={"skill_id": skill_id,
                                 "enabled": bool(body.enabled)})

    # 启停影响批量检测结果 → 入队 RESCAN（幂等键随版本）
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_RESCAN,
        params={"skill_id": skill_id, "changed": ["lens_switch"]},
        idem_key=f"rescan:lens:{skill_id}:{ctx.repo.current_version(case_id)}",
        created_by=p.operator)
    return ok({"skill_id": skill_id, "enabled": bool(body.enabled),
               "rescan_task": task_dto(task)},
              data_version=ctx.repo.current_version(case_id))


def _precheck_unknown_only(spec: Any, params: dict) -> None:
    """仅校验「未声明参数」——auto 模式下仍要防越界下发，但允许必填缺失。"""
    unknown = sorted(k for k in params if k not in spec.params_schema)
    if unknown:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {spec.skill_id} 未声明参数：{', '.join(unknown)}",
                       400)


def _precheck_run_params(spec: Any, params: dict) -> None:
    """定向运行参数路由侧预检（与 skill_invoke._validate_params 同口径，
    202 契约下不让必败任务进队列；Worker 侧 _validate_params 纵深兜底）：
    未声明参数拒、必填缺失拒。

    放行自动填充：画布定向调度时前端可"零填写"直接提交（参数由
    /params 接口的推荐值静默带入、或整份缺省）。此时必填缺失不应
    400——改由 skill_invoke 按 auto_from 推导补齐，推导结果记
    param_source 进线索 detail，可审计、可人工覆盖。
    """
    unknown = sorted(k for k in params if k not in spec.params_schema)
    if unknown:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {spec.skill_id} 未声明参数：{', '.join(unknown)}",
                       400)
    if params.pop("_auto", None) is True or params.get("_auto") is True:
        return  # 显式声明"自动填充"：交由 skill_invoke 推导，不预检必填
    missing = sorted(
        k for k, ps in spec.params_schema.items()
        if ps.get("required")
        and (k not in params or params.get(k) in (None, "")))
    if missing:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {spec.skill_id} 缺少必填参数："
                       f"{', '.join(missing)}", 400)


class LensParamsIn(BaseModel):
    """参数候选查询入参（画布上下文驱动，规模可控）。"""
    canvas_nodes: list[str] = []      # 画布当前可见主体（规模主力，典型 20-80）
    selected_node: str | None = None  # 画布选中主体（唯一时前端静默带入）
    keyword: str | None = None        # 候选过多时按需搜索词


@router.post("/cases/{case_id}/lenses/{skill_id}/params")
def lens_param_candidates(case_id: str, skill_id: str, body: LensParamsIn,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """定向镜头参数候选（画布定向调度「自动推荐」数据源）。

    候选按「画布选中 > 画布可见 > 案件登记 > 线索反推 > 语义层截断」排序，
    语义层**不返回全量**（实测真实案件可达 2.5 万主体，全量进下拉会 DOM
    爆炸），超出部分由前端传 keyword 走按需搜索。

    返回每个必填参数的 {candidates, recommended, source, auto_only,
    listable, searchable}，前端据此决定：
      auto_only(候选唯一) → 静默带入不展示；
      listable(≤100)      → 下拉 + 默认选推荐值；
      searchable(超量)    → 搜索框 + 推荐置顶。
    """
    _get_owned_case(case_id, p, ctx.cases)
    spec = next((s for s in _registry_specs()
                 if s.skill_id == skill_id), None)
    if spec is None:
        raise APIError(ERR_NOT_FOUND, f"镜头不存在：{skill_id}", 404)

    try:
        from core.focus import resolve_param_candidates
        from core.store import Store as CoreStore
        # 开库与 lens_run worker 同径（当前生效版本的语义层 obj_*/lnk_*）
        version = ctx.repo.current_version(case_id)
        store = CoreStore(db_path=str(ctx.factory.version_path(case_id, version)))
        try:
            got = resolve_param_candidates(
                spec, store, canvas_nodes=body.canvas_nodes,
                selected_node=body.selected_node, keyword=body.keyword,
                pack=getattr(ctx, "pack", "default"))
        finally:
            try:
                store.close()
            except Exception:
                pass
    except Exception:
        got = {}
    return ok({"skill_id": skill_id,
               "params": {k: v.to_dict() for k, v in got.items()}},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/lenses/{skill_id}/run", status_code=202)
def run_lens(case_id: str, skill_id: str, body: LensRunIn,
             p: Principal = Depends(get_principal),
             ctx: WebContext = Depends(get_ctx)):
    """定向镜头带参调度（202 入队 LENS_RUN；启停面板/画布定向入口）。

    对当前生效版本只读跑单个定向镜头（requires_params），线索落
    lens_runs 补充产物并线进线索读面；不产版本文件。权限/边界同启停：
    偏将及以上；内置技能/包级停用/草案镜头/案件停用一律 400。
    """
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)

    spec = next((s for s in _registry_specs()
                 if s.skill_id == skill_id), None)
    if spec is None:
        raise APIError(ERR_NOT_FOUND, f"镜头不存在：{skill_id}", 404)
    if spec.pack_id == BUILTIN_PACK_ID:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {skill_id} 为内置核心技能，不接受定向调度",
                       400)
    if not spec.enabled:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {skill_id} 包级已停用", 400)
    if spec.mode != "deterministic":
        raise APIError(ERR_VALIDATION,
                       f"镜头 {skill_id} 为草案镜头（mode={spec.mode}），"
                       "产出须经人验，不支持直接调度", 400)
    if load_lens_overrides(ctx.factory.case_dir(case_id)).get(skill_id) is False:
        raise APIError(ERR_VALIDATION,
                       f"镜头 {skill_id} 已在本案件停用（启停面板）", 400)
    # 零填写（auto）：跳过必填预检，由 skill_invoke 按 auto_from 推导补齐
    if not body.auto:
        _precheck_run_params(spec, body.params)
    else:
        _precheck_unknown_only(spec, body.params)

    version = ctx.repo.current_version(case_id)
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_LENS_RUN,
        params={"skill_id": skill_id, "params": body.params, "auto": body.auto,
                "reason": body.reason},
        idem_key=(f"lensrun:{skill_id}:{version}:"
                  f"{json.dumps(body.params, ensure_ascii=False, sort_keys=True, default=str)}"),
        created_by=p.operator)
    return ok({"skill_id": skill_id, "task": task_dto(task)},
              data_version=ctx.repo.current_version(case_id))
