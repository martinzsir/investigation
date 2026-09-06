"""REQ-D-015 业务键去重测试（binding key：columns + on_conflict）。

电诈需按流水号去重——同一笔交易重复导入应按业务键识别，而非全行比对
（IS NOT DISTINCT FROM 所有列）。去重在代理键分配**之前**执行。

  - AC-1 按业务键去重生效（同键内容略异的两行折叠为一行），非全行比对
  - AC-2 keep_latest 保留末行 / keep_first 保留首行 / fail 冲突硬失败
  - AC-3 未声明 key 时行为与现状一致（同键异值行各自保留）
  - AC-4 业务键冲突数量被统计（stats["dedup_conflicts"]）
  - AC-5 业务键含空值：整行剔除并留痕，不静默保留
"""
import unittest

import duckdb

from core.ontology import build_ontology
import core.ontology_loader as ol
from tests.test_one2one import _PackCtx

_OBJ = {"name": "txn", "title": "交易", "pk": "txn_id", "kind": "event",
        "name_property": "serial_no",
        "properties": {"serial_no": "string", "amount": "decimal"}}


def _bind(**extra):
    b = {"object": "txn",
         "source": {"table": "TXN",
                    "columns": {"serial_no": "流水号", "amount": "金额"}}}
    b.update(extra)
    return b


def _build(rows, **extra):
    with _PackCtx([_OBJ], [_bind(**extra)]):
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE TXN ("流水号" VARCHAR, "金额" VARCHAR)')
        conn.executemany("INSERT INTO TXN VALUES (?,?)", rows)
        stats = build_ontology(conn, pack="p")
        return conn, stats


def _by_serial(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT serial_no, amount FROM obj_txn").fetchall()}


class TestDedupKey(unittest.TestCase):
    def test_AC1_dedup_by_business_key_not_full_row(self):
        """同流水号两行金额略异：全行比对不会去重，业务键去重折叠为一行。"""
        conn, stats = _build(
            [("T001", "100"), ("T001", "200"), ("T002", "300")],
            key={"columns": ["serial_no"], "on_conflict": "keep_latest"})
        rows = _by_serial(conn)
        self.assertEqual(set(rows), {"T001", "T002"})   # 同键折叠
        self.assertEqual(rows["T001"], 200.0)           # 保留末行
        self.assertTrue(stats["dedup_conflicts"])
        self.assertEqual(stats["dedup_conflicts"][0]["conflict_groups"], 1)

    def test_AC2_keep_first_keeps_first_row(self):
        """keep_first：同键保留首行（amount=100）。"""
        conn, _ = _build(
            [("T001", "100"), ("T001", "200")],
            key={"columns": ["serial_no"], "on_conflict": "keep_first"})
        rows = _by_serial(conn)
        self.assertEqual(rows["T001"], 100.0)

    def test_AC2_fail_raises_on_conflict(self):
        """fail：同键冲突即硬失败（不静默收敛）。"""
        with self.assertRaises(ValueError) as cm:
            _build([("T001", "100"), ("T001", "200")],
                   key={"columns": ["serial_no"], "on_conflict": "fail"})
        self.assertIn("业务键", str(cm.exception))

    def test_AC3_no_key_keeps_all_rows(self):
        """未声明 key：同流水号异金额两行内容不同，各自保留（现状口径）。"""
        conn, stats = _build([("T001", "100"), ("T001", "200"), ("T002", "300")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM obj_txn").fetchone()[0], 3)
        self.assertFalse(stats.get("dedup_conflicts"))

    def test_AC4_AC5_conflict_counted_and_null_key_dropped(self):
        """冲突组统计落 stats；业务键空值行剔除并留痕（不静默）。"""
        conn, stats = _build(
            [("T001", "100"), ("T001", "200"), (None, "300")],
            key={"columns": ["serial_no"], "on_conflict": "keep_latest"})
        # 空键行剔除，T001 收敛为一行
        self.assertEqual(set(_by_serial(conn)), {"T001"})
        self.assertEqual(stats["dedup_conflicts"][0]["duplicate_rows"], 1)
        null_drop = [e for e in stats["clean_stats"]
                     if e["rule"] == "dedup_key_null"]
        self.assertEqual(len(null_drop), 1)
        self.assertEqual(null_drop[0]["dropped_rows"], 1)

    def test_loader_rejects_unknown_key_column(self):
        """loader：key.columns 含未声明属性 → 装载硬失败。"""
        with _PackCtx([_OBJ], [_bind(key={"columns": ["nope"]})]):
            with self.assertRaises(ValueError) as cm:
                ol.load_pack("p")
        self.assertIn("key.columns", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
