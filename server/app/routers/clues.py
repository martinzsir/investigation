"""
server/app/routers/clues.py
W-019/020 线索与处置端点。

POST /cases/{cid}/clues/{clue_id}/actions 🔒：处置动作入队 DISPOSE 快速通道
（API 不直接写 state.sqlite，backend_api.md 2.4：业务写仍入队、FIFO、不产
版本文件）。会话快照（operator/role/clearance）随任务行，operator 强制取
会话；file 前置校验（human 角色 + legal_basis），core ActionExecutor 四步
校验在 Worker 兜底（纵深防御，两处红线语义一致）。

GET 列表/详情/suppressed（W-019）：读产物 artifact + state 状态真值，
只读不触发任务（AC-6）；内间线索秩级过滤（REQ-011 延续）。
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from core.verify_machine import REQUEST_STATUSES, VERIFY_STATUSES
from core.llm.draft_verify import draft_verify_items

from core.access import AccessContext, can_see_jian_types
from server.app import clues_view, ontology_meta
from server.app.verify_functions_map import resolve_replay_mapping
from server.app.deps import (
    WebContext,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import (
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.backend import open_local_conn
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TASK_DISPOSE, TASK_VERIFY, enqueue_task

router = APIRouter(tags=["clues"])
logger = logging.getLogger(__name__)


def _aggregate_cross_rows(ctx: WebContext, case_id: str, pack_id: str,
                          version: int, base_dir) -> list[dict] | None:
    """方案 B：只读开版本库执行 jian_cross_level，返回结构化命中间行。

    供旧版用间聚合线索（artifact 无行集）回填表级汇总行。任何失败
    （版本文件缺失/语义层未建/包装载失败）优雅降级为 None——
    读面不炸，聚合线索回落 a 卡留痕。pack 经案件快照基目录装载
    （与 worker verify 同纪律，不用共享 ontology/）。
    """
    try:
        with ctx.factory.for_case(case_id, mode="read",
                                  version=version) as ro:
            from core.functions import FunctionExecutor
            out = FunctionExecutor(
                ro, pack=pack_id, base_dir=base_dir
            ).invoke("jian_cross_level")
        return (out.get("result") or {}).get("rows")
    except Exception:
        logger.warning(
            "jian_cross_level 聚合回填不可用 case=%s v=%s",
            case_id, version, exc_info=True)
        return None

# REQ-V-005：核查项读面投影（剔除 case_id/clue_id/item_key 内部列）
_VERIFY_ITEM_FIELDS = (
    "item_id", "kind", "text", "origin", "status", "conclusion",
    "operator", "updated_at", "channel", "ref_function",
    "external", "falsification", "replay",
)

_EMPTY_VERIFY_PROGRESS = {
    "total": 0, "concluded": 0, "pending": 0,
    "suggested": 0, "ignored": 0, "by_status": {},
}


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _verify_items_payload(state: StateStore | None, clue_id: str) -> dict:
    """state 不存在（未 BUILD/未打开过工作区）→ available:false 空清单。

    纯读 state 真值；REQ-V-002 惰性供给在线索详情 assemble_detail 触发，
    本端点不重跑 evidence 构建（读面保持轻量、不创建 state.sqlite）。
    REQ-V-011：每项回显已挂接书证（按 item_id 反查，未挂接不进详情）。
    """
    if state is None:
        return {"items": [], "progress": dict(_EMPTY_VERIFY_PROGRESS),
                "available": False}
    items = [{k: it.get(k) for k in _VERIFY_ITEM_FIELDS}
             for it in state.list_verify_items(clue_id)]
    ev_map: dict[str, list[dict]] = {}
    for r in state.list_evidence(clue_id):
        owner = r.get("item_id")
        if owner:
            ev_map.setdefault(owner, []).append({
                "material_id": r["material_id"],
                "orig_name": r["orig_name"],
                "material_type": r["material_type"],
                "uploaded_at": r["uploaded_at"],
            })
    for it in items:
        it["evidence"] = ev_map.get(it["item_id"], [])
    return {"items": items,
            "progress": state.verify_progress(clue_id),
            "available": True}


def _read_state(factory, case_id: str):
    """只读打开 state.sqlite（不存在则 None，读面不创建文件）。"""
    path = factory.case_dir(case_id) / "state.sqlite"
    if not path.exists():
        return None, {}
    st = StateStore(case_id, path)
    return st, st.status_map()


class ClueActionIn(BaseModel):
    action: str
    note: str = ""
    reason: str = ""
    legal_basis: str = ""
    idem_key: str = ""


@router.post("/cases/{case_id}/clues/{clue_id}/actions", status_code=202)
def clue_action(case_id: str, clue_id: str, body: ClueActionIn,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """W-020：处置动作入队（202 + task_id；进度走任务 SSE）。"""
    _get_owned_case(case_id, p, ctx.cases)  # 跨租户 404
    case = ctx.repo.get_case(case_id)
    # R6：合法动作来自案件包 actions 声明（set_clue_status 类），不硬编码
    legal_actions = ontology_meta.dispose_action_names(
        case.pack_id, ctx.cases.snapshot_ontology_root(case_id))
    action = (body.action or "").strip()
    if action not in legal_actions:
        raise APIError(
            ERR_VALIDATION,
            f"非法处置动作：{action!r}（合法：{', '.join(sorted(legal_actions))}）",
            400)
    # 前置红线（core 在 Worker 兜底；API 侧快速失败省一次入队）
    if action == "file":
        if p.role != "human":
            raise APIError(
                ERR_FORBIDDEN,
                "「已立案」为 human 专属终态，正兵/AI 无权操作（W-020 AC-2）",
                403)
        if not body.legal_basis.strip():
            raise APIError(
                ERR_VALIDATION,
                "file 动作必须提供 legal_basis（法定依据/案号，W-020 AC-3）",
                400)
    action_params: dict = {}
    if body.note.strip():
        action_params["note"] = body.note.strip()
    if body.reason.strip():
        action_params["reason"] = body.reason.strip()
    if body.legal_basis.strip():
        action_params["legal_basis"] = body.legal_basis.strip()
    # 会话快照入任务行——Worker 不信任请求体 operator（REQ-009 主体一致性）
    params = {
        "action": action,
        "clue_id": clue_id,
        "action_params": action_params,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = body.idem_key.strip() or f"dispose:{clue_id}:{action}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_DISPOSE,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# W-019：线索读面（只读已产出结果，不触发重扫，AC-6）
# ----------------------------------------------------------------------
@router.get("/cases/{case_id}/clues")
def list_clues(case_id: str,
               level: str | None = None,
               dimension: str | None = None,
               jian: str | None = None,
               subject: str | None = None,
               status: str | None = None,
               skill: str | None = None,
               lens_run: bool = False,
               page: int = Query(1, ge=1),
               page_size: int = Query(50, ge=1, le=200),
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """线索列表：级别/维度/间类/主体/状态/镜头筛选 + 优先级排序 + 分页。

    skill：按产出技能（镜头）过滤；lens_run=true 仅定向镜头运行线索
    （lens_runs 补充产物并线，带 lens_run_id 留痕）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    try:
        data = clues_view.assemble_list(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            state_map=state_map, role=p.role,
            level=level, dimension=dimension, jian=jian,
            subject=subject, status=status,
            skill=skill or None, lens_run=lens_run,
            page=page, page_size=page_size,
            pack_id=case.pack_id,
            ontology_base=ctx.cases.snapshot_ontology_root(case_id))
    finally:
        if state is not None:
            state.close()
    return ok(data, data_version=version)


@router.get("/cases/{case_id}/clues/suppressed")
def list_suppressed(case_id: str,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """被抑制规则结果（不删除，仅移出主列表，AC-4）。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    data = clues_view.assemble_suppressed(
        case_dir=ctx.factory.case_dir(case_id), version=version)
    return ok(data, data_version=version)


@router.get("/cases/{case_id}/clues/{clue_id}")
def clue_detail(case_id: str, clue_id: str,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """线索详情：五间/source_rows 溯源/merged_from 合并来源/状态/决策。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    access = AccessContext(
        operator=p.operator, role=p.role, clearance=p.clearance,
        case_id=case_id, purpose="线索详情", network="web")
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    try:
        decisions = state.list_decisions() if state is not None else []
        # 方案 B：聚合线索（用间交叉）表级汇总行回填所需的 Function 结果
        cross_rows = _aggregate_cross_rows(
            ctx, case_id, case.pack_id, version, base_dir)
        data = clues_view.assemble_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            clue_id=clue_id, state_map=state_map, decisions=decisions,
            access=access, pack_id=case.pack_id,
            base_dir=base_dir,
            state_store=state, cross_rows=cross_rows)
    finally:
        if state is not None:
            state.close()
    if data is None:
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    # 间类密级过滤（详情 fail-closed 不可枚举；声明驱动，不硬编码内间）
    if not can_see_jian_types(
            data.get("jian_types") or [], role=p.role,
            jian_clearances=ontology_meta.jian_clearances(
                case.pack_id, base_dir)):
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    return ok(data, data_version=version)


# ----------------------------------------------------------------------
# REQ-V-005：核查工作区读写端点（读同步、写 202 入队 TASK_VERIFY）
# 状态机/红线在 Worker（core.verify_machine + agent: 拒绝）兜底，
# API 侧只做参数形状快速失败——与处置动作同款纵深防御。
# ----------------------------------------------------------------------
class VerifyItemIn(BaseModel):
    text: str = ""
    idem_key: str = ""


class VerifyTransitionIn(BaseModel):
    next_status: str = ""
    conclusion: str = ""
    text: str | None = None      # 仅采纳（建议→待核查）时有效=改写建议文本
    idem_key: str = ""


@router.get("/cases/{case_id}/clues/{clue_id}/verify-items")
def list_verify_items(case_id: str, clue_id: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """核查项清单 + progress（纯读 state；无 state.sqlite → available:false）。"""
    _get_owned_case(case_id, p, ctx.cases)  # 跨租户 404
    state, _ = _read_state(ctx.factory, case_id)
    try:
        data = _verify_items_payload(state, clue_id)
    finally:
        if state is not None:
            state.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/clues/{clue_id}/verify-items",
             status_code=202)
def add_verify_item(case_id: str, clue_id: str, body: VerifyItemIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """人工添加核查项（结构化构造器同通道）→ 202 + task_dto。"""
    _get_owned_case(case_id, p, ctx.cases)
    text = (body.text or "").strip()
    if not text:
        raise APIError(ERR_VALIDATION, "核查项文本不能为空（text）", 400)
    params = {
        "op": "add_manual",
        "clue_id": clue_id,
        "text": text,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = body.idem_key.strip() or f"verify-add:{clue_id}:{_sha1(text)}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


@router.post(
    "/cases/{case_id}/clues/{clue_id}/verify-items/{item_id}/transitions",
    status_code=202)
def transition_verify_item(case_id: str, clue_id: str, item_id: str,
                           body: VerifyTransitionIn,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """核查项状态迁移（采纳/忽略/裁决/重开）→ 202 + task_dto。

    迁移合法性、终态结论必填、item 归属校验均在 Worker 兜底；
    API 侧仅校验 next_status 形状（七态白名单），省一次非法入队。
    """
    _get_owned_case(case_id, p, ctx.cases)
    nxt = (body.next_status or "").strip()
    if not nxt:
        raise APIError(ERR_VALIDATION, "缺少目标状态 next_status", 400)
    if nxt not in VERIFY_STATUSES:
        # 仅形状校验；当前状态→nxt 是否合法由 core 状态机在 Worker 判
        raise APIError(
            ERR_VALIDATION,
            f"非法核查项状态：{nxt!r}（合法：{'/'.join(VERIFY_STATUSES)}）",
            400)
    params = {
        "op": "transition",
        "clue_id": clue_id,
        "item_id": item_id,
        "next_status": nxt,
        "conclusion": (body.conclusion or "").strip(),
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    # text 仅采纳（建议→待核查）有效；不传不落键，传空串与不传区分幂等域
    if body.text is not None:
        params["text"] = body.text
    text_for_key = body.text if body.text is not None else ""
    idem = (body.idem_key.strip()
            or f"verify:{item_id}:{nxt}:{_sha1(text_for_key)}")
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# REQ-V-017：一键复跑回填（op=replay，202 入队同 transition 纪律）。
# 归属校验（404）与映射预检（无映射 400 NO_REPLAY_MAPPING）同步做——
# 入队前快速失败，省一次注定 FAILED 的任务；Worker 侧同款校验兜底
# （映射/归属在消费时可能已变化，纵深防御）。复跑允许重复执行：
# idem_key 缺省带秒级时间戳（同秒重试幂等，跨秒可再跑）。
# ----------------------------------------------------------------------
@router.post(
    "/cases/{case_id}/clues/{clue_id}/verify-items/{item_id}/replay",
    status_code=202)
def replay_verify_item(case_id: str, clue_id: str, item_id: str,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """运行内核核查：只读 Function 结果回填 replay_json（不改状态/结论）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    state, _ = _read_state(ctx.factory, case_id)
    try:
        item = state.get_verify_item(item_id) if state is not None else None
        if item is None or item.get("clue_id") != clue_id:
            raise APIError(ERR_NOT_FOUND, f"核查项不存在：{item_id}", 404)
        if resolve_replay_mapping(
                item, pack_id=case.pack_id,
                base_dir=ctx.cases.snapshot_ontology_root(case_id)) is None:
            raise APIError(
                "NO_REPLAY_MAPPING",
                f"核查项 {item_id} 无库内可复跑映射"
                "（channel/ref_function/文本关键词均未命中）", 400)
    finally:
        if state is not None:
            state.close()
    params = {
        "op": "replay",
        "clue_id": clue_id,
        "item_id": item_id,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = (f"verify-replay:{item_id}:"
            f"{datetime.now().isoformat(timespec='seconds')}"
            .replace(":", "").replace("-", ""))
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# REQ-V-011：书证挂接核查项（link/unlink，202 入队同 transition 纪律）
# 归属/存在性校验在 Worker 兜底；API 侧 agent 红线 + 参数形状快速失败。
# ----------------------------------------------------------------------
class EvidenceLinkIn(BaseModel):
    material_id: str = ""
    idem_key: str = ""


def _reject_agent_evidence(p: Principal) -> None:
    if p.operator.startswith("agent:"):
        raise APIError(
            ERR_FORBIDDEN,
            f"Agent 身份 {p.operator!r} 不得挂接/解除书证"
            "（书证挂接是人的判断，REQ-V-011 验收 3）", 403)


@router.post(
    "/cases/{case_id}/clues/{clue_id}/verify-items/{item_id}/evidence",
    status_code=202)
def link_evidence_to_item(case_id: str, clue_id: str, item_id: str,
                          body: EvidenceLinkIn,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """书证挂接到核查项 → 202 + task_dto；核查项详情回显该材料。"""
    _get_owned_case(case_id, p, ctx.cases)
    _reject_agent_evidence(p)
    mid = (body.material_id or "").strip()
    if not mid:
        raise APIError(ERR_VALIDATION, "缺少 material_id", 400)
    params = {
        "op": "link",
        "clue_id": clue_id,
        "item_id": item_id,
        "material_id": mid,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = body.idem_key.strip() or f"verify-link:{clue_id}:{item_id}:{mid}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


@router.delete(
    "/cases/{case_id}/clues/{clue_id}/evidence/{material_id}/link",
    status_code=202)
def unlink_evidence_from_item(case_id: str, clue_id: str, material_id: str,
                              p: Principal = Depends(get_principal),
                              ctx: WebContext = Depends(get_ctx)):
    """解除书证挂接（按材料；行保留）→ 202 + task_dto。"""
    _get_owned_case(case_id, p, ctx.cases)
    _reject_agent_evidence(p)
    params = {
        "op": "unlink",
        "clue_id": clue_id,
        "material_id": material_id,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = f"verify-unlink:{clue_id}:{material_id}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# REQ-V-019：LLM 核查方向草案（同步端点；人显式点击触发，不自动发起）。
# 能力闸/选档交集/端点闸/脱敏分档/幻觉护栏/幂等/shadow 断言全部在
# core.llm.draft_verify（唯一实现）；本层只做案件归属、线索可见性、
# 确定性上下文组装与透传。degraded/blocked 一律 200 + {ok:false,...}
# （前端按档位提示渲染，不当 5xx 报错）。
# ----------------------------------------------------------------------
def _draft_context(detail: dict, verify_items: list[dict]) -> dict:
    """聚合上下文（确定性、轻量）：线索摘要 + 已有核查项。

    只取脱敏面需要的聚合字段；source_rows 明细不进 prompt（量大且
    轨迹/正文类键会被整段 drop，无增益）。
    """
    return {
        "线索": {
            "clue_id": detail.get("clue_id"),
            "标题": detail.get("title", ""),
            "依据": detail.get("basis", ""),
            "维度": detail.get("dimension") or "",
            "等级": detail.get("level") or "",
            "状态": detail.get("status", ""),
        },
        "已有核查项": [
            {"text": it.get("text", ""), "status": it.get("status", ""),
             "channel": it.get("channel", ""),
             "ref_function": it.get("ref_function", "")}
            for it in verify_items
        ],
    }


@router.post("/cases/{case_id}/clues/{clue_id}/verify-items/draft")
def draft_verify_direction(case_id: str, clue_id: str,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """LLM 核查方向草案：成功→提案队列（status=draft，人审后 REQ-V-014
    桥接 TASK_VERIFY）；降级/拦截→200 {ok:false, degraded/blocked, reason}。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    state, state_map = _read_state(ctx.factory, case_id)
    access = AccessContext(
        operator=p.operator, role=p.role, clearance=p.clearance,
        case_id=case_id, purpose="draft_verify", network="web")
    try:
        detail = clues_view.assemble_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            clue_id=clue_id, state_map=state_map, decisions=[],
            access=access, pack_id=case.pack_id, base_dir=base_dir,
            state_store=state)
        verify_items = (state.list_verify_items(clue_id)
                        if state is not None else [])
    finally:
        if state is not None:
            state.close()
    if detail is None:
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    # 间类密级过滤（与 clue_detail 同款 fail-closed 不可枚举）
    if not can_see_jian_types(
            detail.get("jian_types") or [], role=p.role,
            jian_clearances=ontology_meta.jian_clearances(
                case.pack_id, base_dir)):
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)

    conn = open_local_conn(ctx.proposals_db)
    try:
        result = draft_verify_items(
            conn, access, clue_id, case_id=case_id,
            context=_draft_context(detail, verify_items),
            pack=case.pack_id, base_dir=base_dir)
    finally:
        conn.close()
    return ok(result, data_version=version)


# ----------------------------------------------------------------------
# REQ-V-013：调取清单台账（GET 案件级 + 202 入队 add_request/request_transition）
# 状态机/归属校验在 Worker 兜底；API 侧 agent 红线 + 参数形状快速失败。
# ----------------------------------------------------------------------
class VerifyRequestCreateIn(BaseModel):
    target: str = ""            # 调取对象（如：海州银行营业部）
    material: str = ""          # 调取材料
    legal_instrument: str = ""  # 法律手续（调取函/审批文号）
    handler: str = ""           # 经办人
    due_date: str = ""          # 期限 YYYY-MM-DD（可空）
    note: str = ""
    item_id: str | None = None  # 关联核查项（可空）
    idem_key: str = ""


class VerifyRequestTransitionIn(BaseModel):
    next_status: str = ""
    idem_key: str = ""


_DUE_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/cases/{case_id}/verify-requests")
def list_verify_requests(case_id: str,
                         clue_id: str | None = Query(None),
                         status: str | None = Query(None),
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """调取清单台账（案件级；overdue 为读面派生，存储状态不变）。"""
    _get_owned_case(case_id, p, ctx.cases)
    if status is not None and status not in REQUEST_STATUSES:
        raise APIError(
            ERR_VALIDATION,
            f"非法调取请求状态：{status!r}（合法：{'/'.join(REQUEST_STATUSES)}）",
            400)
    state, _ = _read_state(ctx.factory, case_id)
    items: list[dict] = []
    available = state is not None
    if state is not None:
        try:
            items = state.list_verify_requests(clue_id, status)
        finally:
            state.close()
    return ok({"items": items, "available": available},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/clues/{clue_id}/verify-requests",
             status_code=202)
def create_verify_request(case_id: str, clue_id: str,
                          body: VerifyRequestCreateIn,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """发起调取登记 → 202 + task_dto（op=add_request）。"""
    _get_owned_case(case_id, p, ctx.cases)
    _reject_agent_evidence(p)  # 台账是人的催办行为（REQ-V-014 同红线）
    target = (body.target or "").strip()
    material = (body.material or "").strip()
    if not target:
        raise APIError(ERR_VALIDATION, "缺少调取对象（target）", 400)
    if not material:
        raise APIError(ERR_VALIDATION, "缺少调取材料（material）", 400)
    due = (body.due_date or "").strip()
    if due and not _DUE_DATE_SHAPE.match(due):
        raise APIError(ERR_VALIDATION,
                       f"非法期限格式：{due!r}（应为 YYYY-MM-DD）", 400)
    params = {
        "op": "add_request",
        "clue_id": clue_id,
        "target": target,
        "material": material,
        "legal_instrument": (body.legal_instrument or "").strip(),
        "handler": (body.handler or "").strip(),
        "due_date": due,
        "note": (body.note or "").strip(),
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    if body.item_id:
        params["item_id"] = body.item_id.strip()
    idem = (body.idem_key.strip() or "verify-req:{}:{}".format(
        clue_id,
        hashlib.sha1(f"{target}|{material}|{due}".encode()).hexdigest()[:12]))
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/verify-requests/{request_id}/transitions",
             status_code=202)
def transition_verify_request(case_id: str, request_id: str,
                              body: VerifyRequestTransitionIn,
                              p: Principal = Depends(get_principal),
                              ctx: WebContext = Depends(get_ctx)):
    """调取请求状态迁移（发起/回执登记/关闭）→ 202 + task_dto。"""
    _get_owned_case(case_id, p, ctx.cases)
    _reject_agent_evidence(p)
    nxt = (body.next_status or "").strip()
    if not nxt:
        raise APIError(ERR_VALIDATION, "缺少目标状态 next_status", 400)
    if nxt not in REQUEST_STATUSES:
        raise APIError(
            ERR_VALIDATION,
            f"非法调取请求状态：{nxt!r}（合法：{'/'.join(REQUEST_STATUSES)}）",
            400)
    params = {
        "op": "request_transition",
        "request_id": request_id,
        "next_status": nxt,
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = body.idem_key.strip() or f"verify-req-t:{request_id}:{nxt}"
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)
    return ok(task_dto(task),
              data_version=ctx.repo.current_version(case_id))
