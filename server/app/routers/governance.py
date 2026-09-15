"""
server/app/routers/governance.py
S4 治理建模：Action Type 编辑器（actions.json）+ 状态机编辑器（states.json）。

读：
  GET /cases/{cid}/actions —— 动作声明列表 + 角色/副作用枚举 + 状态名集；
  GET /cases/{cid}/states  —— 状态/迁移表 + 每个状态被哪些动作引用。
写（均为案件快照整体替换，写盘前临时副本过 core loader 全量强校验）：
  PUT /cases/{cid}/actions —— 🔴 改 terminal / requires_role 必须带非空理由
    （R2，后端硬门禁，绕过前端同样拦截），理由随审计链留痕；
    target_status / only_from 悬空引用阻止保存（R5，loader 亦兜底）。
  PUT /cases/{cid}/states  —— 🔴 终态不得设为可转移出（R3，硬阻止）；
    删除仍被 actions 引用的状态阻止保存（E3-2，列出引用动作）；
    取消终态标记（terminal true→false）同样危险确认 + 理由（§8.4）。

永不开放：llm_policy.json 无读模型端点、无写路由、无前端入口（R4）。
权限：GET 登录即可；PUT 需偏将及以上（require_analyst，案件域）。
未知字段：前端回传完整动作/状态对象，顶层保留 schema_version/_note，
loader 强校验兜底，不在此剥离任何键（R6）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.ontology_loader import (
    ALLOWED_ROLES,
    ALLOWED_SIDE_EFFECTS,
)

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.snapshot_config import (
    record_config_audit,
    require_analyst,
    save_config_json,
    snapshot_paths,
)

router = APIRouter(tags=["governance"])

# derive 规则与 core.ontology_loader._DERIVE_RULES 同源（当前仅 reverse_reach；
# 该符号为 loader 私有，此处本地声明，枚举扩展时两处同步）。
DERIVE_RULES = ["reverse_reach"]

# 动作表单覆盖的已知键（其余键视为未知字段，原样透传 + 前端提示，R6）
_ACTION_KNOWN_KEYS = {
    "name", "title", "target_status", "derive", "only_from", "parameters",
    "requires_role", "terminal", "side_effects", "description",
}
_STATE_KNOWN_KEYS = {
    "name", "label", "tone", "terminal", "requires_role",
    "requires_basis", "sla_days", "outcome",
}


# ----------------------------------------------------------------------
# 入参
# ----------------------------------------------------------------------
class ActionsIn(BaseModel):
    actions: list[dict]
    # 🔴 R2：危险变更理由；terminal/requires_role 变更时服务端强制非空
    reason: str | None = None


class StatesIn(BaseModel):
    states: list[dict]
    # 迁移表以 {from: [to,...]} map 往返（与 GET 一致，前端勾选直接消费）；
    # 落盘时转回 [{from,to}] 列表（states.json 声明格式）。
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    # 取消终态标记等危险变更理由（§8.4）
    reason: str | None = None


# ----------------------------------------------------------------------
# 共享读
# ----------------------------------------------------------------------
def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _state_name_set(states_data: dict) -> set[str]:
    return {s.get("name") for s in states_data.get("states", []) if s.get("name")}


def _unknown_keys(item: dict, known: set[str]) -> list[str]:
    return sorted(k for k in item.keys() if k not in known)


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------
@router.get("/cases/{case_id}/actions")
def list_actions(case_id: str,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """动作声明只读目录（S4-F2 表单挂数据源）。

    返回原始动作对象（保留 derive/description/only_from 等全部键）+
    角色/副作用/derive 枚举 + 状态名集 + 每条动作的界面未覆盖键（R6 提示）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    data = _read_json(snap_dir / "actions.json")
    states_data = _read_json(snap_dir / "states.json") if (
        snap_dir / "states.json").is_file() else {"states": []}
    actions = data.get("actions", [])
    for a in actions:
        a["_unknown_keys"] = _unknown_keys(a, _ACTION_KNOWN_KEYS)
    return ok({
        "actions": actions,
        "pack": pack_id,
        "enums": {
            "requires_role": sorted(ALLOWED_ROLES),
            "side_effects": sorted(ALLOWED_SIDE_EFFECTS),
            "derive": DERIVE_RULES,
        },
        "state_names": sorted(_state_name_set(states_data)),
    }, data_version=ctx.repo.current_version(case_id))


def _validate_action_refs(actions: list[dict],
                          state_names: set[str]) -> None:
    """R5：target_status / only_from 悬空引用 → 400 阻止保存。

    只做引用存在性与基本枚举校验，结构/可达性等交由 loader 全量校验兜底。
    """
    seen: set[str] = set()
    for i, a in enumerate(actions):
        ctx_ = f"actions[{i}]（{a.get('name', '?')}）"
        name = a.get("name")
        if not name or not isinstance(name, str):
            raise APIError(ERR_VALIDATION, f"{ctx_} 缺少 name", 400)
        if name in seen:
            raise APIError(ERR_VALIDATION, f"{ctx_} 动作名重复：{name}", 400)
        seen.add(name)
        target = a.get("target_status")
        if not target:
            raise APIError(ERR_VALIDATION, f"{ctx_} 缺少 target_status", 400)
        if target not in state_names:
            raise APIError(
                ERR_VALIDATION,
                f"🔴 目标状态「{target}」不存在于 states.json。"
                f"请先在状态机中创建该状态。保存已被阻止。", 400)
        only_from = a.get("only_from")
        if only_from is not None:
            if not isinstance(only_from, list) or not only_from:
                raise APIError(
                    ERR_VALIDATION,
                    f"{ctx_} only_from 必须为非空数组（留空表示不额外收紧）",
                    400)
            bad = [s for s in only_from if s not in state_names]
            if bad:
                raise APIError(
                    ERR_VALIDATION,
                    f"🔴 动作 {name} 的前置状态引用了不存在的状态：{bad}。"
                    f"保存已被阻止。", 400)
        role = a.get("requires_role", "any")
        if role not in ALLOWED_ROLES:
            raise APIError(
                ERR_VALIDATION,
                f"{ctx_} requires_role='{role}' 非法，允许 {sorted(ALLOWED_ROLES)}",
                400)
        for fx in a.get("side_effects", []):
            if fx not in ALLOWED_SIDE_EFFECTS:
                raise APIError(
                    ERR_VALIDATION,
                    f"{ctx_} 引用未注册副作用：{fx}，"
                    f"可用 {sorted(ALLOWED_SIDE_EFFECTS)}", 400)
        for j, param in enumerate(a.get("parameters", [])):
            if not isinstance(param, dict) or not param.get("name"):
                raise APIError(
                    ERR_VALIDATION,
                    f"{ctx_} parameters[{j}] 缺少 name", 400)


def _action_danger_changes(old: list[dict], new: list[dict]) -> list[dict]:
    """检测 terminal / requires_role 改前→改后（R2 危险字段）。"""
    old_by_name = {a.get("name"): a for a in old}
    changes = []
    for a in new:
        o = old_by_name.get(a.get("name"))
        if not o:
            continue
        before_t, after_t = bool(o.get("terminal", False)), bool(a.get("terminal", False))
        if before_t != after_t:
            changes.append({
                "name": a.get("name"), "title": a.get("title") or a.get("name"),
                "field": "terminal",
                "before": "是（终态）" if before_t else "否",
                "after": "是（终态）" if after_t else "否",
            })
        before_r = o.get("requires_role", "any")
        after_r = a.get("requires_role", "any")
        if before_r != after_r:
            changes.append({
                "name": a.get("name"), "title": a.get("title") or a.get("name"),
                "field": "requires_role",
                "before": before_r, "after": after_r,
            })
    return changes


@router.put("/cases/{case_id}/actions")
def save_actions(case_id: str, body: ActionsIn,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """整体替换 actions.json（S4-F2）。

    🔴 R2：terminal/requires_role 变更必须非空理由（服务端强制，UC-S4-9/10）；
    R5：悬空状态引用 400；写盘前临时副本 load_pack 全量校验，不合法不落盘。
    """
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)

    path = snap_dir / "actions.json"
    old_data = _read_json(path)
    states_path = snap_dir / "states.json"
    states_data = _read_json(states_path) if states_path.is_file() else {"states": []}
    state_names = _state_name_set(states_data)

    # 剥离 GET 附带的界面提示键，避免把 _unknown_keys 写回文件
    new_actions = [{k: v for k, v in a.items() if k != "_unknown_keys"}
                   for a in body.actions]

    _validate_action_refs(new_actions, state_names)

    danger = _action_danger_changes(old_data.get("actions", []), new_actions)
    if danger and not (body.reason or "").strip():
        fields = "、".join(sorted({c["field"] for c in danger}))
        raise APIError(
            ERR_VALIDATION,
            f"🔴 检测到危险字段变更（{fields}），必须填写变更理由后才能保存"
            f"（审计留痕，理由不可为空）。", 400)

    data = dict(old_data)  # 保留 schema_version 等顶层未知键（R6）
    data["actions"] = new_actions
    try:
        save_config_json(snap_dir, pack_id, base_dir, "actions.json", data,
                         ctx=ctx, case_id=case_id, op="actions_save", p=p,
                         detail={"actions": len(new_actions),
                                 "danger_changes": len(danger)})
    except APIError:
        raise
    except Exception as e:  # loader 全量校验失败（含 only_from 不可达等）→ 400 不落盘
        raise APIError(ERR_VALIDATION,
                       f"actions.json 校验失败，未落盘：{e}", 400)
    record_config_audit(ctx, case_id, p, "actions_save",
                        filename="actions.json", reason=body.reason,
                        summary={"actions": len(new_actions),
                                 "danger_changes": danger})
    return ok({"saved": True, "danger_changes": danger},
              data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# States
# ----------------------------------------------------------------------
@router.get("/cases/{case_id}/states")
def list_states(case_id: str,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """状态机只读目录（S4-F3）：状态 + 迁移表 + 被动作引用情况。"""
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, snap_dir, _ = snapshot_paths(ctx, case_id)
    states_path = snap_dir / "states.json"
    data = _read_json(states_path) if states_path.is_file() else {}
    states = data.get("states", [])
    transitions = {t.get("from"): list(t.get("to", []))
                   for t in data.get("transitions", []) if t.get("from")}

    actions_path = snap_dir / "actions.json"
    actions = _read_json(actions_path).get("actions", []) \
        if actions_path.is_file() else []
    referenced_by: dict[str, list[str]] = {}

    def _ref(state: str, aname: str) -> None:
        lst = referenced_by.setdefault(state, [])
        if aname not in lst:  # target_status + only_from 同动作只记一次
            lst.append(aname)

    for a in actions:
        aname = a.get("name")
        if a.get("target_status"):
            _ref(a["target_status"], aname)
        for s in a.get("only_from", []) or []:
            _ref(s, aname)

    for s in states:
        s["_unknown_keys"] = _unknown_keys(s, _STATE_KNOWN_KEYS)
    return ok({
        "states": states,
        "transitions": transitions,
        "referenced_by": referenced_by,
        "pack": pack_id,
    }, data_version=ctx.repo.current_version(case_id))


def _validate_states(states: list[dict],
                     transitions: dict[str, list[str]]) -> None:
    names: set[str] = set()
    for i, s in enumerate(states):
        name = s.get("name")
        if not name or not isinstance(name, str):
            raise APIError(ERR_VALIDATION,
                           f"states[{i}] 缺少 name", 400)
        if name in names:
            raise APIError(ERR_VALIDATION, f"状态名重复：{name}", 400)
        names.add(name)
    if not names:
        raise APIError(ERR_VALIDATION,
                       "states.json 声明为空：至少需要一个状态（REQ-R6）", 400)
    terminal = {s["name"] for s in states if s.get("terminal")}
    for frm, tos in transitions.items():
        if frm not in names:
            raise APIError(ERR_VALIDATION,
                           f"迁移表 from='{frm}' 未在 states 声明", 400)
        for to in tos:
            if to not in names:
                raise APIError(ERR_VALIDATION,
                               f"迁移表 {frm}→'{to}' 目标状态未声明", 400)
        # 🔴 R3：终态不得被设为可转移出（硬阻止）
        if frm in terminal and tos:
            raise APIError(
                ERR_VALIDATION,
                f"🔒 「{frm}」是终态，不可设置转出目标（终态一旦进入不可再转出，"
                f"否则审计链断裂）。如需变更，请先取消其终态标记（该操作需危险确认）。",
                400)


@router.put("/cases/{case_id}/states")
def save_states(case_id: str, body: StatesIn,
                p: Principal = Depends(get_principal),
                ctx: WebContext = Depends(get_ctx)):
    """整体替换 states.json（S4-F3）。

    🔴 R3 终态出边硬阻止；E3-2 删除被 actions 引用的状态阻止（列引用动作）；
    取消终态标记（terminal true→false）需非空理由（§8.4）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    require_analyst(p)
    pack_id, snap_dir, base_dir = snapshot_paths(ctx, case_id)

    states_path = snap_dir / "states.json"
    old_data = _read_json(states_path) if states_path.is_file() else {}
    old_states = old_data.get("states", [])

    new_states = [{k: v for k, v in s.items() if k != "_unknown_keys"}
                  for s in body.states]
    _validate_states(new_states, body.transitions)

    new_names = {s["name"] for s in new_states}

    # E3-2：删除的状态若仍被 actions 引用 → 阻止并列出引用动作
    actions_path = snap_dir / "actions.json"
    actions = _read_json(actions_path).get("actions", []) \
        if actions_path.is_file() else []
    old_names = {s.get("name") for s in old_states if s.get("name")}
    blocked: dict[str, list[str]] = {}
    for dn in old_names - new_names:
        refs = [a.get("name") for a in actions
                if a.get("target_status") == dn
                or dn in (a.get("only_from") or [])]
        if refs:
            blocked[dn] = refs
    if blocked:
        detail = "；".join(f"状态「{dn}」被动作 {refs} 引用"
                           for dn, refs in blocked.items())
        raise APIError(
            ERR_VALIDATION,
            f"🔴 以下状态仍被 Action 引用，不能删除（请先改动作的目标/前置状态）：{detail}",
            400)

    # 取消终态标记 = 危险变更（§8.4：终态可被转出），理由必填
    old_term = {s.get("name"): bool(s.get("terminal")) for s in old_states}
    unterminal = [s["name"] for s in new_states
                  if old_term.get(s["name"]) and not s.get("terminal")]
    if unterminal and not (body.reason or "").strip():
        raise APIError(
            ERR_VALIDATION,
            f"🔴 取消终态标记（{ '、'.join(unterminal) }）属危险变更，"
            f"终态将可被再次转出，必须填写变更理由后才能保存。", 400)

    data = dict(old_data)  # 保留 _note 等顶层键（R6）
    data.setdefault("schema_version", 2)
    data["states"] = new_states
    # map → states.json 声明格式 [{from,to}]（保留状态声明顺序）
    data["transitions"] = [
        {"from": frm, "to": tos}
        for frm in (s["name"] for s in new_states)
        for tos in [body.transitions.get(frm, [])]
    ]

    n_edges = sum(len(v) for v in body.transitions.values())
    try:
        save_config_json(snap_dir, pack_id, base_dir, "states.json", data,
                         ctx=ctx, case_id=case_id, op="states_save", p=p,
                         detail={"states": len(new_states),
                                 "transitions": n_edges,
                                 "unterminal": unterminal})
    except APIError:
        raise
    except Exception as e:  # loader 全量校验失败（如 actions 目标失配）→ 400 不落盘
        raise APIError(ERR_VALIDATION,
                       f"states.json 校验失败，未落盘：{e}", 400)
    record_config_audit(ctx, case_id, p, "states_save",
                        filename="states.json", reason=body.reason,
                        summary={"states": len(new_states),
                                 "transitions": n_edges})
    return ok({"saved": True, "unterminal": unterminal},
              data_version=ctx.repo.current_version(case_id))
