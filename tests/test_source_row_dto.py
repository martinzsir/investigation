"""
tests/test_source_row_dto.py
B1 溯源行适配器测试。

覆盖：
  - 纯数据 dict → 字段表（路径 A，当前 demo_case 的实际形态）
  - hit 标记
  - 遮蔽（id_card partial → 310****1234，正兵无权；主办原文）
  - 未声明属性 visible（fail-closed 只管对象级，属性级无声明=允许）
  - 内部字段跳过（row_uri / knowledge_sources / knowledge_version）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext                            # noqa: E402
from server.app.source_row_dto import resolve_source_row        # noqa: E402


class TestSourceRowDto(unittest.TestCase):
    """溯源行适配器测试。"""

    # ---- 路径 A：纯数据 dict ----
    def test_pure_data_dict_to_fields(self):
        """纯数据 dict → 字段表（无 URI 回退路径）。"""
        sr = {"raw_name": "宏业建设", "legal_rep": "李志强",
              "relation": "", "matched_person": ["李志强"]}
        result = resolve_source_row(source_row=sr, pack_id="default")
        names = [f["name"] for f in result["fields"]]
        self.assertIn("raw_name", names)
        self.assertIn("legal_rep", names)
        # 内部字段跳过
        self.assertNotIn("matched_person", names)
        # 值正确
        raw_name = next(f for f in result["fields"] if f["raw"] == "raw_name")
        self.assertEqual(raw_name["value"], "宏业建设")

    def test_hit_fields_marked(self):
        """hit_fields 参数标记命中字段。"""
        sr = {"caller_raw": "张卫国", "callee_raw": "李志强", "times": 12}
        result = resolve_source_row(
            source_row=sr, pack_id="default",
            hit_fields=["caller_raw", "times"])
        caller = next(f for f in result["fields"] if f["raw"] == "caller_raw")
        self.assertTrue(caller["hit"])
        callee = next(f for f in result["fields"] if f["raw"] == "callee_raw")
        self.assertFalse(callee["hit"])

    def test_internal_fields_skipped(self):
        """row_uri/knowledge_sources/knowledge_version 跳过。"""
        sr = {"row_uri": "x@1#p/abc",
               "raw_name": "test", "knowledge_sources": ["a"], "knowledge_version": "v1"}
        result = resolve_source_row(source_row=sr, pack_id="default")
        names = [f["raw"] for f in result["fields"]]
        self.assertNotIn("row_uri", names)
        self.assertNotIn("knowledge_sources", names)
        self.assertNotIn("knowledge_version", names)

    # ---- 遮蔽 ----
    def test_id_card_masked_for_low_role(self):
        """正兵读 person.id_card → policy=masked, mask=idcard（前端 MaskedField 遮蔽）。"""
        sr = {"raw_name": "张三", "id_card": "310101199001011234"}
        access = AccessContext(operator="王正兵", role="正兵", clearance=1)
        result = resolve_source_row(
            source_row=sr, pack_id="default",
            access=access, hit_fields=["id_card"])
        id_field = next(f for f in result["fields"] if f["raw"] == "id_card")
        self.assertEqual(id_field["policy"], "masked")
        self.assertEqual(id_field["mask"], "idcard")
        # 遮蔽由前端执行，服务端送原值（非 denied 字段不截断）
        self.assertEqual(id_field["value"], "310101199001011234")
        self.assertTrue(id_field["hit"])

    def test_id_card_visible_for_high_role(self):
        """主办读 person.id_card → 原文。"""
        sr = {"raw_name": "张三", "id_card": "310101199001011234"}
        access = AccessContext(operator="赵主办", role="主办", clearance=3)
        result = resolve_source_row(
            source_row=sr, pack_id="default", access=access)
        id_field = next(f for f in result["fields"] if f["raw"] == "id_card")
        self.assertEqual(id_field["policy"], "visible")
        self.assertEqual(id_field["value"], "310101199001011234")

    def test_system_role_no_masking(self):
        """system 角色无遮蔽。"""
        sr = {"raw_name": "张三", "id_card": "310101199001011234"}
        access = AccessContext(operator="sys", role="system", clearance=99)
        result = resolve_source_row(
            source_row=sr, pack_id="default", access=access)
        id_field = next(f for f in result["fields"] if f["raw"] == "id_card")
        self.assertEqual(id_field["policy"], "visible")

    def test_no_policy_declared_visible(self):
        """未声明属性级策略 → visible（属性级无声明=允许，fail-closed 只管对象级）。"""
        sr = {"raw_name": "张三", "raw_addr": "某路1号"}
        access = AccessContext(operator="王正兵", role="正兵", clearance=1)
        result = resolve_source_row(
            source_row=sr, pack_id="default", access=access)
        addr = next(f for f in result["fields"] if f["raw"] == "raw_addr")
        self.assertEqual(addr["policy"], "visible")

    def test_tipoff_content_denied_for_low_role(self):
        """正兵读 tipoff.content_raw → masked（partial 遮蔽）。"""
        sr = {"content_raw": "举报人张三称...", "reporter_raw": "举报人"}
        access = AccessContext(operator="王正兵", role="正兵", clearance=1)
        result = resolve_source_row(
            source_row=sr, pack_id="default", access=access)
        # tipoff 未声明对象级策略给正兵 → 但属性级策略仍会遮蔽
        # 注意：source_row_dto 不做对象级检查（那是 OntologyReadGateway 的职责），
        # 只做属性级遮蔽
        content = next(f for f in result["fields"] if f["raw"] == "content_raw")
        self.assertEqual(content["policy"], "masked")
        # masked 字段送原值，前端 MaskedField 按 mask type 遮蔽
        self.assertEqual(content["value"], "举报人张三称...")

    # ---- 无 access 上下文 ----
    def test_no_access_no_masking(self):
        """无 access 参数 → visible（不遮蔽，调用方保证已授权）。"""
        sr = {"raw_name": "张三", "id_card": "310101199001011234"}
        result = resolve_source_row(source_row=sr, pack_id="default")
        id_field = next(f for f in result["fields"] if f["raw"] == "id_card")
        self.assertEqual(id_field["policy"], "visible")

    # ---- 值类型 ----
    def test_list_value_stringified(self):
        """list 值转 JSON 字符串。"""
        sr = {"raw_name": "张三", "aliases": ["李志强", "王五"]}
        result = resolve_source_row(source_row=sr, pack_id="default")
        al = next(f for f in result["fields"] if f["raw"] == "aliases")
        self.assertIn("李志强", al["value"])

    def test_none_value_empty(self):
        """None 值 → 空串。"""
        sr = {"raw_name": "张三", "relation": None}
        result = resolve_source_row(source_row=sr, pack_id="default")
        rel = next(f for f in result["fields"] if f["raw"] == "relation")
        self.assertEqual(rel["value"], "")


if __name__ == "__main__":
    unittest.main()
