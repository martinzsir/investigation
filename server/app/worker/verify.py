"""
server/app/worker/verify.py
REQ-V-004 TASK_VERIFY：核查项写操作唯一执行体（校验 → state 写 → 审计链）。

与 worker/dispose.py、worker/review.py 同纪律：
  - 业务面短频写：秒级完成、**不产 DuckDB 版本文件**，只写 per-case
    state.sqlite（clue_verify_item + audit_chain）；
  - operator/role/clearance 为入队时会话快照，Worker 不信任请求体外的身份；
  - 合法转移单点在 core.verify_machine（REQ-V-003），本文件不重写状态表；
  - 红线：operator 以 'agent:' 开头一律拒绝（对齐 mcp_server
    clue_transition 白名单；系统建议永不自动成任务、永不下结论）；
  - 校验失败转 TaskExecError（任务 FAILED + 稳定错误码，不崩 Worker）。

错误码：
  CASE_NOT_FOUND / NO_VERSION / UNKNOWN_OP / CLUE_REQUIRED / ITEM_REQUIRED /
  TEXT_REQUIRED  —— 入参/前置类；
  ITEM_NOT_FOUND —— 核查项不存在或不属于该线索（含 KeyError 兜底）；
  VERIFY_FORBIDDEN —— agent:/匿名身份（含 PermissionError 兜底）；
  VERIFY_REJECTED —— 非法迁移/未知状态（ValueError 兜底）；
  CONCLUSION_REQUIRED —— 已证实/已查否缺结论（保留机器码便于前端定向提示）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.verify_machine import (
    ERR_CONCLUSION_REQUIRED,
    ERR_INVALID_TRANSITION,
    ERR_UNKNOWN_STATUS,
    PENDING,
    SUGGESTED,
    VerifyTransitionError,
    validate_conclusion,
    validate_verify_transition,
)

from core.audit import AuditChain

from server.app.store.state_sink import StateSink
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError

_VALID_OPS = ("transition", "add_manual")

# 状态机错误码 → 任务错误码（非法迁移/未知状态归 VERIFY_REJECTED；
# 缺结论保留 CONCLUSION_REQUIRED）
_MACHINE_CODE_MAP = {
    ERR_INVALID_TRANSITION: "VERIFY_REJECTED",
    ERR_UNKNOWN_STATUS: "VERIFY_REJECTED",
    ERR_CONCLUSION_REQUIRED: "CONCLUSION_REQUIRED",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def handle_verify(task, *, repo, factory, **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    p = task.params or {}
    op = str(p.get("op") or "").strip()
    clue_id = str(p.get("clue_id") or "").strip()
    operator = str(p.get("operator") or "").strip()

    if op not in _VALID_OPS:
        raise TaskExecError(
            "UNKNOWN_OP",
            f"未知核查写操作：{op!r}（合法：{', '.join(_VALID_OPS)}）")
    if not clue_id:
        raise TaskExecError("CLUE_REQUIRED", "缺少 clue_id")
    if not operator:
        raise TaskExecError("VERIFY_FORBIDDEN",
                            "缺少 operator（须由入队会话快照提供具名操作人）")
    # 红线：Agent 身份不得直接写核查项（采纳/忽略/裁决同此红线）
    if operator.startswith("agent:"):
        raise TaskExecError(
            "VERIFY_FORBIDDEN",
            f"Agent 身份 {operator!r} 不得直接裁决/采纳核查项；"
            "核查写操作须由具名人工触发")

    # 核查项锚定已 BUILD 数据版本（审计链 ontology_version 不允许挂 v0）
    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无核查项可写")

    state_path = factory.case_dir(case.id) / "state.sqlite"
    state = StateStore(case.id, state_path)
    try:
        sink = StateSink(state, ontology_version=f"v{ver}")
        chain = AuditChain(sink.conn, case_id=task.case_id,
                           backend="sqlite", ontology_version=f"v{ver}")
        if op == "add_manual":
            return _add_manual(state, chain, task.case_id, clue_id,
                               operator, ver, p)
        return _transition(state, chain, clue_id, operator, ver, p)
    except TaskExecError:
        raise
    except VerifyTransitionError as e:
        # 理论上 _transition 已映射，兜底防止漏网
        raise TaskExecError(
            _MACHINE_CODE_MAP.get(e.code, "VERIFY_REJECTED"), str(e))
    except PermissionError as e:
        raise TaskExecError("VERIFY_FORBIDDEN", str(e))
    except KeyError as e:
        raise TaskExecError("ITEM_NOT_FOUND", f"核查项字段缺失：{e}")
    except ValueError as e:
        raise TaskExecError("VERIFY_REJECTED", str(e))
    finally:
        state.close()


def _add_manual(state: StateStore, chain: AuditChain, case_id: str,
                clue_id: str, operator: str, ver: int,
                p: dict) -> dict[str, Any]:
    text = str(p.get("text") or "").strip()
    if not text:
        raise TaskExecError("TEXT_REQUIRED", "人工核查项文本不能为空")
    row = state.add_manual_verify_item(case_id, clue_id, text)
    chain.append(
        operator=operator, before=None,
        after={"event": "verify_item_add_manual",
               "clue_id": clue_id, "item_id": row["item_id"],
               "kind": "manual", "origin": "manual",
               "status": row["status"], "text": text},
        source_row_ids=[row["item_id"]],
        ontology_version=f"v{ver}")
    return {"op": "add_manual", "clue_id": clue_id,
            "item_id": row["item_id"], "status": row["status"],
            "added": row.get("added", 0), "version": ver}


def _transition(state: StateStore, chain: AuditChain, clue_id: str,
                operator: str, ver: int, p: dict) -> dict[str, Any]:
    item_id = str(p.get("item_id") or "").strip()
    if not item_id:
        raise TaskExecError("ITEM_REQUIRED", "缺少 item_id")
    item = state.get_verify_item(item_id)
    # 不存在或不属于本线索一律 ITEM_NOT_FOUND（防跨线索改写）
    if item is None or item.get("clue_id") != clue_id:
        raise TaskExecError(
            "ITEM_NOT_FOUND",
            f"核查项不存在：{item_id}（线索 {clue_id}）")

    nxt = str(p.get("next_status") or "").strip()
    conclusion = str(p.get("conclusion") or "").strip()
    cur_status = item["status"]

    # 状态机校验（单一事实源 core.verify_machine）
    try:
        validate_verify_transition(cur_status, nxt)
        validate_conclusion(nxt, conclusion)
    except VerifyTransitionError as e:
        raise TaskExecError(
            _MACHINE_CODE_MAP.get(e.code, "VERIFY_REJECTED"), str(e))

    # text 仅在采纳建议项（建议→待核查）时作为「改一改」覆写；
    # 其余迁移携带 text 一律忽略（与 REQ-V-005 API 语义一致）
    new_text: str | None = None
    raw_text = p.get("text")
    if raw_text is not None and cur_status == SUGGESTED and nxt == PENDING:
        candidate = str(raw_text).strip()
        if candidate:
            new_text = candidate

    before = {"status": cur_status,
              "conclusion": item.get("conclusion") or "",
              "text": item.get("text") or ""}
    ts = _now()
    updated = state.transition_verify_item(
        item_id, status=nxt, conclusion=conclusion,
        operator=operator, updated_at=ts, text=new_text)
    if updated is None:  # 极端并发：读之后行被删
        raise TaskExecError("ITEM_NOT_FOUND",
                            f"核查项已不存在：{item_id}")

    # 扁平落链（对齐 case_library/action_executor 惯例）：before_state 独立列，
    # after_state = 事件元数据 + 新状态（from_status 冗余便于时间线筛选）
    chain.append(
        operator=operator, before=before,
        after={"event": "verify_item_transition",
               "clue_id": clue_id, "item_id": item_id,
               "from_status": cur_status, "status": nxt,
               "conclusion": conclusion,
               "text": updated.get("text") or "",
               "rewritten": new_text is not None},
        source_row_ids=[item_id],
        ontology_version=f"v{ver}")
    return {"op": "transition", "clue_id": clue_id, "item_id": item_id,
            "status": nxt, "conclusion": conclusion,
            "rewritten": new_text is not None, "version": ver}
