"""REQ-D-012 1:1 约束守护测试。

映射层严格 1:1（一源列 → 一属性）是可追溯性的底线：
  - 同一 binding 内同一源列映射多个别名 → 装载硬失败，错误信息报冲突双方与出路；
  - transform 声明 split 类 op → 硬失败（列拆分不得进入声明层，出路 source_sql 上游拆分）；
  - 豁免边界：source_sql 绑定 = 派生属性（UNION 不违规）；跨对象共用源列合法。
"""
import tempfile
import unittest
from pathlib import Path

import core.ontology_loader as ol
from tests.test_ontology import _write_v2_pack

_OBJ = {
    "name": "track", "title": "轨迹", "pk": "track_id", "kind": "event",
    "name_property": "person_raw",
    "properties": {"person_raw": "string", "location": "string", "date": "date"},
}


def _bind(columns=None, **extra):
    b = {"object": "track",
         "source": {"table": "轨迹出行",
                    "columns": columns or {"person_raw": "主体", "location": "地点",
                                           "date": "日期"}}}
    b.update(extra)
    return b


class _PackCtx:
    """临时 PACK_ROOT 上下文（与 test_ontology/test_missing_column 同口径）。"""

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


class TestOneToOneGuard(unittest.TestCase):
    def test_same_column_two_aliases_hard_fail(self):
        """AC-1：同一 binding 内两个属性映射同一源列 → 装载硬失败，报冲突双方。"""
        bad = _bind(columns={"person_raw": "主体", "location": "主体", "date": "日期"})
        with _PackCtx([_OBJ], [bad]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            msg = str(cm.exception)
            self.assertIn("主体", msg)          # 冲突源列
            self.assertIn("person_raw", msg)    # 冲突双方
            self.assertIn("location", msg)

    def test_error_message_actionable_exit(self):
        """AC-4：错误信息给出可行出路（提示改用 source_sql 在上游处理）。"""
        bad = _bind(columns={"person_raw": "主体", "location": "主体", "date": "日期"})
        with _PackCtx([_OBJ], [bad]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            msg = str(cm.exception)
            self.assertIn("REQ-D-012", msg)
            self.assertIn("source_sql", msg)

    def test_split_op_rejected(self):
        """AC-2：transform 声明 split 类 op → 硬失败，出路提示 source_sql 上游拆分。"""
        bad = _bind(transform={"person_raw": ["split", "strip"]})
        with _PackCtx([_OBJ], [bad]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            msg = str(cm.exception)
            self.assertIn("split", msg)
            self.assertIn("REQ-D-012", msg)
            self.assertIn("source_sql", msg)

    def test_split_prefixed_op_rejected(self):
        """AC-2：split_part 等前缀变体同样拒绝（split 类 op 家族）。"""
        bad = _bind(transform={"person_raw": "split_part:|"})
        with _PackCtx([_OBJ], [bad]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("split_part", str(cm.exception))

    def test_transform_without_split_not_blocked(self):
        """transform 含非 split op 时不被 1:1 守护拦截（op 合法性由 transform 层校验）。"""
        ok = _bind(transform={"person_raw": ["strip_thousands"]})
        with _PackCtx([_OBJ], [ok]):
            pack = ol.load_pack("p")
            self.assertIn("track", pack.object_bindings)

    def test_cross_object_shared_column_ok(self):
        """豁免：跨对象共用同一源列合法（如 通话记录.主体 同时喂 person/call）。"""
        pa = {"name": "pa", "title": "PA", "pk": "pa_id", "kind": "entity",
              "name_property": "raw_name",
              "properties": {"raw_name": "string", "phone": "string"}}
        ev = {"name": "ev", "title": "EV", "pk": "ev_id", "kind": "event",
              "name_property": "raw_name",
              "properties": {"raw_name": "string", "note": "string"}}
        binds = [
            {"object": "pa", "source": {"table": "共享表",
                                        "columns": {"raw_name": "主体", "phone": "电话"}}},
            {"object": "ev", "source": {"table": "共享表",
                                        "columns": {"raw_name": "主体", "note": "备注"}}},
        ]
        with _PackCtx([pa, ev], binds):
            pack = ol.load_pack("p")
            self.assertIn("pa", pack.object_bindings)
            self.assertIn("ev", pack.object_bindings)

    def test_source_sql_union_exempt(self):
        """豁免：source_sql 绑定 = 派生属性，同一列 UNION 多路引用不违规（person/account 现状）。"""
        bind = {"object": "track",
                "source_table": "轨迹出行",
                "source_sql": "SELECT 主体 AS person_raw FROM 轨迹出行 "
                              "UNION SELECT 主体 FROM 轨迹出行"}
        with _PackCtx([_OBJ], [bind]):
            pack = ol.load_pack("p")
            self.assertIn("track", pack.object_bindings)

    def test_existing_packs_all_pass(self):
        """AC-5：现有案件包声明文件全部通过 1:1 校验（无既存违规，随包扩充自动覆盖）。"""
        packs = [d.name for d in ol.PACK_ROOT.iterdir()
                 if d.is_dir() and (d / "objects.json").exists()]
        self.assertTrue(packs, "ontology/ 下应至少存在一个案件包")
        for name in packs:
            pack = ol.load_pack(name)
            self.assertTrue(pack.object_bindings)


if __name__ == "__main__":
    unittest.main()
