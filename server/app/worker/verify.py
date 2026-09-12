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
  TEXT_REQUIRED / MATERIAL_REQUIRED / TARGET_REQUIRED / REQUEST_REQUIRED
    —— 入参/前置类；
  ITEM_NOT_FOUND —— 核查项不存在或不属于该线索（含 KeyError 兜底）；
  MATERIAL_NOT_FOUND —— 书证不存在或不属于该线索/案件（link/unlink）；
  REQUEST_NOT_FOUND —— 调取请求不存在或不属于本案件（request_transition）；
  VERIFY_FORBIDDEN —— agent:/匿名身份（含 PermissionError 兜底）；
  VERIFY_REJECTED —— 非法迁移/未知状态（ValueError 兜底）；
  REQUEST_REJECTED —— 调取请求非法迁移/未知状态（状态机错误码映射）；
  CONCLUSION_REQUIRED —— 已证实/已查否缺结论（保留机器码便于前端定向提示）；
  NO_REPLAY_MAPPING —— 复跑无库内可复跑映射（REQ-V-017，API 已同步预检，
    Worker 兜底：映射/归属在消费时可能已变化）；
  REPLAY_FAILED —— 内核 Function 执行失败（主跑+备选均失败，不写任何字段）。
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
    validate_request_transition,
    validate_verify_transition,
)

from core.audit import AuditChain

from server.app.verify_functions_map import resolve_replay_mapping
from server.app.store.state_sink import StateSink
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError

_VALID_OPS = ("transition", "add_manual", "link", "unlink",
              "add_request", "request_transition", "replay")

# 状态机错误码 → 任务错误码（非法迁移/未知状态归 VERIFY_REJECTED；
# 缺结论保留 CONCLUSION_REQUIRED；调取请求归 REQUEST_REJECTED）
_MACHINE_CODE_MAP = {
    ERR_INVALID_TRANSITION: "VERIFY_REJECTED",
    ERR_UNKNOWN_STATUS: "VERIFY_REJECTED",
    ERR_CONCLUSION_REQUIRED: "CONCLUSION_REQUIRED",
}

_REQUEST_CODE_MAP = {
    ERR_INVALID_TRANSITION: "REQUEST_REJECTED",
    ERR_UNKNOWN_STATUS: "REQUEST_REJECTED",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def handle_verify(task, *, repo, factory, snapshot_base_for=None, **_: Any) -> dict[str, Any]:
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
    # request_transition 的 clue_id 从台账行派生（案件级路由不带线索路径）
    if op != "request_transition" and not clue_id:
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
        if op in ("link", "unlink"):
            return _evidence_link(state, chain, task.case_id, clue_id,
                                  operator, ver, p, unlink=(op == "unlink"))
        if op == "add_request":
            return _add_request(state, chain, task.case_id, clue_id,
                                operator, ver, p)
        if op == "request_transition":
            return _request_transition(state, chain, task.case_id,
                                       operator, ver, p)
        if op == "replay":
            return _replay(state, chain, case, clue_id, operator, ver, p,
                           factory=factory,
                           snapshot_base_for=snapshot_base_for)
        return _transition(state, chain, clue_id, operator, ver, p)
    except TaskExecError:
        raise
    except VerifyTransitionError as e:
        # 理论上各分支已映射，兜底防止漏网
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
    # REQ-V-019：提案审批桥接可携带结构化路由字段（origin='ai_draft'）；
    # 人工通道不传这些键，origin 缺省 'manual'，行为不变。
    origin = str(p.get("origin") or "manual")
    if origin not in ("manual", "ai_draft"):
        raise TaskExecError(
            "VERIFY_REJECTED",
            f"非法核查项 origin：{origin!r}（合法：manual/ai_draft）")
    row = state.add_manual_verify_item(
        case_id, clue_id, text, origin=origin,
        channel=str(p.get("channel") or ""),
        ref_function=str(p.get("ref_function") or ""),
        external=p.get("external")
        if isinstance(p.get("external"), dict) else None,
        falsification=str(p.get("falsification") or ""))
    chain.append(
        operator=operator, before=None,
        after={"event": "verify_item_add_manual",
               "clue_id": clue_id, "item_id": row["item_id"],
               "kind": "manual", "origin": origin,
               "status": row["status"], "text": text},
        source_row_ids=[row["item_id"]],
        ontology_version=f"v{ver}")
    return {"op": "add_manual", "clue_id": clue_id,
            "item_id": row["item_id"], "status": row["status"],
            "origin": origin,
            "added": row.get("added", 0), "version": ver}


def _replay(state: StateStore, chain: AuditChain, case, clue_id: str,
            operator: str, ver: int, p: dict, *,
            factory, snapshot_base_for=None) -> dict[str, Any]:
    """REQ-V-017 一键复跑回填（op=replay，202 执行体）。

    映射解析走 server.app.verify_functions_map（playbook 主路径 + 关键词
    兜底，fail-closed 唯一解析面）→ 只读 FunctionExecutor 调内核 Function
    → 结果回填 replay_json（Function 结果+溯源+时间戳）。status/conclusion/
    operator/updated_at 裁决四列不动（复跑是 AI辅助推演·需人确认，永不
    参与固证门禁判定）。主跑失败试 fallback_function（库内先试一把）；
    均失败 → REPLAY_FAILED，不写任何字段。
    """
    from core.functions import FunctionExecutor

    item_id = str(p.get("item_id") or "").strip()
    if not item_id:
        raise TaskExecError("ITEM_REQUIRED", "缺少 item_id")
    item = state.get_verify_item(item_id)
    # 不存在或不属于本线索一律 ITEM_NOT_FOUND（防跨线索改写）
    if item is None or item.get("clue_id") != clue_id:
        raise TaskExecError(
            "ITEM_NOT_FOUND",
            f"核查项不存在：{item_id}（线索 {clue_id}）")

    base_dir = (snapshot_base_for(case.id)
                if snapshot_base_for is not None else None)
    mapping = resolve_replay_mapping(item, pack_id=case.pack_id,
                                     base_dir=base_dir)
    if mapping is None:
        raise TaskExecError(
            "NO_REPLAY_MAPPING",
            f"核查项 {item_id} 无库内可复跑映射"
            "（channel/ref_function/文本关键词均未命中）")

    store = factory.for_case(case.id, mode="read")
    try:
        fx = FunctionExecutor(store, pack=case.pack_id, base_dir=base_dir)
        try:
            out = fx.invoke(mapping.function)
            used, fallback_used = mapping.function, False
        except Exception as primary_err:  # noqa: BLE001 —— 主跑失败试备选
            if (not mapping.fallback_function
                    or mapping.fallback_function == mapping.function):
                raise TaskExecError(
                    "REPLAY_FAILED",
                    f"内核核查执行失败（{mapping.function}）："
                    f"{type(primary_err).__name__}: {primary_err}"
                ) from primary_err
            try:
                out = fx.invoke(mapping.fallback_function)
                used, fallback_used = mapping.fallback_function, True
            except Exception as fb_err:  # noqa: BLE001 —— 备选也失败
                raise TaskExecError(
                    "REPLAY_FAILED",
                    f"内核核查主跑（{mapping.function}）与备选"
                    f"（{mapping.fallback_function}）均失败："
                    f"{type(fb_err).__name__}: {fb_err}") from fb_err
    finally:
        store.close()

    replay = {
        "function": used,
        "fallback_used": fallback_used,
        "mapping_source": mapping.source,
        "params_used": out.get("params_used", {}),
        "output_type": out.get("output_type"),
        "result": (out.get("result") if out.get("result") is not None
                   else out.get("rows")),
        "degraded": bool(out.get("degraded")),
        "degraded_reason": out.get("degraded_reason"),
        "version": f"v{ver}",
        "source_row_ids": [item_id],
        "operator": operator,
        "replayed_at": _now(),
    }
    updated = state.set_verify_item_replay(item_id, replay)
    if updated is None:  # 极端并发：读之后行被删
        raise TaskExecError("ITEM_NOT_FOUND", f"核查项已不存在：{item_id}")
    # 审计链留痕（只读 Function 副作用 = replay_json 回填一条事件）
    chain.append(
        operator=operator, before=None,
        after={"event": "verify_item_replay", "clue_id": clue_id,
               "item_id": item_id, "function": used,
               "fallback_used": fallback_used,
               "mapping_source": mapping.source,
               "degraded": replay["degraded"],
               "replayed_at": replay["replayed_at"]},
        source_row_ids=[item_id],
        ontology_version=f"v{ver}")
    return {"op": "replay", "clue_id": clue_id, "item_id": item_id,
            "function": used, "fallback_used": fallback_used,
            "degraded": replay["degraded"], "version": ver}


def _evidence_link(state: StateStore, chain: AuditChain, case_id: str,
                   clue_id: str, operator: str, ver: int, p: dict,
                   *, unlink: bool) -> dict[str, Any]:
    """REQ-V-011 书证 ↔ 核查项挂接/解除（202 异步执行体）。

    归属校验：材料与核查项都必须属于本线索（防跨线索改写）；
    已终态核查项允许挂接（案卷补充不影响已固定结论，REQ-V-011 细节）。
    """
    material_id = str(p.get("material_id") or "").strip()
    if not material_id:
        raise TaskExecError("MATERIAL_REQUIRED", "缺少 material_id")
    row = state.get_evidence(material_id)
    if (row is None or row.get("case_id") != case_id
            or row.get("clue_id") != clue_id):
        raise TaskExecError(
            "MATERIAL_NOT_FOUND",
            f"书证不存在：{material_id}（线索 {clue_id}）")

    if unlink:
        before = {"item_id": row.get("item_id") or ""}
        updated = state.unlink_evidence(material_id)
        if updated is None:  # 极端并发：读之后行被删
            raise TaskExecError("MATERIAL_NOT_FOUND",
                                f"书证已不存在：{material_id}")
        chain.append(
            operator=operator, before=before,
            after={"event": "evidence_unlink", "clue_id": clue_id,
                   "material_id": material_id,
                   "orig_name": row.get("orig_name") or "",
                   "prev_item_id": before["item_id"]},
            source_row_ids=[material_id],
            ontology_version=f"v{ver}")
        return {"op": "unlink", "clue_id": clue_id,
                "material_id": material_id, "version": ver}

    item_id = str(p.get("item_id") or "").strip()
    if not item_id:
        raise TaskExecError("ITEM_REQUIRED", "缺少 item_id")
    item = state.get_verify_item(item_id)
    if item is None or item.get("clue_id") != clue_id:
        raise TaskExecError(
            "ITEM_NOT_FOUND",
            f"核查项不存在：{item_id}（线索 {clue_id}）")
    before = {"item_id": row.get("item_id") or ""}
    updated = state.link_evidence(material_id, item_id)
    if updated is None:
        raise TaskExecError("MATERIAL_NOT_FOUND",
                            f"书证已不存在：{material_id}")
    chain.append(
        operator=operator, before=before,
        after={"event": "evidence_link", "clue_id": clue_id,
               "item_id": item_id, "material_id": material_id,
               "orig_name": row.get("orig_name") or "",
               "prev_item_id": before["item_id"]},
        source_row_ids=[material_id, item_id],
        ontology_version=f"v{ver}")
    return {"op": "link", "clue_id": clue_id, "item_id": item_id,
            "material_id": material_id, "version": ver}


def _add_request(state: StateStore, chain: AuditChain, case_id: str,
                 clue_id: str, operator: str, ver: int,
                 p: dict) -> dict[str, Any]:
    """REQ-V-013 调取清单创建（op=add_request，202 执行体）。"""
    target = str(p.get("target") or "").strip()
    material = str(p.get("material") or "").strip()
    if not target:
        raise TaskExecError("TARGET_REQUIRED", "缺少调取对象（target）")
    if not material:
        raise TaskExecError("MATERIAL_REQUIRED", "缺少调取材料（material）")
    row = state.insert_verify_request(
        case_id=case_id, clue_id=clue_id, target=target, material=material,
        item_id=(str(p.get("item_id") or "").strip() or None),
        legal_instrument=str(p.get("legal_instrument") or "").strip(),
        handler=str(p.get("handler") or "").strip(),
        due_date=str(p.get("due_date") or "").strip(),
        note=str(p.get("note") or "").strip(),
        created_by=operator, created_at=_now())
    chain.append(
        operator=operator, before=None,
        after={"event": "verify_request_create", "clue_id": clue_id,
               "request_id": row["request_id"], "target": target,
               "material": material,
               "due_date": row.get("due_date") or "",
               "status": row["status"]},
        source_row_ids=[row["request_id"]],
        ontology_version=f"v{ver}")
    return {"op": "add_request", "clue_id": clue_id,
            "request_id": row["request_id"], "status": row["status"],
            "version": ver}


def _request_transition(state: StateStore, chain: AuditChain, case_id: str,
                        operator: str, ver: int, p: dict) -> dict[str, Any]:
    """REQ-V-013 调取请求状态迁移（发起/回执登记/关闭，202 执行体）。"""
    request_id = str(p.get("request_id") or "").strip()
    if not request_id:
        raise TaskExecError("REQUEST_REQUIRED", "缺少 request_id")
    row = state.get_verify_request(request_id)
    if row is None or row.get("case_id") != case_id:
        raise TaskExecError(
            "REQUEST_NOT_FOUND", f"调取请求不存在：{request_id}")

    nxt = str(p.get("next_status") or "").strip()
    cur_status = row["status"]
    try:
        validate_request_transition(cur_status, nxt)
    except VerifyTransitionError as e:
        raise TaskExecError(
            _REQUEST_CODE_MAP.get(e.code, "REQUEST_REJECTED"), str(e))

    updated = state.update_verify_request_status(
        request_id, nxt, updated_at=_now())
    if updated is None:  # 极端并发：读之后行被删
        raise TaskExecError("REQUEST_NOT_FOUND",
                            f"调取请求已不存在：{request_id}")
    chain.append(
        operator=operator, before={"status": cur_status},
        after={"event": "verify_request_transition",
               "clue_id": row.get("clue_id") or "",
               "request_id": request_id, "from_status": cur_status,
               "status": nxt},
        source_row_ids=[request_id],
        ontology_version=f"v{ver}")
    return {"op": "request_transition", "request_id": request_id,
            "clue_id": row.get("clue_id") or "", "status": nxt,
            "version": ver}


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
