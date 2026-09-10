"""
server/app/worker/dispose.py
W-020 DISPOSE 快速通道：线索处置动作（verify/reset/exclude/confirm/file）。

业务面短频写（backend_api.md 2.4）：入队、案件级 FIFO、秒级完成、
**不产 DuckDB 版本文件**；写 per-case state.sqlite——经 StateSink 走
core DisposalBoard/ActionExecutor（四步校验单点在 core：角色/必填参数/
状态机/权限上下文），AuditChain sqlite 后端落持久哈希链。

会话快照（operator/role/clearance）入队时从会话写入任务行 params；
Worker 不接受请求体 operator（REQ-009 主体一致性由 core 兜底）。

校验失败转 TaskExecError（任务 FAILED + 明确错误码，不崩 Worker）：
  PermissionError → ACTION_FORBIDDEN（如正兵 file）；
  ValueError      → ACTION_REJECTED（缺 legal_basis/非法状态迁移等）。
"""
from __future__ import annotations

from typing import Any

from core.access import AccessContext
from core.disposal import DisposalBoard
from core.registry import ClueStatus

from server.app import ontology_meta
from server.app.clues_artifact import load_case_clues
from server.app.store.state_sink import StateSink
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError


def handle_dispose(task, *, repo, factory, **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    p = task.params or {}
    action = str(p.get("action") or "").strip()
    clue_id = str(p.get("clue_id") or "").strip()
    operator = str(p.get("operator") or "").strip()
    role = str(p.get("role") or "正兵")
    clearance = int(p.get("clearance") or 1)
    action_params = dict(p.get("action_params") or {})

    # R6：合法动作来自案件包 actions 声明（set_clue_status 类），不硬编码
    legal_actions = ontology_meta.dispose_action_names(
        case.pack_id, factory.case_dir(case.id) / "ontology")
    if action not in legal_actions:
        raise TaskExecError(
            "UNKNOWN_ACTION",
            f"未知处置动作：{action!r}（合法：{', '.join(sorted(legal_actions))}）")
    if not clue_id:
        raise TaskExecError("CLUE_REQUIRED", "缺少 clue_id")

    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无线索可处置")
    try:
        clues = load_case_clues(factory.case_dir(case.id), ver)
    except FileNotFoundError as e:
        raise TaskExecError("CLUES_NOT_READY", str(e))
    if not any(c.clue_id == clue_id for c in clues):
        raise TaskExecError(
            "CLUE_NOT_FOUND",
            f"线索不存在：{clue_id}（当前版本 v{ver}，共 {len(clues)} 条）")

    state_path = factory.case_dir(case.id) / "state.sqlite"
    state = StateStore(case.id, state_path)
    try:
        sink = StateSink(state, ontology_version=f"v{ver}")
        ctx = AccessContext(operator=operator, role=role, clearance=clearance,
                            case_id=case.id, purpose="web:dispose",
                            network="web")
        board = DisposalBoard(clues, store=sink, pack=case.pack_id,
                              sink=sink, access=ctx)
        board.restore()  # state 状态回灌（处置状态真值源）
        try:
            clue = _apply_action(board, action, clue_id, operator,
                                 action_params)
        except PermissionError as e:
            raise TaskExecError("ACTION_FORBIDDEN", str(e))
        except ValueError as e:
            raise TaskExecError("ACTION_REJECTED", str(e))
        board.persist()  # clue_disposal_status 落 state（UPSERT 幂等）
        return {"action": action, "clue_id": clue_id,
                "status": clue.status, "version": ver}
    finally:
        state.close()


def _apply_action(board: DisposalBoard, action: str, clue_id: str,
                  operator: str, params: dict):
    note = params.get("note") or params.get("reason") or ""
    if action == "verify":
        return board.verify(clue_id, operator=operator, note=note)
    if action == "reset":
        return board.transition(clue_id, ClueStatus.PENDING,
                                operator=operator, note=note)
    if action == "exclude":
        return board.exclude(clue_id, operator=operator,
                             reason=params.get("reason") or note)
    if action == "confirm":
        return board.confirm(clue_id, operator=operator, note=note)
    if action == "file":
        legal = str(params.get("legal_basis") or "").strip()
        return board.file(clue_id, operator=operator, legal_basis=legal)
    raise TaskExecError("UNKNOWN_ACTION", f"未知处置动作：{action!r}")
