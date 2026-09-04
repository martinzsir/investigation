"""
core/llm/align.py
REQ-036 LLM 实体对齐预标注。

LLM 只看最小必要片段，输出 merge_risk / support / conflict / question_for_operator。
关键约束（与三条禁令一致）：
  - AC1: LLM 输出不改变 needs_human_review 标志
  - AC2: LLM 不得预置 accept（字段断言）
  - AC3: 含已知错误合并陷阱 → 自动合并率不上升
  - AC4: LLM 建议全拒时系统行为与原确定性版一致
  - AC5: 输入含真实身份信息 → 脱敏后才发送（REQ-038 联动）
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any, Callable

from core.access import AccessContext
from core.llm.guard import sanitize_candidate, assert_no_status_change, scan_text
from core.llm.llm_client import LLMClient
from core.llm.redact import build_redacted_context, redact_payload, call_llm
from core.proposal import ProposalStore, validate_proposal


def align_entities(
    conn,
    ctx: AccessContext,
    entities: list[dict],
    *,
    pack: str = "default",
    llm_client: LLMClient | None = None,
    policy: dict | None = None,
) -> dict[str, Any]:
    """REQ-036: LLM 实体对齐预标注。

    Args:
        conn: DuckDB 连接
        ctx: AccessContext
        entities: 待对齐实体列表 [{name, type, source, attrs...}]
        pack: ontology 案件包
        llm_client: LLM 客户端
        policy: LLM 策略

    Returns:
        {
            "ok": True/False,
            "reviews": list[dict],   # [{entity_pair, merge_risk, support, conflict, question_for_operator}]
            "needs_human_review": True,  # AC1: 恒为 True（LLM 不改变此标志）
            "auto_merged": 0,            # AC2/AC4: 恒为 0（不自动合并）
            "model": str,
            "error": str | None,
        }
    """
    from core.llm.redact import load_llm_policy
    policy = policy or load_llm_policy(pack)

    # REQ-040：一键降级开关——关闭时零模型调用，落审计后静默回落（AC1/AC4）
    from core.llm.fallback import ensure_llm_capability, LLMDegraded
    _model_hint = getattr(llm_client, "model", None)
    try:
        ensure_llm_capability(conn, ctx, policy, model=_model_hint,
                              source="align_entities")
    except LLMDegraded as e:
        return {"ok": False, "reviews": [], "needs_human_review": True,
                "auto_merged": 0, "model": _model_hint or "offline",
                "error": str(e), "degraded": True}

    # AC5: 脱敏实体信息后才发送
    redacted_entities = redact_payload(entities, policy)

    system_prompt = (
        "你是实体对齐分析助手。判断两个实体是否可能指同一主体。\n"
        "严格约束：\n"
        "1. 只输出分析意见，不得做出合并决定\n"
        "2. 不得预置 accept（不接受自动合并）\n"
        "3. 输出 JSON：{\"reviews\": [{\"entity_pair\": [\"A\", \"B\"], "
        "\"merge_risk\": \"high/medium/low\", \"support\": [\"...\"], "
        "\"conflict\": [\"...\"], \"question_for_operator\": \"...\"}]}\n"
        "4. merge_risk=high 表示合并风险高（可能是不同人），low 表示可能同一人\n"
        "5. support 是支持合并的证据，conflict 是反对合并的证据\n"
        "6. question_for_operator 是需操作员确认的问题\n"
        "以 ```json``` 代码块输出。"
    )

    user_prompt = json.dumps({
        "待对齐实体（脱敏）": redacted_entities,
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    client = llm_client or LLMClient()
    model_id = client.model

    def _invoke(model, prompt, redacted_input):
        resp = client.chat_json(messages, temperature=0.2, max_tokens=2048)
        if resp.get("ok"):
            return resp["result"]
        return resp

    # 构造 redacted_input（带 redaction_hash）
    redacted_input = {
        "schema": "sunzi.align_input/v1",
        "entities": redacted_entities,
    }
    import hashlib
    blob = json.dumps(redacted_input, ensure_ascii=False, sort_keys=True, default=str)
    redacted_input["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    try:
        llm_result = call_llm(
            conn, ctx, policy,
            model=model_id,
            prompt=user_prompt,
            redacted_input=redacted_input,
            fake_invoke=_invoke,
        )
    except Exception as e:
        return {"ok": False, "reviews": [], "needs_human_review": True,
                "auto_merged": 0, "model": model_id, "error": str(e)}

    raw_result = llm_result["result"]
    parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None

    if not parsed or "reviews" not in parsed:
        return {"ok": False, "reviews": [], "needs_human_review": True,
                "auto_merged": 0, "model": model_id,
                "error": "LLM 输出无法解析为含 reviews 的 JSON"}

    # AC2: 清洗候选——不得含 accept/approved/merged 等字段
    reviews: list[dict] = []
    for r in parsed["reviews"]:
        clean, _dropped = sanitize_candidate(r, "alignment_review")
        assert_no_status_change(clean, "alignment_review")
        # AC2: 强制删除任何 accept 字段
        clean.pop("accept", None)
        clean.pop("accepted", None)
        clean.pop("auto_merge", None)
        reviews.append(clean)

    # AC1: needs_human_review 恒为 True（LLM 不改变此标志）
    # AC4: auto_merged 恒为 0（LLM 建议不自动执行）
    return {
        "ok": True,
        "reviews": reviews,
        "needs_human_review": True,
        "auto_merged": 0,
        "model": model_id,
        "error": None,
    }
