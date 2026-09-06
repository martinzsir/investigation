"""REQ-D-001 数据元标准注册测试（第 14 声明文件 data_elements.json）。

- AC-1 必填字段缺失硬失败；AC-2 未知 checksum 算法硬失败（fail-closed）；
- AC-3 schema_version 与其余声明文件一致；AC-5 元素 ID 重复硬失败；
- clean_rule 必须已在 op 注册表（与 binding→clean 同口径交叉校验）；
- 缺失 data_elements.json 的案件包向后兼容（回落空集）。
"""
import json
import tempfile
import unittest
from pathlib import Path

import core.ontology_loader as ol
from core.data_elements import CHECKSUM_ALGOS, checksum_idcard_mod11
from tests.test_ontology import _write_v2_pack

_OBJ = {
    "name": "track", "title": "轨迹", "pk": "track_id", "kind": "event",
    "name_property": "person_raw",
    "properties": {"person_raw": "string", "location": "string", "date": "date"},
}
_BIND = {"object": "track",
         "source": {"table": "轨迹出行",
                    "columns": {"person_raw": "主体", "location": "地点",
                                "date": "日期"}}}

_VALID_DE = {"schema_version": 2, "elements": {
    "DE_IDCARD": {"name": "公民身份号码", "type": "string", "length": 18,
                  "format": "^\\d{17}[\\dXx]$", "checksum": "idcard_mod11"}}}


class _PackCtx:
    def __init__(self, objects, bindings, de_raw=None):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name) / "p"
        self.d.mkdir()
        _write_v2_pack(self.d, objects=objects, object_bindings=bindings)
        if de_raw is not None:
            (self.d / "data_elements.json").write_text(
                de_raw if isinstance(de_raw, str)
                else json.dumps(de_raw, ensure_ascii=False), encoding="utf-8")
        self._orig = ol.PACK_ROOT

    def __enter__(self):
        ol.PACK_ROOT = Path(self._td.name)
        return self

    def __exit__(self, *exc):
        ol.PACK_ROOT = self._orig
        self._td.cleanup()


class TestDataElements(unittest.TestCase):
    def test_missing_required_field_hard_fail(self):
        """AC-1：必填字段（name/type）缺失 → 装载硬失败。"""
        bad = {"schema_version": 2, "elements": {
            "DE_X": {"type": "string"}}}
        with _PackCtx([_OBJ], [_BIND], de_raw=bad):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("缺必填字段", str(cm.exception))

    def test_unknown_checksum_hard_fail(self):
        """AC-2：未知 checksum 算法 → 硬失败（fail-closed 不放宽）。"""
        bad = {"schema_version": 2, "elements": {
            "DE_X": {"name": "X", "type": "string", "checksum": "crc999"}}}
        with _PackCtx([_OBJ], [_BIND], de_raw=bad):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            msg = str(cm.exception)
            self.assertIn("未知 checksum", msg)
            self.assertIn("crc999", msg)

    def test_schema_version_mismatch_hard_fail(self):
        """AC-3：schema_version 与其余声明文件不一致 → 硬失败。"""
        bad = dict(_VALID_DE, schema_version=1)
        with _PackCtx([_OBJ], [_BIND], de_raw=bad):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("schema_version", str(cm.exception))

    def test_valid_elements_load_and_checksum_impl(self):
        """AC-4（前半）：合法数据元装载成功、规格可查询；checksum 实现可用。"""
        with _PackCtx([_OBJ], [_BIND], de_raw=_VALID_DE):
            pack = ol.load_pack("p")
            elements = ol.load_data_elements("p")
        self.assertIn("track", pack.object_bindings)
        self.assertIn("DE_IDCARD", elements)
        self.assertEqual(elements["DE_IDCARD"]["type"], "string")
        # idcard_mod11：合法号通过、校验位错/位数不足拒绝、小写 x 兼容
        self.assertTrue(checksum_idcard_mod11("11010519491231002X"))
        self.assertFalse(checksum_idcard_mod11("110105194912310021"))
        self.assertFalse(checksum_idcard_mod11("1101051949123100"))
        self.assertTrue(checksum_idcard_mod11("11010519491231002x"))
        self.assertIn("idcard_mod11", CHECKSUM_ALGOS)

    def test_duplicate_element_id_hard_fail(self):
        """AC-5：元素 ID 重复注册 → 硬失败（JSON 键级检测）。"""
        raw = ('{"schema_version": 2, "elements": {'
               '"DE_X": {"name": "A", "type": "string"},'
               '"DE_X": {"name": "B", "type": "string"}}}')
        with _PackCtx([_OBJ], [_BIND], de_raw=raw):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            msg = str(cm.exception)
            self.assertIn("重复注册", msg)
            self.assertIn("DE_X", msg)

    def test_clean_rule_must_be_registered(self):
        """clean_rule 交叉校验：未注册 op 硬失败（与 binding→clean 同口径）。"""
        bad = {"schema_version": 2, "elements": {
            "DE_X": {"name": "X", "type": "string", "clean_rule": "no_such_op"}}}
        with _PackCtx([_OBJ], [_BIND], de_raw=bad):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("未在 op 注册表", str(cm.exception))

    def test_pack_without_data_elements_ok(self):
        """向后兼容：缺失 data_elements.json 的案件包装载正常（回落空集）。"""
        with _PackCtx([_OBJ], [_BIND]):
            pack = ol.load_pack("p")
            self.assertEqual(ol.load_data_elements("p"), {})
        self.assertIn("track", pack.object_bindings)


if __name__ == "__main__":
    unittest.main()
