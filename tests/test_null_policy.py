"""REQ-D-014 属性级空值策略测试（allow / reject / quarantine）。

电诈场景"金额为空"与"备注为空"严重性不同。空值策略在 fetch 后最先执行
（空值先于 CAST）：真空值（源列也空）走 null_policy，CAST 失败（源列非空→NULL）
走 on_cast_error，两者可组合且优先级明确。

  - AC-1 reject：空值行剔除并记原因（clean_stats，rule=null_policy:reject）
  - AC-2 allow：空值行保留 NULL，与现状一致
  - AC-3 transform/物化后行数与空值统计准确
  - AC-4 空值策略与 on_cast_error 可组合、分流不混淆
  - AC-5 空值统计进健康度（reject→clean_drop_rate；quarantine→source_value_quarantined）
"""
import unittest

import duckdb

from core.ontology import build_ontology
from core.run_health import (RunHealth, record_build_quarantine,
                             record_clean_stats)
import core.ontology_loader as ol
from tests.test_one2one import _PackCtx

_OBJ = {"name": "txn", "title": "交易", "pk": "txn_id", "kind": "event",
        "name_property": "person_raw",
        "properties": {"person_raw": "string", "amount": "decimal",
                       "memo": "string"}}


def _bind(**extra):
    b = {"object": "txn",
         "source": {"table": "TXN",
                    "columns": {"person_raw": "主体", "amount": "金额",
                                "memo": "备注"}}}
    b.update(extra)
    return b


def _build(rows, **extra):
    with _PackCtx([_OBJ], [_bind(**extra)]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TXN ("主体" VARCHAR, "金额" VARCHAR, "备注" VARCHAR)')
        conn.executemany("INSERT INTO TXN VALUES (?,?,?)", rows)
        stats = build_ontology(conn, pack="p")
        return conn, stats


def _names(conn):
    return [r[0] for r in conn.execute(
        "SELECT person_raw FROM obj_txn ORDER BY person_raw").fetchall()]


class TestNullPolicy(unittest.TestCase):
    def test_AC1_reject_drops_null_rows_with_reason(self):
        """金额为空行被 reject 剔除，非空行保留，clean_stats 记 null_policy:reject。"""
        conn, stats = _build(
            [("张三", "48000", "备注1"),
             ("李四", None, "备注2"),       # 金额空 → reject
             ("王五", "12000", "备注3")],
            null_policy={"amount": "reject"})
        self.assertEqual(_names(conn), ["张三", "王五"])
        cs = [e for e in stats["clean_stats"] if e["rule"] == "null_policy:reject"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["property"], "amount")
        self.assertEqual(cs[0]["dropped_rows"], 1)

    def test_AC2_allow_keeps_null_rows(self):
        """allow（缺省）：备注为空的行保留 NULL，行数不变。"""
        conn, stats = _build(
            [("张三", "48000", None),        # 备注空，allow 保留
             ("李四", "12000", "备注2")],
            null_policy={"memo": "allow"})
        self.assertEqual(_names(conn), ["张三", "李四"])
        memo_null = conn.execute(
            "SELECT COUNT(*) FROM obj_txn WHERE memo IS NULL").fetchone()[0]
        self.assertEqual(memo_null, 1)
        self.assertFalse(any(e.get("rule") == "null_policy:reject"
                             for e in stats["clean_stats"]))

    def test_AC4_null_before_cast_priority(self):
        """空值(reject) 与 CAST 失败(quarantine) 同列组合：真空值剔除、CAST 失败隔离。"""
        conn, stats = _build(
            [("张三", "48000", "备注1"),
             ("李四", None, "备注2"),        # 真空值 → null_policy reject
             ("王五", "四万八", "备注3")],    # CAST 失败 → on_cast_error quarantine
            null_policy={"amount": "reject"},
            on_cast_error={"amount": "quarantine"})
        # 仅张三存活
        self.assertEqual(_names(conn), ["张三"])
        # 李四走 reject（clean_stats），不进 quarantine
        cs = [e for e in stats["clean_stats"] if e["rule"] == "null_policy:reject"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["dropped_rows"], 1)
        # 王五走 CAST 隔离（reason=decimal_cast_failed），不是 null_value
        q = conn.execute(
            "SELECT name_value, reason FROM build_quarantine").fetchall()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0][1], "decimal_cast_failed")

    def test_quarantine_state_isolates_null_rows(self):
        """quarantine 态：空值行整行落 build_quarantine（reason=null_value）。"""
        conn, stats = _build(
            [("张三", "48000", "备注1"),
             ("李四", None, "备注2")],       # 金额空 → quarantine
            null_policy={"amount": "quarantine"})
        self.assertEqual(_names(conn), ["张三"])
        rows = conn.execute(
            "SELECT name_value, reason FROM build_quarantine").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "null_value")
        self.assertTrue(any(e.get("reason") == "null_value"
                            for e in stats["quarantine"]))

    def test_AC5_null_stats_recorded_to_health(self):
        """reject 剔除落 clean_drop_rate；null_value 隔离落 source_value_quarantined。"""
        conn, _ = _build(
            [("张三", "48000", "备注1"),
             ("李四", None, "备注2"),
             ("王五", None, "备注3")],
            null_policy={"amount": "quarantine"})
        rh = RunHealth(conn)
        n_q = record_build_quarantine(conn,
                                      {"quarantine": [{"object": "txn", "property": "amount",
                                                       "column": "金额", "reason": "null_value",
                                                       "quarantined_rows": 2,
                                                       "sample_masked": []}]},
                                      run_id=rh.run_id)
        n_c = record_clean_stats(conn,
                                 {"clean_stats": [{"object": "txn", "property": "memo",
                                                   "rule": "null_policy:reject",
                                                   "dropped_rows": 1, "rows_before": 3,
                                                   "sample_masked": []}]},
                                 run_id=rh.run_id)
        self.assertEqual(n_q, 1)
        self.assertEqual(n_c, 1)
        s = rh.summary()
        kinds = s["by_kind"]
        self.assertIn("source_value_quarantined", kinds)
        self.assertIn("clean_drop_rate", kinds)
        self.assertIn("build_ontology", s["by_source"])


if __name__ == "__main__":
    unittest.main()
