"""REQ-D-002 属性引用数据元（AD-5 装载期展开继承）测试。

属性经 {"data_element": "DE_X"} 引用数据元后：
  - AC-1 类型自动继承（无需重复声明 type）；
  - AC-2 本地 type 与数据元冲突 → 硬失败（不静默覆盖标准）；
  - AC-3 数据元 clean_rule 自动挂接（binding 无需重复声明清洗）；
  - AC-4 sensitive 数据元属性必须已在 policies.json 声明遮蔽，否则硬失败（AD-5）；
  - AC-5 未引用数据元的属性行为与现状完全一致；
  - AC-6 引用不存在的数据元 ID → 硬失败。
"""
import json
import unittest

import duckdb

import core.ontology_loader as ol
from core.ontology import build_ontology
from tests.test_one2one import _PackCtx

_DE_PHONE = {"name": "电话", "type": "string", "clean_rule": "strip"}
_DE_ID = {"name": "证件号", "type": "string", "sensitive": True, "mask": "partial"}


def _write_de(pc, elements):
    (pc.d / "data_elements.json").write_text(
        json.dumps({"schema_version": 2, "elements": elements},
                   ensure_ascii=False), encoding="utf-8")


def _write_policies(pc, masked):
    """masked=((object, property), ...) 写入 property_policies 遮蔽声明。"""
    props = [{"object": o, "property": p, "default": "deny",
              "allow_roles": ["主办", "human"], "mask": "partial"}
             for o, p in masked]
    doc = {"schema_version": 2,
           "object_policies": [{"object": "person",
                                "roles": ["见习", "正兵", "偏将", "主办", "human"],
                                "min_clearance": 0}],
           "property_policies": props}
    (pc.d / "policies.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _obj(phone_decl, id_decl=None):
    props = {"name": "string", "phone": phone_decl}
    if id_decl is not None:
        props["id_no"] = id_decl
    return {"name": "person", "title": "人员", "pk": "person_id",
            "kind": "entity", "name_property": "name", "properties": props}


def _bind(cols):
    return {"object": "person",
            "source": {"table": "PERS", "columns": cols}}


class TestDataElementReference(unittest.TestCase):
    def test_type_inherited(self):
        """AC-1：只写 data_element 不写 type → 类型从数据元继承为 string。"""
        obj = _obj({"data_element": "DE_PHONE"})
        with _PackCtx([obj], [_bind({"name": "名称", "phone": "电话"})]) as pc:
            _write_de(pc, {"DE_PHONE": _DE_PHONE})
            pack = ol.load_pack("p")
            self.assertEqual(pack.objects[0].properties["phone"], "string")
            self.assertEqual(pack.objects[0].prop_data_elements["phone"], "DE_PHONE")

    def test_type_conflict_hard_fail(self):
        """AC-2：本地 type 与数据元 type 冲突 → 硬失败（不静默覆盖）。"""
        obj = _obj({"type": "integer", "data_element": "DE_PHONE"})
        with _PackCtx([obj], [_bind({"name": "名称", "phone": "电话"})]) as pc:
            _write_de(pc, {"DE_PHONE": _DE_PHONE})
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("冲突", str(cm.exception))
            self.assertIn("DE_PHONE", str(cm.exception))

    def test_clean_rule_auto_applied(self):
        """AC-3：数据元 clean_rule=strip 自动挂接——binding 不声明 clean，
        源值带空格，构建后 phone 被 strip（无需 binding 重复）。"""
        obj = _obj({"data_element": "DE_PHONE"})
        with _PackCtx([obj], [_bind({"name": "名称", "phone": "电话"})]) as pc:
            _write_de(pc, {"DE_PHONE": _DE_PHONE})
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "电话" VARCHAR)')
            conn.execute("INSERT INTO PERS VALUES ('张三', ' 13800138000 ')")
            build_ontology(conn, pack="p")
            val = conn.execute("SELECT phone FROM obj_person").fetchone()[0]
            self.assertEqual(val, "13800138000")   # strip 自动生效
            pack = ol.load_pack("p")
            self.assertEqual(pack.objects[0].prop_de_clean.get("phone"), "strip")

    def test_sensitive_requires_mask_declaration(self):
        """AC-4/AD-5：引用 sensitive 数据元的属性必须已声明遮蔽。
        无 policies → 硬失败；补 property_policies 遮蔽 → 通过。"""
        obj = _obj("string", {"data_element": "DE_ID"})
        bind = _bind({"name": "名称", "id_no": "证件"})
        # 路径一：未声明遮蔽 → 硬失败
        with _PackCtx([obj], [bind]) as pc:
            _write_de(pc, {"DE_ID": _DE_ID})
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("policies.json", str(cm.exception))
            self.assertIn("id_no", str(cm.exception))
        # 路径二：补遮蔽声明 → 装载通过
        with _PackCtx([obj], [bind]) as pc:
            _write_de(pc, {"DE_ID": _DE_ID})
            _write_policies(pc, [("person", "id_no")])
            pack = ol.load_pack("p")
            self.assertEqual(pack.objects[0].prop_data_elements["id_no"], "DE_ID")

    def test_plain_property_unchanged(self):
        """AC-5：未引用数据元的普通属性行为不变（值原样入库、无清洗挂接）。"""
        obj = _obj("string")
        with _PackCtx([obj], [_bind({"name": "名称", "phone": "电话"})]) as pc:
            _write_de(pc, {"DE_PHONE": _DE_PHONE})
            conn = duckdb.connect(":memory:")
            conn.execute('CREATE TABLE PERS ("名称" VARCHAR, "电话" VARCHAR)')
            conn.execute("INSERT INTO PERS VALUES ('张三', ' 原样保留 ')")
            build_ontology(conn, pack="p")
            val = conn.execute("SELECT phone FROM obj_person").fetchone()[0]
            self.assertEqual(val, " 原样保留 ")   # 未引用 → 不 strip
            pack = ol.load_pack("p")
            self.assertEqual(pack.objects[0].prop_de_clean, {})

    def test_unknown_element_hard_fail(self):
        """AC-6：引用不存在的数据元 ID → 硬失败。"""
        obj = _obj({"data_element": "DE_GHOST"})
        with _PackCtx([obj], [_bind({"name": "名称", "phone": "电话"})]) as pc:
            _write_de(pc, {"DE_PHONE": _DE_PHONE})
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("DE_GHOST", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
