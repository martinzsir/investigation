"""REQ-D-010 on_cast_error 三态（CAST 失败处置：null / fail / quarantine）测试。

三态语义（bindings.json 属性级映射，仅结构化源）：
  - null（缺省）：TRY_CAST 降级 NULL + 脏值计数留痕（B2-08 现状，回归保障）；
  - fail：渲染硬 CAST——脏值中断 build（显式声明"本列必须干净"的回退能力）；
  - quarantine：TRY_CAST 失败行整行剔出 obj_* 并落 build_quarantine
    （含失败原因 + 脱敏样本 + name_value，AD-4），诊断 kind=source_value_quarantined
    不静默（AC-3/AC-5）；
  - 隐藏列 __raw_<别名> 仅存在于编译期投影，物化前剔除——obj_* schema 不变。
"""
import unittest

import duckdb

from core.ontology import build_ontology
from core.run_health import RunHealth, record_build_quarantine
import core.ontology_loader as ol
from tests.test_one2one import _PackCtx

_OBJ = {"name": "txn", "title": "交易", "pk": "txn_id", "kind": "event",
        "name_property": "person_raw",
        "properties": {"person_raw": "string", "amount": "decimal",
                       "date": "date", "memo": "string"}}

_ROWS = [
    ("张三", "48000", "2024-03-15", "签约"),      # 好行
    ("李四", "12000.50", "2024-03-16", "报销"),   # 好行
    ("王五", "四万八", "2024-03-17", "现金"),      # 金额脏
    ("赵六", "N/A", "2024-03-18", "现金"),         # 金额脏
]


def _bind(**extra):
    b = {"object": "txn",
         "source": {"table": "TXN",
                    "columns": {"person_raw": "主体", "amount": "金额",
                                "date": "日期", "memo": "备注"}}}
    b.update(extra)
    return b


def _load(**extra):
    with _PackCtx([_OBJ], [_bind(**extra)]):
        return ol.load_pack("p")


def _build(rows=None, **extra):
    """临时案件包 + 内存源表 → build_ontology，返回 (conn, stats)。"""
    with _PackCtx([_OBJ], [_bind(**extra)]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TXN ("主体" VARCHAR, "金额" VARCHAR, '
                     '"日期" VARCHAR, "备注" VARCHAR)')
        conn.executemany("INSERT INTO TXN VALUES (?,?,?,?)",
                         rows if rows is not None else _ROWS)
        stats = build_ontology(conn, pack="p")
        return conn, stats


def _obj_cols(conn):
    return {d[0] for d in
            conn.execute("SELECT * FROM obj_txn LIMIT 0").description}


class TestQuarantine(unittest.TestCase):
    def test_quarantine_removes_dirty_keeps_good(self):
        """4 行 2 脏 → obj_txn 仅剩 2 好行，build_quarantine 落 2 条。"""
        conn, stats = _build(on_cast_error={"amount": "quarantine"})
        rows = conn.execute(
            "SELECT person_raw, amount FROM obj_txn "
            "ORDER BY person_raw").fetchall()
        self.assertEqual(rows, [("张三", 48000.0), ("李四", 12000.5)])
        n = conn.execute(
            "SELECT COUNT(*) FROM build_quarantine").fetchone()[0]
        self.assertEqual(n, 2)
        self.assertNotIn("__raw_amount", _obj_cols(conn))   # 隐藏列不进 obj_*

    def test_quarantine_records_reason_sample_name(self):
        """隔离记录：reason=<值类型>_cast_failed + 脱敏样本 + name_value + 源表。"""
        conn, stats = _build(on_cast_error={"amount": "quarantine"})
        recs = conn.execute(
            "SELECT object, property, src_column, reason, sample_masked, "
            "name_value, source_table FROM build_quarantine").fetchall()
        self.assertEqual(len(recs), 2)
        r = next(x for x in recs if x[5] == "赵六")   # 按 name_value 精确取行（不依赖中文排序）
        self.assertEqual(r[:4], ("txn", "amount", "金额",
                                 "decimal_cast_failed"))
        self.assertEqual(r[4], "N**")          # _mask_sample("N/A")
        self.assertEqual(r[6], "TXN")
        q = stats["quarantine"]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["object"], "txn")
        self.assertEqual(q[0]["property"], "amount")
        self.assertEqual(q[0]["column"], "金额")
        self.assertEqual(q[0]["reason"], "decimal_cast_failed")
        self.assertEqual(q[0]["quarantined_rows"], 2)
        self.assertEqual(len(q[0]["sample_masked"]), 2)   # 脱敏样本随 stats 可审计

    def test_quarantine_diagnostic_recorded(self):
        """record_build_quarantine 落 run_diagnostic（kind=source_value_quarantined）。"""
        conn, stats = _build(on_cast_error={"amount": "quarantine"})
        n = record_build_quarantine(conn, stats, run_id="run-cast-test")
        self.assertEqual(n, 1)
        rows = conn.execute(
            "SELECT severity, reason FROM run_diagnostic "
            "WHERE kind='source_value_quarantined' AND run_id='run-cast-test'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "warning")
        self.assertIn("txn.amount", rows[0][1])
        self.assertIn("2 行已隔离", rows[0][1])

    def test_source_null_not_quarantined(self):
        """源列本身为 NULL（非 CAST 失败）→ 正常保留 NULL 语义，不隔离。"""
        conn, stats = _build(
            rows=[("张三", None, "2024-03-15", "m"),
                  ("李四", "四万八", "2024-03-16", "m")],
            on_cast_error={"amount": "quarantine"})
        rows = conn.execute(
            "SELECT person_raw FROM obj_txn ORDER BY person_raw").fetchall()
        self.assertEqual(rows, [("张三",)])       # 仅 CAST 失败行被隔离
        q = stats["quarantine"][0]
        self.assertEqual(q["quarantined_rows"], 1)


class TestFailMode(unittest.TestCase):
    def test_fail_mode_aborts_build(self):
        """fail：脏值渲染硬 CAST → build 抛 ConversionException（回退能力）。"""
        with self.assertRaises(duckdb.ConversionException):
            _build(on_cast_error={"amount": "fail"})

    def test_fail_mode_clean_data_builds(self):
        """fail + 干净数据 → 正常构建（fail 只是回退能力，不是禁用）。"""
        conn, stats = _build(
            rows=[("张三", "48000", "2024-03-15", "签约")],
            on_cast_error={"amount": "fail"})
        self.assertEqual(
            conn.execute("SELECT amount FROM obj_txn").fetchone()[0], 48000.0)
        self.assertEqual(stats["dirty"], [])


class TestNullMode(unittest.TestCase):
    def test_null_mode_dirty_counted(self):
        """null（显式 JSON null）：TRY_CAST 降级 NULL + stats["dirty"] 计数。"""
        conn, stats = _build(on_cast_error={"amount": None})
        self.assertEqual(len(stats["dirty"]), 1)
        self.assertIn("amount", stats["dirty"][0])
        self.assertIn("txn", stats["dirty"][0])
        self.assertEqual(stats["quarantine"], [])

    def test_default_backward_compat(self):
        """缺省（未声明 on_cast_error）= null：B2-08 现状语义不变；不建隔离表。"""
        conn, stats = _build()
        self.assertEqual(len(stats["dirty"]), 1)
        self.assertEqual(stats["quarantine"], [])
        n = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name='build_quarantine'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_mixed_null_and_quarantine(self):
        """同 binding 混装：amount=quarantine 隔离、date 未声明（null）仍走脏值计数。"""
        rows = [("张三", "48000", "2024-03-15", "签约"),
                ("李四", "四万八", "2024年3月16日", "现金")]
        conn, stats = _build(rows=rows,
                             on_cast_error={"amount": "quarantine"})
        self.assertEqual(stats["quarantine"][0]["property"], "amount")
        self.assertEqual(stats["quarantine"][0]["quarantined_rows"], 1)
        self.assertEqual(len(stats["dirty"]), 1)   # date 仍按 null 态计数
        self.assertIn("date", stats["dirty"][0])
        n = conn.execute("SELECT COUNT(*) FROM obj_txn").fetchone()[0]
        self.assertEqual(n, 1)   # 李四整行隔离，张三保留


class TestLoaderValidation(unittest.TestCase):
    def test_string_property_hard_fail(self):
        """string 属性无 CAST 无从失败 → 装载硬失败。"""
        obj = {"name": "txn", "title": "T", "pk": "txn_id", "kind": "event",
               "name_property": "person_raw",
               "properties": {"person_raw": "string", "memo": "string"}}
        with _PackCtx([obj], [_bind(on_cast_error={"memo": "quarantine"})]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("string", str(cm.exception))
            self.assertIn("REQ-D-010", str(cm.exception))

    def test_source_sql_binding_hard_fail(self):
        """on_cast_error 仅支持结构化源；手写 source_sql 请在上游保证 CAST 语义。"""
        bind = {"object": "txn", "source_table": "TXN",
                "source_sql": "SELECT 主体 AS person_raw, 金额 AS amount, "
                              "日期 AS date, 备注 AS memo FROM TXN",
                "on_cast_error": {"amount": "quarantine"}}
        with _PackCtx([_OBJ], [bind]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
            self.assertIn("结构化源", str(cm.exception))

    def test_unknown_state_hard_fail(self):
        """未知状态硬失败（fail-closed）：仅允许 fail / quarantine。"""
        with _PackCtx([_OBJ], [_bind(on_cast_error={"amount": "explode"})]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
        msg = str(cm.exception)
        self.assertIn("explode", msg)
        self.assertIn("REQ-D-010", msg)

    def test_quarantine_parse_stored(self):
        """合法声明装载为 ((属性, 状态), ...) 元组并存入 binding。"""
        pack = _load(on_cast_error={"amount": "quarantine"})
        b = pack.object_bindings["txn"]
        self.assertEqual(b.on_cast_error, (("amount", "quarantine"),))
        self.assertIn('__raw_amount"', b.source_sql)   # 编译期追加隐藏列


if __name__ == "__main__":
    unittest.main()
