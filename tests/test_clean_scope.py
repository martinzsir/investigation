"""REQ-D-005 属性级清洗作用域测试（批 D2）。

解除 _apply_clean 两条硬限制（只 person、只名称列）并升级双通道（AD-3）：
  - AC-1 非 person 对象（transaction 等）的属性可被清洗；
  - AC-2 非名称列（card_no/phone_raw）可被清洗；
  - AC-3 数组形式向后兼容（= {"_name": [...]}，滤行行为与现状一致）；
  - AC-4 属性与规则均按声明顺序执行；
  - AC-5 未声明属性不被改动；
  - AD-3 py op 三种返回契约（str 改值 / tuple 显式 / bool 滤行），
    剔除计数按 (object, property, rule) 进 stats["clean_stats"]，样本脱敏。
"""
import unittest

import duckdb

from core import clean_ops
from core.ontology import build_ontology
import core.ontology_loader as ol
from tests.test_one2one import _PackCtx

# ---- 测试专用 py op（唯一前缀避免与平台 op 冲突；fn 契约 fn(value, ctx)）----
clean_ops.register_op("d2c_append_a", impl="py", layer="clean",
                      fn=lambda v, ctx=None: f"{v}A", description="测试：追加固化后缀 A")
clean_ops.register_op("d2c_append_b", impl="py", layer="clean",
                      fn=lambda v, ctx=None: f"{v}B", description="测试：追加固化后缀 B")


def _drop_x(v, ctx=None):
    """bool 谓词契约：True=剔除（既有滤行口径）。"""
    return v == "X"


def _drop_cond_tuple(v, ctx=None):
    """tuple 显式双通道契约：命中 DROPME 剔除，其余保留。"""
    return (v, v != "DROPME")


def _drop_masked_card(v, ctx=None):
    """bool 谓词契约：星号遮蔽卡号（无法还原）应剔除而非静默通过。"""
    return "*" in str(v)


clean_ops.register_op("d2c_drop_x", impl="py", layer="clean", fn=_drop_x,
                      description="测试：名称为 X 时剔除")
clean_ops.register_op("d2c_drop_cond", impl="py", layer="clean", fn=_drop_cond_tuple,
                      description="测试：tuple 契约条件剔除")
clean_ops.register_op("d2c_drop_masked_card", impl="py", layer="clean",
                      fn=_drop_masked_card,
                      description="测试：含星号遮蔽的卡号剔除")

_OBJ = {"name": "txn", "title": "交易", "pk": "txn_id", "kind": "event",
        "name_property": "person_raw",
        "properties": {"person_raw": "string", "card_no": "string",
                       "phone_raw": "string"}}

_ROWS = [
    ("张三", "622202020001", "13800138000"),
    ("李四", "622202020002", "13900139000"),
    ("王五", "622202020003", "13700137000"),
]


def _build(clean_decl, rows=None, extra=None):
    """临时案件包 + 内存源表 → build_ontology，返回 (conn, stats)。"""
    import copy
    b = {"object": "txn",
         "source": {"table": "TXN", "columns": {"person_raw": "主体",
                                                "card_no": "卡号",
                                                "phone_raw": "电话"}}}
    if clean_decl is not None:
        b["clean"] = clean_decl
    if extra:
        b.update(extra)
    with _PackCtx([_OBJ], [b]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TXN ("主体" VARCHAR, "卡号" VARCHAR, "电话" VARCHAR)')
        conn.executemany("INSERT INTO TXN VALUES (?,?,?)", rows or _ROWS)
        stats = build_ontology(conn, pack="p")
        return conn, stats


def _col(conn, col):
    return [r[0] for r in conn.execute(f"SELECT {col} FROM obj_txn").fetchall()]


class TestCleanScope(unittest.TestCase):
    def test_non_person_object_property_cleaned(self):
        """AC-1：event 对象（非 person）的属性可被清洗——此前被 otype.name 硬限制跳过。"""
        conn, stats = _build({"card_no": ["d2c_append_a"]})
        self.assertEqual(stats["objects"]["txn"], 3)
        self.assertTrue(all(v.endswith("A") for v in _col(conn, "card_no")))

    def test_non_name_column_cleaned(self):
        """AC-2：非名称列（phone_raw）可被清洗，名称列不受牵连。"""
        conn, _ = _build({"phone_raw": ["d2c_append_a"]})
        self.assertTrue(all(v.endswith("A") for v in _col(conn, "phone_raw")))
        self.assertEqual(_col(conn, "person_raw"),
                         sorted(("张三", "李四", "王五")))

    def test_array_form_backward_compat(self):
        """AC-3：数组形式 = _name 映射，滤行行为与现状一致（命中即剔除，其余保留）。"""
        rows = [("张三", "c1", "p1"), ("X", "c2", "p2"), ("王五", "c3", "p3")]
        conn, stats = _build(["d2c_drop_x"], rows=rows)
        self.assertEqual(stats["objects"]["txn"], 2)   # X 行被剔除
        self.assertNotIn("X", _col(conn, "person_raw"))

    def test_rules_execute_in_declared_order(self):
        """AC-4：规则按声明顺序链式执行（顺序敏感）。"""
        conn, _ = _build({"person_raw": ["d2c_append_a", "d2c_append_b"]})
        self.assertEqual(_col(conn, "person_raw"), sorted(("张三AB", "李四AB", "王五AB")))
        conn2, _ = _build({"person_raw": ["d2c_append_b", "d2c_append_a"]})
        self.assertEqual(_col(conn2, "person_raw"), sorted(("张三BA", "李四BA", "王五BA")))

    def test_undeclared_property_untouched(self):
        """AC-5：清洗只作用于已声明属性，未声明属性不被改动。"""
        conn, _ = _build({"card_no": ["d2c_append_a"]})
        self.assertEqual(sorted(_col(conn, "phone_raw")),
                         sorted(("13800138000", "13900139000", "13700137000")))

    def test_filter_drop_counted_in_clean_stats(self):
        """AD-3：滤行剔除计数进 stats["clean_stats"]，含对象/属性/规则定位信息。"""
        rows = [("张三", "c1", "p1"), ("X", "c2", "p2"), ("X", "c3", "p3")]
        _, stats = _build(["d2c_drop_x"], rows=rows)
        self.assertEqual(len(stats["clean_stats"]), 1)
        e = stats["clean_stats"][0]
        self.assertEqual(e["object"], "txn")
        self.assertEqual(e["property"], "person_raw")
        self.assertEqual(e["rule"], "d2c_drop_x")
        self.assertEqual(e["dropped_rows"], 2)

    def test_dropped_sample_masked(self):
        """AD-3：剔除样本脱敏——不含完整原始敏感值（星号卡号被剔除且样本脱敏）。"""
        rows = [("张三", "6222********7890", "p1"), ("李四", "622202020002", "p2")]
        _, stats = _build({"card_no": ["d2c_drop_masked_card"]}, rows=rows)
        self.assertEqual(len(stats["clean_stats"]), 1)
        e = stats["clean_stats"][0]
        self.assertEqual(e["object"], "txn")
        self.assertEqual(e["property"], "card_no")
        self.assertEqual(e["dropped_rows"], 1)
        sample = e["sample_masked"][0]
        self.assertTrue(sample.startswith("6"))
        self.assertNotIn("6222********7890", sample)   # 不含完整原始值
        self.assertIn("*", sample)                     # 已脱敏

    def test_tuple_contract_dual_channel(self):
        """AD-3：tuple 返回 (value, keep) 显式双通道——命中剔除、其余保留。"""
        rows = [("张三", "c1", "p1"), ("DROPME", "c2", "p2"), ("王五", "c3", "p3")]
        conn, stats = _build(["d2c_drop_cond"], rows=rows)
        self.assertEqual(stats["objects"]["txn"], 2)
        self.assertNotIn("DROPME", _col(conn, "person_raw"))
        self.assertEqual(stats["clean_stats"][0]["dropped_rows"], 1)

    def test_strip_value_writeback(self):
        """AD-3：str 返回 op 改值生效——strip 回写落 obj_*（此前 strip 仅作过滤谓词）。"""
        rows = [("张三", "  622202020001  ", "p1")]
        conn, _ = _build({"card_no": ["strip"]}, rows=rows)
        self.assertEqual(_col(conn, "card_no"), ["622202020001"])

    def test_clean_unknown_property_hard_fail(self):
        """声明不存在的属性 → 装载硬失败。"""
        with self.assertRaises(ValueError) as cm:
            _build({"ghost_col": ["strip"]})
        self.assertIn("ghost_col", str(cm.exception))

    def test_clean_sql_op_hard_fail(self):
        """clean 引用 SQL op → 硬失败（SQL op 属 transform 层，clean 仅 py op）。"""
        with self.assertRaises(ValueError) as cm:
            _build({"card_no": ["strip_thousands"]})
        self.assertIn("strip_thousands", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
