"""REQ-D-004 清洗规则注册表测试（实施方案 AD-2：唯一 op 注册表 core/clean_ops.py）。

- AC-1 新规则注册后 CLEAN_RULE_NAMES 自动包含（动态集合，无需重导入）；
- AC-2 重复注册同名规则硬失败；
- AC-3 loader 校验未知清洗规则仍硬失败（fail-closed 不放宽）；
- AC-4 现有 strip / exclude_org_tokens 行为不变（回归锁）；
- AC-5 draft_assembler 的 clean_available 自动列出全部规则。
"""
import tempfile
import unittest
from pathlib import Path

import core.ontology_loader as ol
from core import clean_ops
from core.ontology import (CLEAN_RULE_NAMES, clean_strip,
                           clean_exclude_org_tokens)
from tests.test_ontology import _write_v2_pack

_PROBE = "probe_rule_reqd004"

_OBJ = {
    "name": "track", "title": "轨迹", "pk": "track_id", "kind": "event",
    "name_property": "person_raw",
    "properties": {"person_raw": "string", "location": "string", "date": "date"},
}
_BIND = {"object": "track",
         "source": {"table": "轨迹出行",
                    "columns": {"person_raw": "主体", "location": "地点",
                                "date": "日期"}}}


class _PackCtx:
    def __init__(self, objects, bindings):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name) / "p"
        self.d.mkdir()
        _write_v2_pack(self.d, objects=objects, object_bindings=bindings)
        self._orig = ol.PACK_ROOT

    def __enter__(self):
        ol.PACK_ROOT = Path(self._td.name)
        return self

    def __exit__(self, *exc):
        ol.PACK_ROOT = self._orig
        self._td.cleanup()


class TestOpRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean_ops.register_op(_PROBE, impl="py", fn=lambda v: v,
                              description="测试探针规则（REQ-D-004 AC-1）")

    def test_register_new_rule_auto_in_clean_rule_names(self):
        """AC-1：新规则注册后 CLEAN_RULE_NAMES 自动包含（含既有 strip）。"""
        self.assertIn(_PROBE, CLEAN_RULE_NAMES)
        self.assertIn("strip", CLEAN_RULE_NAMES)
        self.assertIn("exclude_org_tokens", CLEAN_RULE_NAMES)

    def test_duplicate_register_hard_fail(self):
        """AC-2：重复注册同名规则硬失败。"""
        with self.assertRaises(ValueError) as cm:
            clean_ops.register_op("strip", impl="py", fn=str)
        self.assertIn("重复注册", str(cm.exception))

    def test_register_validates_impl_fields(self):
        """注册时结构校验：py 必须带 fn、sql 必须带模板、layer 合法。"""
        with self.assertRaises(ValueError):
            clean_ops.register_op("bad_py", impl="py", fn=None)
        with self.assertRaises(ValueError):
            clean_ops.register_op("bad_sql", impl="sql", sql_template=None)
        with self.assertRaises(ValueError):
            clean_ops.register_op("bad_layer", impl="py", fn=str, layer="nope")

    def test_loader_unknown_clean_rule_hard_fail(self):
        """AC-3：binding 引用未注册清洗规则 → 装载硬失败（fail-closed）。"""
        bad = dict(_BIND, clean=["no_such_rule_xyz"])
        with _PackCtx([_OBJ], [bad]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            msg = str(cm.exception)
            self.assertIn("未在 op 注册表注册", msg)
            self.assertIn("no_such_rule_xyz", msg)

    def test_strip_and_exclude_org_tokens_behavior(self):
        """AC-4：现有两规则行为回归锁。"""
        self.assertEqual(clean_strip("  张三  "), "张三")
        self.assertEqual(clean_strip(None), "")
        self.assertTrue(clean_exclude_org_tokens("A建材", set()))
        self.assertTrue(clean_exclude_org_tokens("某公司", set()))
        self.assertFalse(clean_exclude_org_tokens("张三", set()))

    def test_draft_assembler_clean_available_auto_expand(self):
        """AC-5：draft_assembler 引用同一动态集合，新规则自动进入 clean_available。"""
        import core.draft_assembler as da
        self.assertIs(da.CLEAN_RULE_NAMES, CLEAN_RULE_NAMES)
        self.assertIn(_PROBE, sorted(da.CLEAN_RULE_NAMES))


if __name__ == "__main__":
    unittest.main()
