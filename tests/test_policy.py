"""
tests/test_policy.py
REQ-010 对象级/属性级策略执行 测试。

覆盖 AC1-AC5：
  AC1: 低权限角色查 tipoff → 拒绝（PolicyDeniedError）
  AC2: 无权角色读 person.id_card → mask（310****1234）
  AC3: 有权角色读同一字段 → 原文
  AC4: 策略文件未声明的对象 → 默认拒绝（fail-closed）
  AC5: 策略覆盖率：objects.json 每个对象都有显式策略声明
附加：链接级策略、声明文件缺失 fail-closed、声明角色与 ROLE_RANK 不同源硬失败、
     网关集成（低权限读被拒、mask 生效、system 全旁路）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext, system_context          # noqa: E402
from core.policy import (                                      # noqa: E402
    PolicyEngine, PolicyDeniedError, mask_partial,
)
from core.ontology_loader import load_pack                     # noqa: E402
from tests.test_ontology_version import make_store             # noqa: E402


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine("default")

    def test_ac1_low_role_denied_tipoff(self):
        """AC1: 低权限角色查 tipoff（内间材料）→ 拒绝。"""
        ctx = AccessContext(operator="正兵甲", role="正兵", clearance=1)
        with self.assertRaises(PolicyDeniedError):
            self.engine.check_object(ctx, "tipoff")
        # 偏将（clearance>=2）放行
        self.engine.check_object(
            AccessContext(operator="偏将乙", role="偏将", clearance=2), "tipoff")

    def test_ac2_mask_partial(self):
        """AC2: 无权角色读 person.id_card → 部分遮蔽 310****1234。"""
        self.assertEqual(mask_partial("31012341234"), "310****1234")
        ctx = AccessContext(operator="正兵甲", role="正兵", clearance=1)
        rows = [{"raw_name": "张卫国", "id_card": "31012341234"}]
        masked = self.engine.apply_row_masks(ctx, "person", rows)
        self.assertEqual(masked[0]["id_card"], "310****1234")
        self.assertEqual(masked[0]["raw_name"], "张卫国")  # 非敏感列不动
        # 短串全遮
        self.assertEqual(mask_partial("310"), "***")
        self.assertEqual(mask_partial(None), "")

    def test_ac3_authorized_role_sees_raw(self):
        """AC3: 有权角色（主办）读同一字段 → 原文。"""
        ctx = AccessContext(operator="主办丙", role="主办", clearance=3)
        rows = [{"raw_name": "张卫国", "id_card": "31012341234"}]
        masked = self.engine.apply_row_masks(ctx, "person", rows)
        self.assertEqual(masked[0]["id_card"], "31012341234")
        # human 同权
        ctx_h = AccessContext(operator="王检察官", role="human", clearance=4)
        self.assertEqual(
            self.engine.apply_row_masks(ctx_h, "person", rows)[0]["id_card"],
            "31012341234")

    def test_ac4_undeclared_object_fail_closed(self):
        """AC4: 策略文件未声明的对象 → 默认拒绝。"""
        ctx = AccessContext(operator="主办丙", role="主办", clearance=3)
        with self.assertRaises(PolicyDeniedError):
            self.engine.check_object(ctx, "not_declared_object")
        # system 角色是唯一旁路
        self.engine.check_object(system_context(), "not_declared_object")

    def test_ac5_coverage_all_objects_declared(self):
        """AC5: objects.json 每个对象都有显式策略声明（含链接全覆盖）。"""
        pack = load_pack("default")
        missing = self.engine.coverage_missing({o.name for o in pack.objects})
        self.assertEqual(missing, [], f"缺对象级策略声明：{missing}")
        undeclared_links = ({l.name for l in pack.links}
                            - set(self.engine.link_policies))
        self.assertEqual(undeclared_links, set(),
                         f"缺链接级策略声明：{sorted(undeclared_links)}")

    # ---- 附加 ----
    def test_link_policies(self):
        ctx_low = AccessContext(operator="见习丁", role="见习", clearance=0)
        with self.assertRaises(PolicyDeniedError):
            self.engine.check_link(ctx_low, "transfers")
        self.engine.check_link(
            AccessContext(operator="正兵甲", role="正兵", clearance=1), "transfers")

    def test_missing_policy_file_fail_closed(self):
        """声明文件缺失 → fail-closed 全拒（不允许静默放行）。"""
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "emptypack"
            empty.mkdir()
            pe = PolicyEngine("emptypack", path=empty / "policies.json")
            with self.assertRaises(PolicyDeniedError):
                pe.check_object(AccessContext(operator="x"), "person")

    def test_unknown_role_in_policies_rejected(self):
        """声明了 ROLE_RANK 之外的角色 → 装载期硬失败（口径同源）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "policies.json"
            p.write_text(json.dumps({
                "schema_version": 2,
                "roles": {"管理员": 5},
                "object_policies": [{"object": "person", "roles": ["管理员"],
                                     "min_clearance": 0}],
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                PolicyEngine("default", path=p)

    def test_gateway_integration(self):
        """网关集成：低权限读被拒 / mask 生效 / system 旁路。"""
        from core.gateway import OntologyReadGateway
        from core.policy import PolicyDeniedError
        store = make_store()
        try:
            from core.ontology import build_ontology
            build_ontology(store.conn)
            low = AccessContext(operator="见习丁", role="见习", clearance=0)
            gw = OntologyReadGateway(store.conn, access=low)
            with self.assertRaises(PolicyDeniedError):
                gw.objects("transaction")   # 资金明细正兵及以上
            # 见习可读 person（min_clearance 0）
            persons = gw.objects("person")
            self.assertEqual(len(persons), 2)
            # system 全旁路
            gw_sys = OntologyReadGateway(store.conn)   # access=None → system
            self.assertEqual(len(gw_sys.objects("transaction")), 2)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
