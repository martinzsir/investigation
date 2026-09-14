"""
server/app/worker/report.py
RC-304 TASK_REPORT：研判报告异步生成处理器。

与 worker/verify.py 同纪律：
  - 业务面短频写：只写 per-case state.sqlite（clue_canvas_report + audit_chain）；
  - operator/role/clearance 为入队时会话快照，Worker 不信任请求体外的身份；
  - 读快照 doc → 构建 prompt → call_llm → parse_report_sections → 写回 report 行；
  - 失败 → status=failed + error 记录，不崩 Worker（转 TaskExecError）。

错误码：
  CASE_NOT_FOUND / NO_VERSION / REPORT_NOT_FOUND / SNAPSHOT_NOT_FOUND /
  LLM_DISABLED / LLM_BLOCKED / LLM_ERROR / REPORT_PARSE_FAILED
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.access import AccessContext, LLMBlockedError
from core.llm.draft_verify import select_deployment, _deployment_policy
from core.llm.llm_client import LLMClient
from core.llm.redact import (
    call_llm,
    load_llm_policy,
    redact_payload,
)

from server.app.canvas_citation_guard import build_valid_refs_from_doc
from server.app.canvas_report import (
    build_report_prompt,
    parse_report_sections,
)
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def handle_report(task, *, repo, factory, snapshot_base_for=None, **_: Any) \
        -> dict[str, Any]:
    """研判报告生成（RC-304）。

    task.params: {report_id, clue_id, operator, role, clearance, extra_request?}
    """
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")

    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("NO_VERSION", "案件尚未 BUILD，无报告可生成")

    p = task.params or {}
    report_id = str(p.get("report_id") or "").strip()
    clue_id = str(p.get("clue_id") or "").strip()
    operator = str(p.get("operator") or "").strip()
    role = str(p.get("role") or "正兵")
    clearance = int(p.get("clearance") or 1)
    extra_request = str(p.get("extra_request") or "").strip()

    if not report_id:
        raise TaskExecError("REPORT_NOT_FOUND", "缺少 report_id")
    if not clue_id:
        raise TaskExecError("CLUE_REQUIRED", "缺少 clue_id")

    state_path = factory.case_dir(case.id) / "state.sqlite"
    state = StateStore(case.id, state_path)
    try:
        return _do_generate(
            state, repo, case, ver, report_id, clue_id,
            operator, role, clearance, extra_request,
            snapshot_base_for, task.id)
    finally:
        state.close()


def _do_generate(state, repo, case, ver, report_id, clue_id,
                 operator, role, clearance, extra_request,
                 snapshot_base_for, task_id: str) -> dict[str, Any]:
    """报告生成主流程。"""
    # 1. 取 report 行（确认存在且 status=generating）
    report = state.get_canvas_report(report_id)
    if report is None:
        raise TaskExecError(
            "REPORT_NOT_FOUND", f"报告不存在：{report_id}")
    snapshot_id = report.get("snapshot_id", "")
    if not snapshot_id:
        raise TaskExecError(
            "SNAPSHOT_NOT_FOUND", f"报告未关联快照：{report_id}")

    # 2. 取冻结快照 doc
    snap = state.get_canvas_snapshot(snapshot_id)
    if snap is None:
        raise TaskExecError(
            "SNAPSHOT_NOT_FOUND", f"快照不存在：{snapshot_id}")
    doc = snap["doc"]

    # 3. 构建 AccessContext + LLM 策略
    base_dir = None
    if snapshot_base_for:
        base_dir = snapshot_base_for(case.id)

    access = AccessContext(
        operator=operator, role=role, case_id=case.id,
        purpose="研判报告生成", clearance=clearance)

    policy = load_llm_policy(case.pack_id, base_dir=base_dir)
    if not policy.get("llm_enabled", False):
        state.update_canvas_report_status(
            report_id, status="failed",
            error="llm_disabled")
        raise TaskExecError(
            "LLM_DISABLED", "当前案件未启用 LLM（llm_enabled=false）")

    mode, dep, off_reason = select_deployment(access, policy)
    if mode == "off" or dep is None:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"deployment_off: {off_reason}")
        raise TaskExecError(
            "LLM_BLOCKED", f"智能问答不可用：{off_reason}")

    eff_policy = _deployment_policy(policy, dep)
    model = (dep.get("allowed_models") or [""])[0] or policy.get(
        "allowed_models", [""])[0]
    if not model:
        state.update_canvas_report_status(
            report_id, status="failed", error="no_model")
        raise TaskExecError("LLM_BLOCKED", "无可用模型")

    # 4. 构建 prompt + 脱敏
    system_prompt, user_prompt = build_report_prompt(doc, extra_request)
    import hashlib
    import json
    from server.app.canvas_chat import build_canvas_context
    context = build_canvas_context(doc)
    redacted = redact_payload(context, eff_policy)
    blob = json.dumps(redacted, ensure_ascii=False,
                      sort_keys=True, default=str)
    redacted["redaction_hash"] = hashlib.sha256(
        blob.encode("utf-8")).hexdigest()

    # 5. call_llm
    client = LLMClient()

    def _invoke(model, prompt, redacted_input):  # pragma: no cover
        resp = client.chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=4096)
        if resp.get("ok"):
            return resp["result"]
        raise RuntimeError(resp.get("error", "LLM 调用失败"))

    try:
        llm_result = call_llm(
            state.conn, access, eff_policy, model=model,
            prompt=user_prompt, redacted_input=redacted,
            fake_invoke=_invoke)
    except LLMBlockedError as e:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"llm_blocked: {e}")
        raise TaskExecError("LLM_BLOCKED", f"LLM 被治理策略拒绝：{e}")
    except Exception as e:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"llm_error: {e}")
        raise TaskExecError("LLM_ERROR", f"LLM 调用失败：{e}")

    # 6. 解析 + 引用校验
    raw = llm_result.get("result") or {}
    raw_content = raw.get("content", "") if isinstance(raw, dict) \
        else str(raw)
    valid_refs = build_valid_refs_from_doc(doc)

    try:
        parsed = parse_report_sections(raw_content, valid_refs)
    except Exception as e:
        state.update_canvas_report_status(
            report_id, status="failed",
            error=f"parse_failed: {e}")
        raise TaskExecError(
            "REPORT_PARSE_FAILED", f"报告解析失败：{e}")

    # 7. 写回 report 行
    state.update_canvas_report_status(
        report_id, status="ready",
        content_md=parsed["content_md"],
        sections=parsed["sections"],
        citations=parsed["citations"],
        warnings=parsed["warnings"],
        task_id=task_id)

    return {
        "report_id": report_id,
        "status": "ready",
        "version_no": report.get("version_no", 0),
        "sections": list(parsed["sections"].keys()),
        "citation_count": len(parsed["citations"]),
        "warning_count": len(parsed["warnings"]),
    }
