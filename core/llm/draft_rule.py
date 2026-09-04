"""
core/llm/draft_rule.py
REQ-034 LLM 规则草案生成。

把案件问句（自然语言）转成候选 rule_text / function / params / dimension / jian_types。
关键约束（与三条禁令一致）：
  - 必须映射既有 Function（functions.json 白名单），无映射即失败，不生成自由 SQL；
  - 草案不自动生效（需人工审批，走 ProposalStore.submit → decide）；
  - 影子模式：草案生成不产生 finding，不影响确定性规则运行；
  - 每条草案含 author=model:<id> 与 provenance 溯源。

调用流程：
  1. 脱敏上下文（redact_payload）+ 案件问句 → 构造 prompt
  2. LLMClient.chat_json → 解析候选 JSON
  3. sanitize_candidate（guard 白名单）+ validate_proposal（七项硬校验）
  4. 通过 → ProposalStore.submit（status=draft，需人工 decide）
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any, Callable

from core.access import AccessContext
from core.llm.guard import sanitize_candidate, assert_no_status_change
from core.llm.llm_client import LLMClient
from core.llm.redact import build_redacted_context, redact_payload, call_llm
from core.ontology_loader import load_pack
from core.proposal import ProposalStore, validate_proposal, ProposalValidationError


def draft_rule(
    conn,
    ctx: AccessContext,
    question: str,
    *,
    pack: str = "default",
    llm_client: LLMClient | None = None,
    findings_context: list[dict] | None = None,
    policy: dict | None = None,
) -> dict[str, Any]:
    """REQ-034: LLM 把案件问句转成规则草案。

    Args:
        conn: DuckDB 连接
        ctx: AccessContext（需要 operator / network / clearance）
        question: 案件问句（如"查找季末整数存款异常"）
        pack: ontology 案件包名
        llm_client: LLM 客户端（None 时用默认 Qwen3）
        findings_context: 已有 findings（构造脱敏上下文给 LLM 参考）
        policy: LLM 策略（None 时 load_llm_policy）

    Returns:
        {
            "ok": True/False,
            "proposal_id": str | None,
            "candidate": dict | None,    # 草案候选
            "model": str,
            "error": str | None,
            "provenance": dict,          # 溯源信息
        }
    """
    from core.llm.redact import load_llm_policy
    policy = policy or load_llm_policy(pack)

    # REQ-040：一键降级开关——llm_enabled=false 时零模型调用、零提案写入，
    # 落 llm_call_log + AuditChain 后静默回落确定性路径（degraded 标记，AC1/AC4）
    from core.llm.fallback import ensure_llm_capability, LLMDegraded
    _model_hint = getattr(llm_client, "model", None)
    try:
        ensure_llm_capability(conn, ctx, policy, model=_model_hint,
                              source="draft_rule")
    except LLMDegraded as e:
        return {"ok": False, "proposal_id": None, "candidate": None,
                "model": _model_hint or "offline", "error": str(e),
                "degraded": True,
                "provenance": {"question": question, "degraded": True}}

    # 构造脱敏上下文
    redacted_ctx = build_redacted_context(findings_context or [], policy)

    spec = load_pack(pack)
    func_names = sorted(spec.functions.keys())

    # 构造 system prompt
    system_prompt = (
        "你是侦查规则分析师。根据案件问句，生成一条候选侦查规则草案。\n"
        "严格约束：\n"
        "1. function 字段必须从以下白名单中选择，不得自创函数或写 SQL：\n"
        f"   {json.dumps(func_names, ensure_ascii=False)}\n"
        "2. 输出 JSON 格式，包含字段：rule_text, function, params, dimension, jian_types\n"
        "3. rule_text 是自然语言判据（分析师可读）\n"
        "4. dimension 是维度（资金/通讯/轨迹/交叉/招投/举报/开源）\n"
        "5. jian_types 是间类列表（如 ['生间','内间']）\n"
        "6. 不得包含任何写操作（action/writeback/dispatch）\n"
        "7. 不得包含状态变更指令（立案/核实/固证）\n"
        "以 ```json``` 代码块输出。"
    )

    user_prompt = json.dumps({
        "案件问句": question,
        "已有线索上下文（脱敏）": redacted_ctx,
        "可用函数清单": func_names,
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # LLM 调用（走 call_llm 闸门：脱敏复扫 + 审计日志）
    client = llm_client or LLMClient()
    model_id = client.model

    # 用 fake_invoke 走 call_llm 闸门（生产用真实 API）
    def _invoke(model, prompt, redacted_input):
        resp = client.chat_json(messages, temperature=0.1, max_tokens=1024)
        if resp.get("ok"):
            return resp["result"]
        return resp

    try:
        llm_result = call_llm(
            conn, ctx, policy,
            model=model_id,
            prompt=user_prompt,
            redacted_input=redacted_ctx,
            fake_invoke=_invoke,
        )
    except Exception as e:
        return {"ok": False, "proposal_id": None, "candidate": None,
                "model": model_id, "error": str(e),
                "provenance": {"question": question, "model": model_id}}

    raw_result = llm_result["result"]
    parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None

    if parsed is None:
        # AC2: 无法解析 → 不生成草案
        return {"ok": False, "proposal_id": None, "candidate": None,
                "model": model_id, "error": "LLM 输出无法解析为 JSON",
                "provenance": {"question": question, "model": model_id}}

    # AC2: 必须映射既有 function
    func_name = parsed.get("function")
    if not func_name or func_name not in spec.functions:
        return {"ok": False, "proposal_id": None, "candidate": None,
                "model": model_id,
                "error": f"无可用函数：function={func_name!r} 不在白名单 {func_names}",
                "provenance": {"question": question, "model": model_id}}

    # REQ-039: 候选白名单清洗
    candidate, dropped = sanitize_candidate(parsed, "rule_draft")
    assert_no_status_change(candidate, "rule_draft")

    # 构造提案（按 proposal.schema.json 格式）
    proposal = {
        "proposal_id": f"pp-{uuid.uuid4().hex[:12]}",
        "kind": "rule_draft",
        "case_id": pack,
        "author": f"model:{model_id}",
        "candidate": candidate,
        "constraints": {"must_have_source_rows": True},
        "input": {
            "available_functions": func_names,
        },
        "_sort_hint": {
            "question": question,
            "model": model_id,
            "llm_log_id": llm_result.get("log_id"),
            "dropped_fields": dropped,
        },
    }

    # AC3: 草案不自动生效（submit 后 status=draft，需人工 decide）
    store = ProposalStore(conn, pack=pack)
    try:
        errors = validate_proposal(proposal, pack=pack, conn=conn)
        if errors:
            return {"ok": False, "proposal_id": None, "candidate": candidate,
                    "model": model_id, "error": f"提案校验失败：{errors}",
                    "provenance": proposal["_sort_hint"]}
        pid = store.submit(proposal)
        return {"ok": True, "proposal_id": pid, "candidate": candidate,
                "model": model_id, "error": None,
                "provenance": proposal["_sort_hint"]}
    except ProposalValidationError as e:
        return {"ok": False, "proposal_id": None, "candidate": candidate,
                "model": model_id, "error": str(e),
                "provenance": proposal["_sort_hint"]}
