"""REQ-D-009 transform 层（脏值可用性抢救）测试（批 D2 / AD-1 投影改写管道）。

声明式 op 编译为 SQL 表达式注入 TRY_CAST 之前：
  - AC-1 `48,000.00` 经 transform 后可 CAST 为 DOUBLE（此前静默 NULL）；
  - AC-2 `2024年3月15日` 经 transform 后可 CAST 为 DATE；
  - AC-3 `￥1,280.50` 可 CAST 为 DOUBLE；
  - AC-4 transform 与 clean 作用域分离，互不干扰；
  - AC-5 未声明 transform 的属性行为与现状一致（TRY_CAST 脏值降级 NULL + 计数）；
  - AC-6 transform 引用未知 op 装载硬失败。
"""
import datetime
import unittest

import duckdb

from core.ontology import build_ontology
import core.ontology_loader as ol
from tests.test_one2one import _PackCtx

_OBJ = {"name": "txn", "title": "交易", "pk": "txn_id", "kind": "event",
        "name_property": "person_raw",
        "properties": {"person_raw": "string", "amount": "decimal",
                       "date": "date", "memo": "string"}}


def _bind(transform=None, clean=None, source_sql=False):
    b = {"object": "txn"}
    if source_sql:
        b["source_table"] = "TXN"
        b["source_sql"] = ("SELECT 主体 AS person_raw, 金额 AS amount, "
                           "日期 AS date, 备注 AS memo FROM TXN")
    else:
        b["source"] = {"table": "TXN",
                       "columns": {"person_raw": "主体", "amount": "金额",
                                   "date": "日期", "memo": "备注"}}
    if transform is not None:
        b["transform"] = transform
    if clean is not None:
        b["clean"] = clean
    return b


def _load(binding):
    with _PackCtx([_OBJ], [binding]):
        return ol.load_pack("p").object_bindings["txn"]


def _build(transform=None, clean=None, rows=None):
    """临时案件包 + 内存源表 → build_ontology，返回 (conn, stats)。"""
    with _PackCtx([_OBJ], [_bind(transform=transform, clean=clean)]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TXN ("主体" VARCHAR, "金额" VARCHAR, '
                     '"日期" VARCHAR, "备注" VARCHAR)')
        conn.executemany("INSERT INTO TXN VALUES (?,?,?,?)",
                         rows or [("张三", "48,000.00", "2024年3月15日", "签约")])
        stats = build_ontology(conn, pack="p")
        return conn, stats


def _one(conn, col):
    return conn.execute(f"SELECT {col} FROM obj_txn").fetchone()[0]


class TestTransformCompile(unittest.TestCase):
    def test_transform_compiled_into_projection_before_try_cast(self):
        """声明式 op 编译进投影：transform 表达式位于 TRY_CAST 之内（AD-1）。"""
        b = _load(_bind(transform={"amount": ["strip_thousands"]}))
        self.assertIn("TRY_CAST(regexp_replace(\"金额\", ',', '', 'g')", b.source_sql)
        self.assertEqual(b.transform, (("amount", ("strip_thousands",)),))

    def test_undeclared_property_projection_unchanged(self):
        """AC-5：未声明 transform 的属性编译不变（无 regexp_replace）。"""
        b = _load(_bind(transform={"amount": ["strip_thousands"]}))
        self.assertIn('"日期"', b.source_sql)     # date 属性直通 TRY_CAST
        self.assertNotIn("regexp_replace(\"日期\"", b.source_sql)


class TestTransformEndToEnd(unittest.TestCase):
    def test_thousands_casts_to_double(self):
        """AC-1：48,000.00 → 48000.0（此前 TRY_CAST 静默 NULL）。"""
        conn, stats = _build(transform={"amount": ["strip_thousands"],
                                       "date": ["cn_date_norm"]})
        self.assertEqual(_one(conn, "amount"), 48000.0)
        self.assertEqual(stats["dirty"], [])   # 抢救后无脏值计数

    def test_cn_date_casts_to_date(self):
        """AC-2：2024年3月15日 → DATE 2024-03-15。"""
        conn, _ = _build(transform={"date": ["cn_date_norm"]})
        self.assertEqual(_one(conn, "date"), datetime.date(2024, 3, 15))

    def test_currency_chain_casts_to_double(self):
        """AC-3：￥1,280.50 → 1280.5（货币符号与千分位两步链式归一）。"""
        conn, _ = _build(
            transform={"amount": ["strip_currency", "strip_thousands"]},
            rows=[("张三", "￥1,280.50", "2024-03-15", "报销")])
        self.assertEqual(_one(conn, "amount"), 1280.5)

    def test_transform_and_clean_scope_separation(self):
        """AC-4：transform（SQL 侧）与 clean（Python 侧）作用域分离，互不干扰。"""
        conn, _ = _build(transform={"amount": ["strip_thousands"]},
                         clean={"person_raw": ["strip"]},
                         rows=[("  李四  ", "48,000.00", "2024-03-15", "m")])
        self.assertEqual(_one(conn, "person_raw"), "李四")   # clean 改值
        self.assertEqual(_one(conn, "amount"), 48000.0)      # transform 抢救

    def test_string_property_transform(self):
        """string 属性投影同样注入 transform（无 CAST，表达式直通）。"""
        conn, _ = _build(transform={"memo": ["cn_date_norm"]},
                         rows=[("张三", "100", "2024-03-15", "签约2024年3月15日")])
        self.assertEqual(_one(conn, "memo"), "签约2024-3-15")

    def test_no_transform_backward_compat(self):
        """AC-5：未声明 transform → 脏值静默降级 NULL + 计数留痕（B2-08 现状口径）。"""
        conn, stats = _build(
            rows=[("张三", "48,000.00", "2024年3月15日", "m"),
                  ("李四", "12000", "2024-03-20", "m")])
        amounts = {r[0] for r in
                   conn.execute("SELECT amount FROM obj_txn").fetchall()}
        self.assertIn(None, amounts)          # 脏值降级 NULL
        self.assertIn(12000.0, amounts)       # 合法值不受影响
        self.assertEqual(len(stats["dirty"]), 2)   # amount/date 各计 1 行
        self.assertTrue(all("txn" in d for d in stats["dirty"]))


class TestTransformValidation(unittest.TestCase):
    def test_unknown_op_hard_fail(self):
        """AC-6：transform 引用未注册 op → 装载硬失败。"""
        with self.assertRaises(ValueError) as cm:
            _load(_bind(transform={"amount": ["no_such_op"]}))
        self.assertIn("no_such_op", str(cm.exception))
        self.assertIn("REQ-D-009", str(cm.exception))

    def test_py_op_in_transform_hard_fail(self):
        """transform 引用 py op → 硬失败（transform 仅 impl=sql 声明式 op）。"""
        with self.assertRaises(ValueError) as cm:
            _load(_bind(transform={"amount": ["strip"]}))
        self.assertIn("strip", str(cm.exception))

    def test_transform_on_source_sql_binding_hard_fail(self):
        """transform 仅支持结构化源；手写 source_sql 请在上游完成变换。"""
        with self.assertRaises(ValueError) as cm:
            _load(_bind(transform={"amount": ["strip_thousands"]}, source_sql=True))
        self.assertIn("结构化源", str(cm.exception))

    def test_transform_unknown_property_hard_fail(self):
        """transform 属性不在对象声明内 → 装载硬失败。"""
        with self.assertRaises(ValueError) as cm:
            _load(_bind(transform={"ghost": ["strip_thousands"]}))
        self.assertIn("ghost", str(cm.exception))

    def test_param_op_whitelist_enforced_d5(self):
        """REQ-D-011：带参 op（op:param）参数仅白名单取值——无白名单 op 带参、
        未注册带参 op 均硬失败（自由文本参数防注入）。"""
        # transform 层 sql op 带了它不接受的参数 → 硬失败
        with self.assertRaises(ValueError) as cm:
            _load(_bind(transform={"amount": ["digits_only:xx"]}))
        self.assertIn("不接受参数", str(cm.exception))
        # 未注册的带参 op 同样硬失败（不报"D5 拒绝"，而是未注册 fail-closed）
        with self.assertRaises(ValueError) as cm2:
            _load(_bind(transform={"amount": ["regex:,=>"]}))
        self.assertIn("未在 op 注册表注册", str(cm2.exception))


if __name__ == "__main__":
    unittest.main()
