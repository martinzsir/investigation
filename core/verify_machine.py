"""
core/verify_machine.py
核查工作区 REQ-V-003：核查项状态机——单一事实源（纯函数、无 I/O）。

定位（实施方案 ADR-V-1 / REQ-V-003）：
  - 合法转移表只在此处声明一次：Worker 写通道（REQ-V-004）据此拒绝非法迁移，
    server 方法层不重复校验；前端 domain/verify.ts 以同构表镜像（按钮显隐、
    vitest 直测同一张表），两侧语义以本文件为准；
  - 纯函数无 I/O、无 DB 依赖：不读 state、不写审计（审计由 REQ-V-004
    落 audit_chain，终态重开的旧结论在 before/after 中可溯）。

状态全图：
    建议   → 待核查（采纳，可带改写文本）| 已忽略（忽略）
    已忽略 → 待核查（重新采纳）
    待核查 → 核查中 | 已证实 | 已查否 | 无法核实
    核查中 → 已证实 | 已查否 | 无法核实 | 待核查（退回）
    已证实 / 已查否 / 无法核实 → 核查中（重开，翻案合法，须具名 operator）

注：「建议/已忽略」是手册建议项生命周期（REQ-V-018），采纳前不是正式
核查任务，不进固证门禁 pending 计数。
"""
from __future__ import annotations

# ---- 状态（展示序亦用于列表 rank，与 state_store SQL rank 保持一致）----
SUGGESTED = "建议"
PENDING = "待核查"
IN_PROGRESS = "核查中"
CONFIRMED = "已证实"
DISPROVED = "已查否"
UNVERIFIABLE = "无法核实"
IGNORED = "已忽略"

VERIFY_STATUSES: tuple[str, ...] = (
    SUGGESTED, PENDING, IN_PROGRESS,
    CONFIRMED, DISPROVED, UNVERIFIABLE, IGNORED,
)

# ---- 合法转移（白名单；未列出即非法）----
VERIFY_TRANSITIONS: dict[str, tuple[str, ...]] = {
    SUGGESTED: (PENDING, IGNORED),          # 采纳（可带改写文本）/ 忽略
    IGNORED: (PENDING,),                    # 重新采纳
    PENDING: (IN_PROGRESS, CONFIRMED, DISPROVED, UNVERIFIABLE),
    IN_PROGRESS: (CONFIRMED, DISPROVED, UNVERIFIABLE, PENDING),  # 末项=退回
    CONFIRMED: (IN_PROGRESS,),              # 重开（翻案）
    DISPROVED: (IN_PROGRESS,),
    UNVERIFIABLE: (IN_PROGRESS,),
}

# 终态裁决：必须随附结论（无法核实可不填但建议填写）
CONCLUSION_REQUIRED: frozenset[str] = frozenset({CONFIRMED, DISPROVED})

# 门禁计数口径（REQ-V-008；state_store.verify_progress 同源引用）：
# pending 只统计 待核查/核查中；建议、已忽略独立计数，不进门禁。
VERIFY_PENDING_STATUSES: tuple[str, ...] = (PENDING, IN_PROGRESS)
VERIFY_CONCLUDED_STATUSES: tuple[str, ...] = (
    CONFIRMED, DISPROVED, UNVERIFIABLE)

# ---- 错误码（Worker TaskExecError 三态映射用，REQ-V-004）----
ERR_INVALID_TRANSITION = "INVALID_TRANSITION"
ERR_UNKNOWN_STATUS = "UNKNOWN_STATUS"
ERR_CONCLUSION_REQUIRED = "CONCLUSION_REQUIRED"


class VerifyTransitionError(ValueError):
    """状态机校验失败；code 取 ERR_* 常量，供上层稳定映射任务错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def can_transition(cur: str, nxt: str) -> bool:
    """转移是否合法（未知状态一律 False；不抛异常，供按钮显隐/路由判断）。"""
    return nxt in VERIFY_TRANSITIONS.get(cur, ())


def validate_verify_transition(cur: str, nxt: str) -> None:
    """校验单步转移；非法迁移/未知状态 raise VerifyTransitionError(ValueError)。

    自迁移（待核查→待核查）不在白名单，按非法处理。
    """
    if cur not in VERIFY_STATUSES:
        raise VerifyTransitionError(
            ERR_UNKNOWN_STATUS, f"未知核查项状态：{cur!r}")
    if nxt not in VERIFY_STATUSES:
        raise VerifyTransitionError(
            ERR_UNKNOWN_STATUS, f"未知核查项目标状态：{nxt!r}")
    if not can_transition(cur, nxt):
        raise VerifyTransitionError(
            ERR_INVALID_TRANSITION,
            f"非法核查项状态迁移：{cur} → {nxt}（允许："
            f"{'/'.join(VERIFY_TRANSITIONS.get(cur, ())) or '无'}）")


def validate_conclusion(nxt: str, conclusion: str | None) -> None:
    """终态裁决结论必填校验：已证实/已查否 缺结论 → CONCLUSION_REQUIRED。

    无法核实不强制（侦查实务允许挂起转外部调取）。仅校验，不判转移合法性。
    """
    if nxt in CONCLUSION_REQUIRED and not (conclusion or "").strip():
        raise VerifyTransitionError(
            ERR_CONCLUSION_REQUIRED,
            f"裁决为「{nxt}」必须填写核查结论（CONCLUSION_REQUIRED）")


def legal_targets(cur: str) -> tuple[str, ...]:
    """当前状态的合法目标元组（未知状态返回空元组，不抛异常）。"""
    return VERIFY_TRANSITIONS.get(cur, ())
