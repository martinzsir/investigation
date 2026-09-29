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


# ----------------------------------------------------------------------
# 物品词汇装载（enum_meta 派生）：声明层硬失败守卫
# ----------------------------------------------------------------------
_DE_KIND = {"name": "物品标识符种类", "type": "enum",
            "enum": ["plate", "serial"],
            "enum_meta": {
                "plate": {"label": "车牌号", "aliases": ["车牌"]},
                "serial": {"label": "出厂序列号", "aliases": ["序列号"]}}}
_DE_TYPE = {"name": "物品类型", "type": "enum",
            "enum": ["vehicle", "gadget"],
            "enum_meta": {
                "vehicle": {"label": "车辆", "identifier_kind": "plate",
                            "source_labels": ["车辆"]},
                "gadget": {}}}   # 无 meta：label 回落代码本身


class _VocabCtx:
    """临时 ontology 根：仅落 p/data_elements.json（词汇装载不读其余声明）。"""

    def __init__(self, elements: dict):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        d = self.root / "p"
        d.mkdir()
        (d / "data_elements.json").write_text(
            json.dumps({"schema_version": 2, "elements": elements},
                       ensure_ascii=False), encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._td.cleanup()

    def load(self) -> dict:
        return ol.load_item_vocabulary("p", base_dir=self.root)


def _de_pair() -> dict:
    """合法 DE_ITEM_IDENTIFIER_KIND + DE_ITEM_TYPE 的深拷贝，可按需打补丁。"""
    import copy
    return {"DE_ITEM_IDENTIFIER_KIND": copy.deepcopy(_DE_KIND),
            "DE_ITEM_TYPE": copy.deepcopy(_DE_TYPE)}


class TestItemVocabulary(unittest.TestCase):
    """enum_meta 形状校验 + 物品词汇派生的跨元素硬失败（fail-closed）。"""

    def test_valid_vocabulary_derives(self):
        with _VocabCtx(_de_pair()) as ctx:
            vocab = ctx.load()
        self.assertEqual(("plate", "serial"), vocab["identifier_kinds"])
        self.assertEqual(("vehicle", "gadget"), vocab["item_types"])
        # label/别名/代码本身全量进索引
        self.assertEqual("plate", vocab["identifier_kind_aliases"]["车牌号"])
        self.assertEqual("plate", vocab["identifier_kind_aliases"]["车牌"])
        self.assertEqual("plate", vocab["identifier_kind_aliases"]["plate"])
        self.assertEqual("serial", vocab["identifier_kind_aliases"]["序列号"])
        # 无 enum_meta 的枚举值 label 回落代码本身
        self.assertEqual("gadget", vocab["item_type_labels"]["gadget"])
        self.assertEqual({"车辆": ("vehicle", "plate")},
                         vocab["item_type_source_map"])
        self.assertEqual(frozenset(), vocab["sensitive_identifier_kinds"])

    def test_enum_meta_key_outside_enum_hard_fail(self):
        """enum_meta 键必须 ⊆ enum：未知名硬失败（与全仓 loader 同口径）。"""
        bad = _de_pair()
        bad["DE_ITEM_TYPE"]["enum_meta"]["zzz"] = {"label": "不存在"}
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("不在 enum 值域内", str(cm.exception))

    def test_enum_meta_unknown_field_hard_fail(self):
        """meta 键白名单 fail-closed：'alias' 少写一个 s 不得静默生效。"""
        bad = _de_pair()
        bad["DE_ITEM_IDENTIFIER_KIND"]["enum_meta"]["plate"] = {"alias": ["车牌"]}
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("未知键", str(cm.exception))

    def test_enum_meta_without_enum_hard_fail(self):
        bad = _de_pair()
        del bad["DE_ITEM_TYPE"]["enum"]
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("没有 enum", str(cm.exception))

    def test_identifier_kind_dangling_ref_hard_fail(self):
        """identifier_kind 必须 ∈ DE_ITEM_IDENTIFIER_KIND 值域：悬空硬失败。"""
        bad = _de_pair()
        bad["DE_ITEM_TYPE"]["enum_meta"]["vehicle"]["identifier_kind"] = "not_a_kind"
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("悬空", str(cm.exception))

    def test_source_labels_require_identifier_kind(self):
        """source_labels 必配 identifier_kind：源映射是 (类型, 凭证) 二元组。"""
        bad = _de_pair()
        del bad["DE_ITEM_TYPE"]["enum_meta"]["vehicle"]["identifier_kind"]
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("source_labels", str(cm.exception))

    def test_alias_collision_hard_fail(self):
        """两个种类共用同一别名 → 装载期硬失败，宁崩不歧义。"""
        bad = _de_pair()
        bad["DE_ITEM_IDENTIFIER_KIND"]["enum_meta"]["plate"]["aliases"] = ["序列号"]
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("抢名", str(cm.exception))

    def test_alias_collides_with_code_hard_fail(self):
        """别名撞上**另一种类的代码本身**同样硬失败（比旧元组检查更严）。"""
        bad = _de_pair()
        bad["DE_ITEM_IDENTIFIER_KIND"]["enum_meta"]["plate"]["aliases"] = ["Serial"]
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("抢名", str(cm.exception))

    def test_source_label_collision_hard_fail(self):
        """同一中文源词指向两个物品类型 → 硬失败（dict 覆盖会静默映射错）。"""
        bad = _de_pair()
        bad["DE_ITEM_TYPE"]["enum_meta"]["gadget"] = {
            "identifier_kind": "serial", "source_labels": ["车辆"]}
        with _VocabCtx(bad) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("抢名", str(cm.exception))

    def test_missing_item_des_hard_fail(self):
        """两个锚定数据元缺失即声明破损：fail-closed，不回落空词汇。"""
        with _VocabCtx({"DE_X": {"name": "X", "type": "string"}}) as ctx:
            with self.assertRaises(ValueError) as cm:
                ctx.load()
        self.assertIn("DE_ITEM_IDENTIFIER_KIND", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
