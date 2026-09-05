"""
tests/test_value_type.py
REQ-P M3 / P1 值类型识别 测试（core/value_type.py 纯函数模块）。

覆盖（实施计划 §三 P1 + ONTOLOGY_PROFILER 缺陷 1/2）：
  缺陷 1：金额串正则收紧——6222000111110001 → 账号（不再被 ^[\\d,]+$ 吞成金额）
  缺陷 2：否定式特异性降序——13800138000 → 手机号（不被账号超集吞掉）
  classify 各类型 + 肯定式 + 空值
  analyze_column：分布 / 混装判定 / 落点建议两方向 / 肯定式需确认
  零依赖：模块源码不 import duckdb/core 其他模块
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.value_type import (                                   # noqa: E402
    ORDER, classify, analyze_column,
)


class ClassifyTests(unittest.TestCase):
    def test_ac01_手机号(self):
        """缺陷 2 反向验证：手机号不被账号超集吞掉。"""
        self.assertEqual(classify("13800138000"), "phone")

    def test_ac02_身份证(self):
        self.assertEqual(classify("11010119900307851X"), "id_card")
        self.assertEqual(classify("110101199003078515"), "id_card")

    def test_ac03_日期串(self):
        self.assertEqual(classify("2021-10-01"), "date_str")
        self.assertEqual(classify("2021/10/1"), "date_str")
        self.assertEqual(classify("2021年10月1日"), "date_str")

    def test_ac04_金额串修正(self):
        """缺陷 1：货币符号/千分位/单位至少其一 → 金额串。"""
        self.assertEqual(classify("¥1,200,000"), "amount")
        self.assertEqual(classify("￥1200000.00"), "amount")
        self.assertEqual(classify("1,200,000.00"), "amount")
        self.assertEqual(classify("15万元"), "amount")
        self.assertEqual(classify("3.5亿"), "amount")

    def test_ac05_纯数字账号不是金额(self):
        """缺陷 1 反向验证：无货币符号/千分位/小数/单位 → 不判金额串。"""
        self.assertEqual(classify("6222000111110001"), "account")

    def test_ac06_账号含遮蔽态(self):
        self.assertEqual(classify("6222****1234"), "account")
        self.assertEqual(classify("6222-0011-1234"), "account")

    def test_ac07_纯整数(self):
        self.assertEqual(classify("12345"), "number")
        self.assertEqual(classify("-42"), "number")

    def test_ac08_肯定式人名与机构名(self):
        """全不中 → 人名/机构名（肯定式，需确认）。"""
        self.assertEqual(classify("张卫国"), "person")
        self.assertEqual(classify("宏业建设有限公司"), "org")
        self.assertEqual(classify("杭州市财政局"), "org")

    def test_ac09_空值(self):
        self.assertEqual(classify(None), "empty")
        self.assertEqual(classify(""), "empty")
        self.assertEqual(classify("   "), "empty")

    def test_ac10_全角归一(self):
        """NFKC：全角数字/货币符号与半角同判。"""
        self.assertEqual(classify("１３８００１３８０００"), "phone")

    def test_ac11_order特异性降序(self):
        """判定序固化：特异性强的排前面（缺陷 2 的机制层保障）。"""
        self.assertEqual(
            ORDER, ("phone", "id_card", "date_str", "amount", "account", "number"))


class AnalyzeColumnTests(unittest.TestCase):
    def test_ac20_混装两方向落点(self):
        """账号+人名混写（沙盒 from_raw 实测形态）→ mixed，落点两方向都报。"""
        r = analyze_column(["6222000111110001", "张卫国", "6222000222220002", "李志强"])
        self.assertTrue(r["mixed"])
        self.assertEqual(r["landing_suggestions"], ["account", "person"])
        self.assertIn("account", r["negative_types"])

    def test_ac21_机构与人名混装(self):
        """org.raw_name 实测形态（机构名 67% 人名 33%）→ mixed。"""
        vals = ["宏业建设", "泰和建材", "A建材"] + ["张卫国"]
        r = analyze_column(vals)
        self.assertTrue(r["mixed"])
        self.assertEqual(r["landing_suggestions"], ["org", "person"])

    def test_ac22_纯净人名列(self):
        r = analyze_column(["张卫国", "李志强"])
        self.assertFalse(r["mixed"])
        self.assertEqual(r["landing_suggestions"], ["person"])
        self.assertTrue(r["needs_confirmation"])   # 肯定式需确认

    def test_ac23_纯净账号列(self):
        r = analyze_column(["6222000111110001", "6222000222220002"])
        self.assertFalse(r["mixed"])
        self.assertEqual(r["landing_suggestions"], ["account"])
        self.assertFalse(r["needs_confirmation"])  # 否定式可靠

    def test_ac24_非身份类型无落点(self):
        """金额/日期/纯数字不映射实体对象。"""
        r = analyze_column(["15万元", "2021-10-01", "42"])
        self.assertFalse(r["mixed"])
        self.assertEqual(r["landing_suggestions"], [])

    def test_ac25_分布计数与空值(self):
        r = analyze_column(["张卫国", "13800138000", None, ""])
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["type_dist"].get("empty"), 2)
        self.assertEqual(r["type_dist"].get("person"), 1)
        self.assertEqual(r["type_dist"].get("phone"), 1)

    def test_ac26_空列(self):
        r = analyze_column([])
        self.assertEqual(r["total"], 0)
        self.assertFalse(r["mixed"])
        self.assertEqual(r["landing_suggestions"], [])


class ZeroDependencyTests(unittest.TestCase):
    def test_ac30_零依赖AST扫描(self):
        """value_type 只依赖 stdlib（re/unicodedata），不 import duckdb/core。"""
        import ast
        src = (ROOT / "core" / "value_type.py").read_text(encoding="utf-8")
        mods: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        mods.discard("")  # relative import 无 module
        self.assertEqual(
            mods, {"__future__", "re", "unicodedata"},
            f"value_type 只允许 stdlib import，实际：{sorted(mods)}")


if __name__ == "__main__":
    unittest.main()
