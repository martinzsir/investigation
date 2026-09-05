"""
core/llm/explain.py
REQ-035 LLM 解释生成 + 证据覆盖检查。

生成面向分析师的摘要；每句结论必须挂证据 ID，无法映射的结论句标注为【假设】或剔除。
关键约束（与三条禁令一致）：
  - AC1: 每个事实断言能匹配 finding / source row
  - AC2: 未能映射的句子 → 标【假设】或剔除
  - AC3: 输出含 evidence coverage 百分比
  - AC4: 含"涉嫌/确认/违法/立案"等定性词 → 拒绝输出
  - AC5: 统一加免责声明
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
from typing import Any, Callable

from core.access import AccessContext
from core.llm.guard import sanitize_candidate, assert_no_status_change
from core.llm.llm_client import LLMClient
from core.llm.redact import build_redacted_context, call_llm
from core.proposal import ProposalStore, validate_proposal


# AC4: 定性词黑名单（出现即拒绝输出）
_QUALITATIVE_WORDS = re.compile(
    r"涉嫌|确认.{0,4}违法|已?违法|已?立案|认定.{0,4}犯罪|确系|"
    r"构成.{0,4}(犯罪|违法)|判定.{0,4}(有罪|犯罪)",
)

# AC5: 免责声明
_DISCLAIMER = "以下均为待核实线索，不构成办案指导。"

# 证据 URI 形态（用于 AC1 事实映射）
_EVIDENCE_URI_RE = re.compile(r"(obj|lnk)_[a-z_]+/[A-Za-z0-9_\-]+")


def generate_explanation(
    conn,
    ctx: AccessContext,
    findings: list[dict],
    *,
    pack: str = "default",
    llm_client: LLMClient | None = None,
    policy: dict | None = None,
) -> dict[str, Any]:
    """REQ-035: 生成解释 + 证据覆盖检查。

    Args:
        conn: DuckDB 连接
        ctx: AccessContext
        findings: 规则检测结果列表
        pack: ontology 案件包
        llm_client: LLM 客户端
        policy: LLM 策略

    Returns:
        {
            "ok": True/False,
            "explanation": str | None,     # 解释文本（含免责声明）
            "evidence_coverage": float,    # 0.0~1.0
            "sentences": list[dict],       # [{text, evidence_uris, is_assumption}]
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
                              source="generate_explanation")
    except LLMDegraded as e:
        return {"ok": False, "explanation": None, "evidence_coverage": 0.0,
                "sentences": [], "model": _model_hint or "offline",
                "error": str(e), "degraded": True}

    # 构造脱敏上下文
    redacted_ctx = build_redacted_context(findings, policy)

    # 收集所有可用证据 URI
    all_evidence_uris: set[str] = set()
    for f in findings:
        for uri in redacted_ctx.get("findings", []):
            all_evidence_uris.update(uri.get("evidence_uris", []))

    system_prompt = (
        "你是侦查线索摘要分析师。根据检测结果生成面向分析师的摘要。\n"
        "严格约束：\n"
        "1. 每句事实断言必须标注证据 URI（格式 obj_<类型>/<主键> 或 lnk_<类型>/<主键>）\n"
        "2. 无法标注证据的句子标为【假设】\n"
        "3. 不得使用定性词：涉嫌、确认违法、已立案、构成犯罪等\n"
        "4. 输出 JSON：{\"sentences\": [{\"text\": \"...\", \"evidence_uris\": [\"...\"], \"is_assumption\": false}]}\n"
        f"5. 可用证据 URI 池：{json.dumps(sorted(all_evidence_uris), ensure_ascii=False)}\n"
        "以 ```json``` 代码块输出。"
    )

    user_prompt = json.dumps({
        "检测结果（脱敏）": redacted_ctx,
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

    try:
        llm_result = call_llm(
            conn, ctx, policy,
            model=model_id,
            prompt=user_prompt,
            redacted_input=redacted_ctx,
            fake_invoke=_invoke,
        )
    except Exception as e:
        return {"ok": False, "explanation": None, "evidence_coverage": 0.0,
                "sentences": [], "model": model_id, "error": str(e)}

    raw_result = llm_result["result"]
    parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None

    if not parsed or "sentences" not in parsed:
        return {"ok": False, "explanation": None, "evidence_coverage": 0.0,
                "sentences": [], "model": model_id,
                "error": "LLM 输出无法解析为含 sentences 的 JSON"}

    sentences_raw = parsed["sentences"]
    sentences: list[dict] = []
    total = 0
    with_evidence = 0

    for s in sentences_raw:
        text = str(s.get("text", ""))
        uris = s.get("evidence_uris") or []
        is_assumption = bool(s.get("is_assumption", False))

        # AC4: 定性词检查
        if _QUALITATIVE_WORDS.search(text):
            continue  # 剔除含定性词的句子

        # AC1/AC2: 证据映射检查
        if not uris and not is_assumption:
            is_assumption = True  # 无证据且未标注假设 → 强制标为假设

        # 验证 URI 格式
        valid_uris = [u for u in uris if _EVIDENCE_URI_RE.match(str(u))]

        total += 1
        if valid_uris and not is_assumption:
            with_evidence += 1

        sentences.append({
            "text": text,
            "evidence_uris": valid_uris,
            "is_assumption": is_assumption,
        })

    # AC3: 证据覆盖百分比
    coverage = with_evidence / total if total > 0 else 0.0

    # AC5: 拼接免责声明
    explanation_text = _DISCLAIMER + "\n\n"
    for s in sentences:
        prefix = "【假设】" if s["is_assumption"] else ""
        evidence_tag = f" [证据: {', '.join(s['evidence_uris'])}]" if s["evidence_uris"] else ""
        explanation_text += f"- {prefix}{s['text']}{evidence_tag}\n"

    # AC4: 最终检查整段文本
    if _QUALITATIVE_WORDS.search(explanation_text):
        return {"ok": False, "explanation": None, "evidence_coverage": 0.0,
                "sentences": [], "model": model_id,
                "error": "解释含定性词，拒绝输出（AC4）"}

    return {
        "ok": True,
        "explanation": explanation_text,
        "evidence_coverage": coverage,
        "sentences": sentences,
        "model": model_id,
        "error": None,
    }
