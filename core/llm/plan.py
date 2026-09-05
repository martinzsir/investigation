"""
core/llm/plan.py
REQ-037 LLM 意图 → Function 选择与参数填充。

将自然语言查询意图映射到既有 Function 并填充参数。
关键约束（与三条禁令一致）：
  - AC1: 合法意图 → 选出正确 function 且参数通过 schema
  - AC2: 未知 function 名 → 硬失败
  - AC3: 参数越界 → 硬失败，不让模型重试绕过
  - AC4: 高影响查询需人工批准（行数/范围阈值）
  - AC5: 查询计划可 explain()
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from core.access import AccessContext
from core.functions import check_param_value, FUNCTION_IMPLS
from core.llm.guard import sanitize_candidate, assert_no_status_change, scan_text
from core.llm.llm_client import LLMClient
from core.llm.redact import call_llm
from core.ontology_loader import load_pack


# AC4: 高影响阈值
HIGH_IMPACT_ROW_THRESHOLD = 10000


class IntentPlanError(ValueError):
    """意图→Function 映射失败（AC2/AC3 硬失败）。"""


def plan_query(
    conn,
    ctx: AccessContext,
    intent: str,
    *,
    pack: str = "default",
    llm_client: LLMClient | None = None,
    policy: dict | None = None,
) -> dict[str, Any]:
    """REQ-037: LLM 意图 → Function 选择与参数填充。

    Args:
        conn: DuckDB 连接
        ctx: AccessContext
        intent: 自然语言查询意图（如"查张三的季度末整数存款"）
        pack: ontology 案件包
        llm_client: LLM 客户端
        policy: LLM 策略

    Returns:
        {
            "ok": True/False,
            "plan": {  # AC5: 可 explain() 的查询计划
                "function": str,
                "params": dict,
                "estimated_rows": int | None,
                "needs_approval": bool,  # AC4
                "explain": str,          # AC5
            },
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
                              source="plan_query")
    except LLMDegraded as e:
        return {"ok": False, "plan": None, "model": _model_hint or "offline",
                "error": str(e), "degraded": True}

    spec = load_pack(pack)
    func_names = sorted(spec.functions.keys())

    # 构造函数清单描述
    func_descs = []
    for name in func_names:
        fn = spec.functions[name]
        params_desc = {}
        for pname, pspec in (fn.parameters or {}).items():
            params_desc[pname] = {"type": pspec.get("type"),
                                   "default": pspec.get("default"),
                                   "enum": pspec.get("enum")}
        func_descs.append({
            "name": name,
            "title": fn.title,
            "description": fn.description,
            "parameters": params_desc,
        })

    system_prompt = (
        "你是侦查查询意图分析助手。将自然语言查询意图映射到既有 Function。\n"
        "严格约束：\n"
        "1. function 必须从白名单选择，不得自创\n"
        "2. 参数必须符合 schema（类型/枚举/数值范围）\n"
        "3. 不得重试绕过参数校验\n"
        "4. 输出 JSON：{\"function\": \"...\", \"params\": {...}, \"reasoning\": \"...\"}\n"
        f"5. 可用函数清单：{json.dumps(func_descs, ensure_ascii=False)}\n"
        "以 ```json``` 代码块输出。"
    )

    user_prompt = json.dumps({
        "查询意图": intent,
    }, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    client = llm_client or LLMClient()
    model_id = client.model

    # 构造 redacted_input
    redacted_input = {
        "schema": "sunzi.plan_input/v1",
        "intent": intent,
        "available_functions": func_names,
    }
    import hashlib
    blob = json.dumps(redacted_input, ensure_ascii=False, sort_keys=True, default=str)
    redacted_input["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

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
            redacted_input=redacted_input,
            fake_invoke=_invoke,
        )
    except Exception as e:
        return {"ok": False, "plan": None, "model": model_id, "error": str(e)}

    raw_result = llm_result["result"]
    parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None

    if not parsed:
        return {"ok": False, "plan": None, "model": model_id,
                "error": "LLM 输出无法解析为 JSON"}

    func_name = parsed.get("function")
    params = parsed.get("params") or {}

    # AC2: 未知 function → 硬失败
    if not func_name or func_name not in spec.functions:
        raise IntentPlanError(
            f"未知 function 名 {func_name!r}，白名单 {func_names}（AC2 硬失败）")

    fn_spec = spec.functions[func_name]

    # AC3: 参数越界 → 硬失败（不让模型重试绕过）
    spec_params = fn_spec.parameters or {}
    for pname, pval in params.items():
        if pname not in spec_params:
            raise IntentPlanError(
                f"参数 {pname!r} 不在函数 {func_name!r} 声明的 "
                f"parameters {sorted(spec_params)} 中（AC3 硬失败）")
        try:
            check_param_value(pname, spec_params[pname], pval,
                              ctx=f"plan_query[{func_name}]")
        except ValueError as e:
            raise IntentPlanError(f"参数 {pname!r} 校验失败：{e}（AC3 硬失败）") from e

    # AC4: 高影响判断
    estimated_rows = None
    needs_approval = False
    if fn_spec.output_type == "rows":
        try:
            from core.functions import execute_function
            result = execute_function(conn, func_name, params, pack=pack)
            if isinstance(result, list):
                estimated_rows = len(result)
                needs_approval = estimated_rows > HIGH_IMPACT_ROW_THRESHOLD
        except Exception:
            pass  # 执行失败不影响计划生成

    # AC5: explain()
    explain_text = (
        f"Function: {func_name}\n"
        f"Title: {fn_spec.title}\n"
        f"Params: {json.dumps(params, ensure_ascii=False)}\n"
        f"Output type: {fn_spec.output_type}\n"
        f"Estimated rows: {estimated_rows}\n"
        f"Needs approval: {needs_approval}\n"
        f"Description: {fn_spec.description}"
    )

    return {
        "ok": True,
        "plan": {
            "function": func_name,
            "params": params,
            "estimated_rows": estimated_rows,
            "needs_approval": needs_approval,
            "explain": explain_text,
        },
        "model": model_id,
        "error": None,
    }
