"""
core/llm/fallback.py
REQ-040 LLM 降级开关与影子模式。

边界：
  - 一键开关：llm_policy.json 的 "llm_enabled": false 关闭全部 LLM 能力
    （默认策略即关闭，内核纯离线立场）；关闭时草案器必须回落确定性路径，
    拒绝原因落 llm_call_log + AuditChain（AC4 降级审计）；
  - 影子模式（"mode": "shadow"，默认）：LLM 草案只产 proposal/日志/审计，
    永远不进生产 finding 集合、不写语义层——shadow_diff 以表行数快照断言；
  - LLMDegraded 是"能力不可用"的显式信号，区别于 LLMBlockedError（闸门拒绝）。
"""
from __future__ import annotations

from typing import Any

# 影子模式下允许因 LLM 活动而变化行数的表（提案 / 调用日志 / 审计链）
LLM_OWNED_TABLES = frozenset({"proposal", "llm_call_log", "audit_chain"})


class LLMDegraded(RuntimeError):
    """LLM 能力被策略关闭（一键开关）；调用方须静默回落确定性路径。"""


def llm_state(policy: dict | None) -> dict[str, Any]:
    """返回 {enabled, reason}：唯一关闭条件是 policy.llm_enabled is False。"""
    if isinstance(policy, dict) and policy.get("llm_enabled") is False:
        return {"enabled": False,
                "reason": "llm_enabled=false：LLM 能力已被策略一键关闭（REQ-040 AC3）"}
    return {"enabled": True, "reason": None}


def ensure_llm_capability(conn, ctx, policy: dict | None = None, *,
                          model: str | None = None, source: str = "llm") -> None:
    """能力门：关闭时落 llm_call_log + AuditChain 后抛 LLMDegraded（AC4）。

    与 call_llm 闸门的分工：本门只看"总开关"；network/model 白名单、脱敏复扫
    仍由 redact.call_llm 负责。草案器在构建上下文之前先过本门，保证关闭时
    零模型调用、零提案写入。
    """
    from core.llm.redact import load_llm_policy, log_llm_call

    pol = policy if policy is not None else load_llm_policy()
    state = llm_state(pol)
    if state["enabled"]:
        return
    reason = f"{state['reason']}；调用方={source}"
    log_llm_call(
        conn, operator=getattr(ctx, "operator", "unknown"),
        network=getattr(ctx, "network", "isolated"),
        model=model, allowed=False, blocked_reason=reason)
    raise LLMDegraded(reason)


def table_snapshot(conn) -> dict[str, int]:
    """全表行数快照（影子隔离断言用）。"""
    out: dict[str, int] = {}
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'").fetchall()
    for (t,) in rows:
        try:
            out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except Exception:
            out[t] = -1
    return out


def shadow_diff(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """对比两次快照：返回非 LLM 所有表的行数增量（空 dict = 影子隔离成立，AC2）。

    只允许 proposal / llm_call_log / audit_chain 三表变化；语义层 obj_*/lnk_*、
    线索表、action_request、writeback_outbox 等任何增量都算越界。
    """
    changed: dict[str, int] = {}
    for t, n in after.items():
        delta = n - before.get(t, 0)
        if delta != 0 and t not in LLM_OWNED_TABLES:
            changed[t] = delta
    return changed
