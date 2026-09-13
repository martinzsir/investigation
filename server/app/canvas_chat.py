"""
server/app/canvas_chat.py
RC-301 画布只读问答（RC-302 引用校验串联）。

流程：
  1. 从画布文档构建业务视图上下文（规则判据/事实/假设/核实项/书证/行摘要）；
  2. redact_payload 脱敏 + redaction_hash（call_llm 闸门验真）；
  3. 走 call_llm 闸门（llm_enabled / network / model 白名单 / 脱敏双保险）；
  4. LLM 输出过 citation_guard：有据句入 facts、无据句入 pending、假引用剔除；
  5. 返回 {answer, facts, pending, warnings, citations}；不写画布（只读）。

纪律：
  - 问答不产生任何画布写入（只读断言，RC-301 AC4）；
  - 发送给模型的上下文不含 denied 字段明文（复用 redact_payload）；
  - llm_enabled=false 或 network=isolated 时由 call_llm 闸门拒绝，返回业务错误；
  - 回答引用角标全部能在当刻文档中解析到节点/行；解析不到的引用被剔除。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from core.access import AccessContext, LLMBlockedError
from core.llm.draft_verify import select_deployment, _deployment_policy
from core.llm.llm_client import LLMClient
from core.llm.redact import (
    call_llm,
    load_llm_policy,
    redact_payload,
)

from server.app.canvas_citation_guard import (
    build_valid_refs_from_doc,
    validate_citations,
)

# 引用标记：LLM 输出中事实句用 [cite:<ref>] 锚定证据
CITE_MARK = "[cite:{}]"

SYSTEM_PROMPT = (
    "你是线索研判助手。基于当前画布的业务视图（规则判据、事实、假设、"
    "核实项状态、书证、数据行摘要）回答分析师的问题。\n"
    "严格约束：\n"
    "1. 每条事实性陈述必须紧跟引用标记 [cite:<ref>]，ref 取自下方"
    "「可用引用」清单中给出的节点 ref 或 row_uri；不得编造引用。\n"
    "2. 无法从画布证据支撑的表述，归入「模型推测」，不要伪装成事实。\n"
    "3. 不输出任何状态变更指令（立案/核实/固证）或写操作建议。\n"
    "4. 回答用中文，简洁，事实句与推测句分段。"
)


def build_canvas_context(doc: dict[str, Any]) -> dict[str, Any]:
    """从画布文档构建业务视图上下文（脱敏前的结构化载荷）。

    只含业务文本：规则判据、事实、假设、核实项状态、书证、行摘要；
    不含原始数据行明细值（行只给 ref + label 摘要）。
    """
    nodes = doc.get("nodes", []) or []
    rules = []
    facts = []
    hypotheses = []
    verify_items = []
    evidences = []
    rows = []
    valid_refs: set[str] = set()

    for n in nodes:
        ref = str(n.get("ref", ""))
        kind = n.get("kind", "")
        label = n.get("label", "")
        props = n.get("props") or {}
        if ref:
            valid_refs.add(ref)

        if kind == "rule":
            rules.append({
                "ref": ref,
                "判据": props.get("rule_text") or label,
                "依据": props.get("依据", ""),
            })
        elif kind == "fact":
            facts.append({"ref": ref, "事实": label or props.get("text", "")})
        elif kind == "hypothesis":
            hypotheses.append({
                "ref": ref,
                "标题": props.get("title", label),
                "内容": props.get("content", ""),
            })
        elif kind == "verify_item":
            verify_items.append({
                "ref": ref,
                "核实项": label,
                "状态": props.get("status", "建议"),
            })
        elif kind == "evidence":
            evidences.append({"ref": ref, "书证": label})
        elif kind == "source_row":
            rows.append({"ref": ref, "摘要": label})

    return {
        "规则": rules,
        "事实": facts,
        "假设": hypotheses,
        "待核实": verify_items,
        "书证": evidences,
        "数据行摘要": rows,
        "可用引用": sorted(valid_refs),
    }


def _make_user_prompt(question: str, context: dict[str, Any]) -> str:
    return json.dumps({
        "画布业务视图": context,
        "分析师问题": question,
        "输出要求": (
            "事实性陈述必须带 [cite:<ref>]，ref 从「可用引用」中选；"
            "无据内容归入「模型推测」段。"
        ),
    }, ensure_ascii=False, indent=2)


def chat_on_canvas(
    *,
    conn,
    ctx: AccessContext,
    doc: dict[str, Any],
    question: str,
    pack_id: str = "default",
    base_dir=None,
    llm_client: LLMClient | None = None,
) -> dict[str, Any]:
    """画布只读问答主流程。

    返回：
      {ok, answer, facts, pending, warnings, citations, model, error?}
    ok=False 时含 error（LLM 被拒/失败的业务原因）。
    """
    policy = load_llm_policy(pack_id, base_dir=base_dir)

    # llm_enabled 总开关（REQ-040）
    if not policy.get("llm_enabled", False):
        return {
            "ok": False,
            "answer": "",
            "facts": [],
            "pending": [],
            "warnings": ["当前案件未启用智能问答（llm_enabled=false）"],
            "citations": [],
            "model": None,
            "error": "llm_disabled",
        }

    # 部署档选择（local/web 交集，fail-closed）
    mode, dep, off_reason = select_deployment(ctx, policy)
    if mode == "off" or dep is None:
        return {
            "ok": False,
            "answer": "",
            "facts": [],
            "pending": [],
            "warnings": [f"智能问答不可用：{off_reason}"],
            "citations": [],
            "model": None,
            "error": "deployment_off",
        }

    eff_policy = _deployment_policy(policy, dep)
    model = (dep.get("allowed_models") or [""])[0] or policy.get(
        "allowed_models", [""])[0]
    if not model:
        return {
            "ok": False,
            "answer": "", "facts": [], "pending": [],
            "warnings": ["无可用模型"],
            "citations": [], "model": None, "error": "no_model",
        }

    # 构建上下文 + 脱敏
    context = build_canvas_context(doc)
    redacted = redact_payload(context, eff_policy)
    blob = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    redacted["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    user_prompt = _make_user_prompt(question, context)

    client = llm_client or LLMClient()

    def _invoke(model, prompt, redacted_input):  # pragma: no cover - 生产路径
        resp = client.chat(
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=2048)
        if resp.get("ok"):
            return resp["result"]
        raise RuntimeError(resp.get("error", "LLM 调用失败"))

    try:
        llm_result = call_llm(
            conn, ctx, eff_policy, model=model, prompt=user_prompt,
            redacted_input=redacted, fake_invoke=_invoke)
    except LLMBlockedError as e:
        return {
            "ok": False, "answer": "", "facts": [], "pending": [],
            "warnings": [f"智能问答被治理策略拒绝：{e}"],
            "citations": [], "model": model, "error": "llm_blocked",
        }
    except Exception as e:
        return {
            "ok": False, "answer": "", "facts": [], "pending": [],
            "warnings": [f"智能问答失败：{e}"],
            "citations": [], "model": model, "error": "llm_error",
        }

    raw = llm_result.get("result") or {}
    raw_content = raw.get("content", "") if isinstance(raw, dict) else str(raw)

    # RC-302 引用校验
    valid_refs = build_valid_refs_from_doc(doc)
    guarded = validate_citations(raw_content, valid_refs)

    return {
        "ok": True,
        "answer": raw_content,
        "facts": guarded["facts"],
        "pending": guarded["pending"],
        "warnings": guarded["warnings"],
        "citations": [c for f in guarded["facts"] for c in f["citations"]],
        "model": model,
    }


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:16]
