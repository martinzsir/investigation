"""REQ-D-021 数据元驱动落点推荐测试。

列名别名 + 值模式 → 推荐数据元 + clean/transform 规则。红线：
  - 只产 draft 提案，永不自动写 objects.json / 不自动生效；
  - 低置信度标 needs_confirmation（需人工确认）；
  - 混装复合列不给单一数据元，只给上游拆分提示（split_hint）。
"""
import unittest

from core.de_recommend import recommend_for_column
from core.ontology_loader import load_data_elements
from core.ontology_profile import build_table_profile

_ELEMENTS = load_data_elements("default")


def _rec(col, vals):
    return recommend_for_column(col, vals, _ELEMENTS)


def _find(rec, de):
    for r in rec["recommendations"]:
        if r["data_element"] == de:
            return r
    return None


class TestDeRecommend(unittest.TestCase):
    def test_idcard_format_high_confidence_sensitive(self):
        """身份证 18 位 format 命中 → DE_IDCARD，high，敏感列，无需人工确认。"""
        rec = _rec("身份证号", ["110101199003078517", "31011019850612432X"])
        self.assertIsNone(rec["split_hint"])
        r = _find(rec, "DE_IDCARD")
        self.assertIsNotNone(r)
        self.assertEqual(r["match_by"], "format")
        self.assertGreaterEqual(r["match_rate"], 0.9)
        self.assertEqual(r["confidence"], "high")
        self.assertFalse(r["needs_confirmation"])
        self.assertTrue(r["sensitive"])

    def test_gender_enum_high_confidence_and_profile_integration(self):
        """性别列值全落 enum → DE_GENDER high；推荐挂到 ColumnProfile（draft 字段）。"""
        rec = _rec("性别", ["男", "女", "未知"])
        r = _find(rec, "DE_GENDER")
        self.assertIsNotNone(r)
        self.assertEqual(r["match_by"], "enum")
        self.assertEqual(r["confidence"], "high")
        self.assertFalse(r["needs_confirmation"])
        # 集成 build_table_profile：推荐落入 de_recommendations，不产生 candidates 写动作
        prof = build_table_profile("t", ["性别"], [["男"], ["女"], ["未知"]],
                                   gateway=None, pack="default")
        cp = prof.columns[0]
        self.assertTrue(cp.de_recommendations)
        self.assertEqual(cp.de_recommendations[0]["data_element"], "DE_GENDER")
        self.assertEqual(prof.candidates, [])  # 只读画像，无库不产生关联

    def test_enum_partial_match_needs_confirmation(self):
        """枚举命中率 0.7~0.9 → medium 且需人工确认（低置信不自动生效）。"""
        rec = _rec("性别", ["男", "女", "未知", "中性"])  # 3/4 命中
        r = _find(rec, "DE_GENDER")
        self.assertIsNotNone(r)
        self.assertEqual(r["confidence"], "medium")
        self.assertTrue(r["needs_confirmation"])
        self.assertTrue(rec["needs_confirmation"])

    def test_phone_rule_hint_without_data_element(self):
        """手机号列无对应数据元 → 仍推荐 transform digits_only/strip_cc，标需确认。"""
        rec = _rec("手机号", ["13800138000", "13912345678"])
        self.assertEqual(len(rec["recommendations"]), 1)
        r = rec["recommendations"][0]
        self.assertIsNone(r["data_element"])
        self.assertEqual(r["match_by"], "value_pattern")
        self.assertIn("digits_only", r["transform"])
        self.assertIn("strip_cc", r["transform"])
        self.assertTrue(r["needs_confirmation"])

    def test_masked_account_reject_hint(self):
        """遮蔽卡号列 → clean reject_if:contains_mask + digits_only（双通道拒行建议）。"""
        rec = _rec("卡号", ["6222021234567890", "6222********7890"])
        self.assertIsNone(rec["split_hint"])  # 单落点 account，非混装
        r = rec["recommendations"][0]
        self.assertEqual(r["match_by"], "value_pattern")
        self.assertIn("reject_if:contains_mask", r["clean"])
        self.assertIn("digits_only", r["clean"])

    def test_composite_column_split_hint_no_single_de(self):
        """账号+人名混装 → 不推单一数据元，给上游拆分提示，需人工确认。"""
        rec = _rec("对方", ["6222021234567890", "张三", "李四"])
        self.assertIsNotNone(rec["split_hint"])
        self.assertIn("拆分", rec["split_hint"])
        self.assertEqual(rec["recommendations"], [])
        self.assertTrue(rec["needs_confirmation"])


if __name__ == "__main__":
    unittest.main()
