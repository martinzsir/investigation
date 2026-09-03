"""
core/access.py
AccessContext 统一权限上下文（REQ-009）。

贯穿五个出口：语义层读网关（REQ-002）/ Function / Action / MCP / 导出。
设计要点：
  - 不可变（frozen）：构造后改字段抛 FrozenInstanceError（AC1）；
  - 缺 operator 硬失败（AC2）：谁在访问必须显式声明；
  - network="isolated" 时拒绝一切 LLM 调用（AC4，require_llm_allowed
    供后续 LLM 草案层 REQ-034~037 统一接线——内核纯离线，当前无 LLM 调用点，
    接口先行）；
  - access=None 的调用方一律取 system_context()：既有代码行为完全不变，
    REQ-010 的 fail-closed 只对显式传入的非 system 上下文生效。

角色分级（rank 码点序，越大权限越高）：
    system(99, 内部旁路) > human(4) > 主办(3) > 偏将(2) > 正兵(1) > 见习(0)
can_read_object / can_read_property 委托策略引擎（core/policy.py，REQ-010）；
本模块不反向 import policy，避免循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass

# 角色等级（策略引擎与 AccessContext 共用同一份定义）
ROLE_RANK: dict[str, int] = {
    "见习": 0,
    "正兵": 1,
    "偏将": 2,
    "主办": 3,
    "human": 4,
    "system": 99,
}

NETWORKS = ("local", "isolated")

# human 专属终态（can_transition 用；REQ-012 两阶段提交时细化）
HUMAN_ONLY_STATUSES = frozenset({"已立案"})


class LLMBlockedError(PermissionError):
    """network="isolated" 环境下发起 LLM 调用（REQ-009 AC4）。"""


@dataclass(frozen=True)
class AccessContext:
    """一次访问的主体/目的/环境声明。用法：
        ctx = AccessContext(operator="王检察官", role="human", purpose="案件复查")
        gw = OntologyReadGateway(conn, access=ctx)
    """

    operator: str
    role: str = "正兵"
    case_id: str = "default"
    purpose: str = ""
    clearance: int = 1
    network: str = "local"          # "local" | "isolated"

    def __post_init__(self):
        if not isinstance(self.operator, str) or not self.operator.strip():
            raise ValueError(
                "AccessContext 缺 operator（REQ-009 AC2）：谁在访问必须显式声明")
        if self.network not in NETWORKS:
            raise ValueError(
                f"network 必须是 {NETWORKS}，收到 {self.network!r}")
        if self.role not in ROLE_RANK:
            raise ValueError(
                f"未声明角色 {self.role!r}，可用 {sorted(ROLE_RANK)}")

    # ---- 派生 ----
    @property
    def rank(self) -> int:
        return ROLE_RANK[self.role]

    @property
    def is_system(self) -> bool:
        return self.role == "system"

    # ---- 判定接口（方案设计四 can_* + LLM）----
    def can_llm_call(self) -> bool:
        """isolated 网络禁止一切 LLM 调用（AC4）。"""
        return self.network == "local"

    def can_read_object(self, name: str, engine=None) -> bool:
        """对象级读权限：有策略引擎则委托（fail-closed），无则放行。
        can_* 语义返回 bool；需要拒绝原因时直接调 engine.check_object。"""
        if engine is None or self.is_system:
            return True
        try:
            engine.check_object(self, name)   # PolicyDeniedError ⊂ PermissionError
            return True
        except PermissionError:
            return False

    def can_read_property(self, obj: str, prop: str, engine=None) -> bool:
        """属性级读权限：有策略引擎则委托，无则放行。"""
        if engine is None:
            return True
        return engine.can_read_property(self, obj, prop)

    def can_invoke(self, function_name: str) -> bool:
        """Function 调用权限（当前全放行；REQ-027 阈值策略对象时细化）。"""
        return True

    def can_transition(self, from_status: str, to_status: str) -> bool:
        """状态迁移权限：human 专属终态（如"已立案"）仅 human/system 可达。"""
        if to_status in HUMAN_ONLY_STATUSES:
            return self.is_system or self.role == "human"
        return True


def system_context() -> AccessContext:
    """内部系统上下文：策略全旁路（access=None 的既有调用路径取此值）。"""
    return AccessContext(operator="system", role="system", clearance=99,
                         purpose="internal")


def require_llm_allowed(ctx: AccessContext) -> None:
    """LLM 调用闸门（REQ-009 AC4）：isolated 一律拒绝。"""
    if not ctx.can_llm_call():
        raise LLMBlockedError(
            f"network={ctx.network!r}（isolated）禁止 LLM 调用"
            f"（operator={ctx.operator}）；离线内核仅允许确定性计算")
