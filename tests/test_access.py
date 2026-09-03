"""
tests/test_access.py
REQ-009 AccessContext 统一权限上下文 测试。

覆盖 AC1-AC4：
  AC1: frozen 不可变，改字段抛 FrozenInstanceError
  AC2: 缺 operator 构造抛 ValueError
  AC3: 网关 / Function / Action 三处签名均接受 access 参数
  AC4: network="isolated" 时 LLM 调用被拒（require_llm_allowed / can_llm_call）
附加：角色分级旁路（system）、human 终态 can_transition、非法 network/角色硬失败。
"""
from __future__ import annotations

import dataclasses
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import (                                      # noqa: E402
    AccessContext, LLMBlockedError, ROLE_RANK, HUMAN_ONLY_STATUSES,
    require_llm_allowed, system_context,
)


class TestAccessContext(unittest.TestCase):
    def test_ac1_frozen_immutable(self):
        """AC1: AccessContext 不可变，改字段抛 FrozenInstanceError。"""
        ctx = AccessContext(operator="王检察官")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.operator = "李检察官"

    def test_ac2_operator_required(self):
        """AC2: 缺 operator 构造抛 ValueError（缺位为 TypeError，空串为 ValueError）。"""
        with self.assertRaises(ValueError):
            AccessContext(operator="")
        with self.assertRaises(ValueError):
            AccessContext(operator="   ")
        with self.assertRaises(TypeError):
            AccessContext()  # dataclass 必填位缺失

    def test_ac3_signatures_accept_access(self):
        """AC3: 网关 / Function / Action 三处签名均接受 access 参数。"""
        from core.gateway import OntologyReadGateway
        from core.functions import FunctionExecutor
        from core.action_executor import ActionExecutor

        for cls in (OntologyReadGateway, FunctionExecutor, ActionExecutor):
            sig = inspect.signature(cls.__init__)
            self.assertIn("access", sig.parameters,
                          f"{cls.__name__}.__init__ 缺 access 参数")
        # access=None → system 旁路（既有调用行为不变）
        ctx = system_context()
        self.assertTrue(ctx.is_system)
        self.assertEqual(ROLE_RANK["system"], 99)

    def test_ac4_isolated_blocks_llm(self):
        """AC4: network="isolated" 时任何 LLM 调用被拒绝。"""
        iso = AccessContext(operator="外勤终端", network="isolated")
        self.assertFalse(iso.can_llm_call())
        with self.assertRaises(LLMBlockedError):
            require_llm_allowed(iso)
        loc = AccessContext(operator="本机侦查员", network="local")
        self.assertTrue(loc.can_llm_call())
        require_llm_allowed(loc)  # 不抛即通过

    # ---- 附加 ----
    def test_invalid_network_and_role(self):
        with self.assertRaises(ValueError):
            AccessContext(operator="x", network="internet")
        with self.assertRaises(ValueError):
            AccessContext(operator="x", role="管理员")  # 未分级角色

    def test_can_transition_human_only_status(self):
        """human 专属终态（已立案）仅 human/system 可达。"""
        self.assertFalse(AccessContext(operator="正兵甲", role="正兵")
                         .can_transition("已固证", "已立案"))
        self.assertTrue(AccessContext(operator="王检察官", role="human")
                        .can_transition("已固证", "已立案"))
        self.assertTrue(system_context().can_transition("已固证", "已立案"))
        # 非 human 终态全放行
        self.assertTrue(AccessContext(operator="正兵甲", role="正兵")
                        .can_transition("待查", "查证中"))
        self.assertIn("已立案", HUMAN_ONLY_STATUSES)

    def test_engine_delegation(self):
        """can_read_object/can_read_property 委托策略引擎；engine=None 放行。"""
        from core.policy import PolicyEngine, PolicyDeniedError
        ctx = AccessContext(operator="正兵甲", role="正兵", clearance=1)
        pe = PolicyEngine()
        self.assertFalse(ctx.can_read_object("tipoff", engine=pe))
        self.assertTrue(ctx.can_read_object("tipoff", engine=None))
        self.assertTrue(ctx.can_read_object("person", engine=pe))
        with self.assertRaises(PolicyDeniedError):
            pe.check_object(ctx, "tipoff")   # 需要拒绝原因时直接走引擎


if __name__ == "__main__":
    unittest.main()
