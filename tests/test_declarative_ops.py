"""REQ-D-006/011 声明式原子 op 与电诈脏格式归一测试。

脏格式样例（17 类中的代表项）经声明式 op 在 transform（SQL 投影，TRY_CAST 前）
与 clean（Python 行后处理）两层归一：
  - 手机号空格/连字符/国家码（+86/0086）→ digits_only + strip_cc；
  - 银行卡号连字符 → digits_only；星号遮蔽残片 → reject_if:contains_mask 整行剔除；
  - 姓名括号绰号 → strip_paren；
  - 中文日期/未补零日期 → cn_date_norm + pad_date（TRY_CAST DATE 成功）；
  - 大小写 → to_upper/to_lower。
红线：带参 op 参数仅白名单取值（REQ-D-011 防注入），未注册/非白名单硬失败。
"""
import unittest

import duckdb

from core.ontology import build_ontology
from core import clean_ops
from tests.test_one2one import _PackCtx

_OBJ = {"name": "person", "title": "人员", "pk": "person_id",
        "kind": "entity", "name_property": "name",
        "properties": {"name": "string", "phone": "string",
                       "card": "string", "dob": "date"}}

_BIND = {"object": "person",
         "source": {"table": "PERS",
                    "columns": {"name": "姓名", "phone": "手机",
                                "card": "卡号", "dob": "生日"}},
         # transform：SQL 投影内归一（digits_only/strip_cc/strip_paren/日期）
         "transform": {"phone": ["digits_only", "strip_cc"],
                       "name": ["strip_paren"],
                       "dob": ["cn_date_norm", "pad_date"]},
         # clean：Python 行后处理——星号遮蔽残片先拒，连字符卡号再剥数字
         "clean": {"card": ["reject_if:contains_mask", "digits_only"]}}

_ROWS = [
    ("李强（绰号小强）", "+86 138-0013-8000", "6222-0212-3456-7890", "2024年3月5日"),
    ("王梅", "138 0013 8000", "6222********7890", "2024-3-5"),      # 星号残片 → 整行剔除
    ("张三", "0086 139 1234 5678", "6228-4800-1234-5678", "2024-12-5"),
]


class TestDeclarativeOps(unittest.TestCase):
    def setUp(self):
        with _PackCtx([_OBJ], [_BIND]) as pc:
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PERS ("姓名" VARCHAR, "手机" VARCHAR, '
                         '"卡号" VARCHAR, "生日" VARCHAR)')
            conn.executemany("INSERT INTO PERS VALUES (?,?,?,?)", _ROWS)
            self.stats = build_ontology(conn, pack="p")
            self.rows = {r[0]: r for r in conn.execute(
                "SELECT name, phone, card, dob FROM obj_person").fetchall()}

    def test_phone_spaces_and_country_code(self):
        """手机号空格/连字符剥离 + 国家码（+86/0086）剥除。"""
        self.assertEqual(self.rows["李强"][1], "13800138000")
        self.assertEqual(self.rows["张三"][1], "13912345678")

    def test_bank_card_strip_dashes(self):
        """银行卡号连字符剥离为纯数字。"""
        self.assertEqual(self.rows["李强"][2], "6222021234567890")
        self.assertEqual(self.rows["张三"][2], "6228480012345678")

    def test_strip_paren_alias(self):
        """姓名括号绰号剥离：李强（绰号小强）→ 李强。"""
        self.assertIn("李强", self.rows)
        self.assertNotIn("李强（绰号小强）", self.rows)

    def test_date_norm_and_pad_to_date(self):
        """中文日期 + 未补零日期归一后 TRY_CAST DATE 成功。"""
        self.assertEqual(self.rows["李强"][3].isoformat(), "2024-03-05")
        self.assertEqual(self.rows["张三"][3].isoformat(), "2024-12-05")

    def test_masked_card_row_rejected(self):
        """星号遮蔽卡号残片整行剔除（王梅不入语义层）。"""
        self.assertEqual(len(self.rows), 2)
        self.assertNotIn("王梅", self.rows)

    def test_reject_recorded_in_clean_stats(self):
        """拒绝动作落 clean_stats（rule 带完整带参 op 名，可下钻）。"""
        rules = {e["rule"] for e in self.stats.get("clean_stats", [])}
        self.assertIn("reject_if:contains_mask", rules)
        dropped = sum(e["dropped_rows"] for e in self.stats["clean_stats"]
                      if e["rule"] == "reject_if:contains_mask")
        self.assertEqual(dropped, 1)

    def test_upper_lower(self):
        """大小写归一：py fn 与 SQL 模板两层一致。"""
        self.assertEqual(clean_ops.OPS["to_upper"].fn("abc88"), "ABC88")
        self.assertEqual(clean_ops.OPS["to_lower"].fn("ABC88"), "abc88")
        self.assertIn("UPPER(", clean_ops.compile_sql_expr(["to_upper"], '"x"'))
        self.assertIn("LOWER(", clean_ops.compile_sql_expr(["to_lower"], '"x"'))

    def test_unknown_and_non_whitelist_param_hard_fail(self):
        """未注册 op / 非白名单参数 / 无白名单 op 带参 → 硬失败（防注入）。"""
        with self.assertRaises(ValueError):
            clean_ops.compile_sql_expr(["no_such_op"], '"x"')
        with self.assertRaises(ValueError):      # digits_only 不接受参数
            clean_ops.validate_op("digits_only:evil", "transform")
        with self.assertRaises(ValueError):      # reject_if 参数非白名单
            clean_ops.validate_op("reject_if:drop_all", "clean")
        # 白名单内参数通过
        self.assertEqual(clean_ops.validate_op("reject_if:contains_mask", "clean"),
                         ("reject_if", "contains_mask"))


if __name__ == "__main__":
    unittest.main()
