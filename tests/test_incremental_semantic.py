"""
tests/test_incremental_semantic.py
REQ-004 语义层增量重建 端到端测试（scripts.incremental + rebuild_from_partition）。

覆盖 AC1-AC4/AC6（AC5 受保护版本属 REQ-016，本批跳过）：
  AC1: 增量完成后 freshness() = FRESH
  AC2: 10 万全量 + 100 新行，重写行数 ≤ 1000（未变行不重写）
  AC3: 分区文件缺失 → PartitionMissingError，main 退出码 1
  AC4: 同分区重复应用幂等（不重复装载、input_hashes 不变、不重复发物化事件）
  AC6: 命名约定 {数据集}_{yyyy}Q{n}.parquet
另外覆盖 REQ-005 隔离路径：schema 漂移分区退出码 2 且不装载。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology import build_ontology                          # noqa: E402
from core.ontology_version import current_version, freshness      # noqa: E402
from scripts.incremental import (                                 # noqa: E402
    run_incremental, PartitionMissingError,
)


def _init_l2(con) -> None:
    con.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    con.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    con.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    con.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    con.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    con.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    con.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")


class TestIncrementalSemantic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 10 万全量基线（2021 年）只构建一次，每例复制隔离（build 较重）
        cls._baseline_tmp = tempfile.TemporaryDirectory()
        cls.baseline_db = str(Path(cls._baseline_tmp.name) / "baseline.duckdb")
        con = duckdb.connect(cls.baseline_db)
        try:
            _init_l2(con)
            con.execute("""
                INSERT INTO 银行流水
                SELECT '主体' || (i % 500) AS 主体,
                       '对方' || (i % 997) AS 对方,
                       CAST(i % 1000 AS DOUBLE) AS 金额,
                       CAST(CAST('2021-01-01' AS DATE)
                            + CAST(i % 365 AS INTEGER) AS VARCHAR) AS 日期
                FROM range(100000) t(i)
            """)
            build_ontology(con)
        finally:
            con.close()

    @classmethod
    def tearDownClass(cls):
        cls._baseline_tmp.cleanup()

    def setUp(self):
        import shutil
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "inv.duckdb")
        shutil.copy(self.baseline_db, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_partition(self, quarter: str, select_sql: str) -> Path:
        """按命名约定写分区 parquet：data/银行流水_{quarter}.parquet。"""
        path = self.dir / f"银行流水_{quarter}.parquet"
        con = duckdb.connect(self.db)
        try:
            con.execute(f"COPY ({select_sql}) TO '{path}' (FORMAT PARQUET)")
        finally:
            con.close()
        return path

    def _connect(self):
        return duckdb.connect(self.db)

    def test_ac1_ac2_ac6_incremental_fresh_and_cheap(self):
        """AC1 FRESH / AC2 只重写增量行 / AC6 命名约定。"""
        # 新分区：100 行（2022Q1），主体既有、对手方全新
        self._write_partition("2022Q1", """
            SELECT '主体0' AS 主体,
                   '新对手' || i AS 对方,
                   (i * 100)::DOUBLE AS 金额,
                   CAST(CAST('2022-01-01' AS DATE) + CAST(i AS INTEGER) AS VARCHAR) AS 日期
            FROM range(100) t(i)
        """)

        rc = run_incremental("2022Q1", db_path=self.db, data_dir=str(self.dir))
        self.assertEqual(rc, 0)

        con = self._connect()
        try:
            # AC1: 增量后 FRESH
            self.assertEqual(freshness(con).state, "FRESH")
            # 源表与语义层行数
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM 银行流水").fetchone()[0], 100100)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM obj_transaction").fetchone()[0], 100100)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM lnk_transfers").fetchone()[0], 100100)
            # AC2: 物化事件记录的重写行数只含增量（100 交易 + 100 账户 + 100 转账边 ≈ 300）
            ev = con.execute(
                "SELECT payload FROM event_log "
                "WHERE type='ontology.materialized' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(ev)
            payload = json.loads(ev[0])
            self.assertGreater(payload["rewritten_rows"], 0)
            self.assertLessEqual(
                payload["rewritten_rows"], 1000,
                f"重写 {payload['rewritten_rows']} 行，未变行被重写了？")
            # 到达事件携带分区水标
            arr = con.execute(
                "SELECT payload FROM event_log "
                "WHERE type='source.partition.arrived' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(json.loads(arr[0])["row_count"], 100)
        finally:
            con.close()

    def test_ac3_missing_partition_nonzero_exit(self):
        """AC3: 分区缺失抛 PartitionMissingError；main() 退出码 1（不静默跳过）。"""
        with self.assertRaises(PartitionMissingError) as ctx:
            run_incremental("1999Q4", db_path=self.db, data_dir=str(self.dir))
        # AC6: 错误信息体现命名约定
        self.assertIn("银行流水_1999Q4.parquet", str(ctx.exception))

        r = subprocess.run(
            [sys.executable, "-m", "scripts.incremental",
             "--quarter", "1999Q4", "--db", self.db,
             "--data-dir", str(self.dir)],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_ac4_idempotent_reapply(self):
        """AC4: 同分区同内容重复应用 → 跳过装载与重建，input_hashes 不变。"""
        self._write_partition("2022Q1", """
            SELECT '主体0' AS 主体, '新对手' || i AS 对方,
                   (i * 100)::DOUBLE AS 金额,
                   CAST(CAST('2022-01-01' AS DATE) + CAST(i AS INTEGER) AS VARCHAR) AS 日期
            FROM range(100) t(i)
        """)
        self.assertEqual(
            run_incremental("2022Q1", db_path=self.db, data_dir=str(self.dir)), 0)

        con = self._connect()
        try:
            v1 = current_version(con)
            n_mat = con.execute(
                "SELECT COUNT(*) FROM event_log WHERE type='ontology.materialized'"
            ).fetchone()[0]
            n_arr = con.execute(
                "SELECT COUNT(*) FROM event_log WHERE type='source.partition.arrived'"
            ).fetchone()[0]
            n_rows = con.execute("SELECT COUNT(*) FROM 银行流水").fetchone()[0]
        finally:
            con.close()

        # 重复应用
        rc = run_incremental("2022Q1", db_path=self.db, data_dir=str(self.dir))
        self.assertEqual(rc, 0)

        con = self._connect()
        try:
            v2 = current_version(con)
            self.assertEqual(v1.input_hashes, v2.input_hashes)
            self.assertEqual(v1.build_id, v2.build_id)  # 未重建 → 版本未推进
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM 银行流水").fetchone()[0], n_rows)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM event_log "
                    "WHERE type='ontology.materialized'").fetchone()[0], n_mat)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM event_log "
                    "WHERE type='source.partition.arrived'").fetchone()[0], n_arr)
            # 幂等台账已登记
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM meta_applied_partition").fetchone()[0], 1)
        finally:
            con.close()

    def test_quarantine_bad_schema_exit_2(self):
        """REQ-005: schema 漂移分区隔离，退出码 2，不装载进 L2。"""
        # 缺列（对方/日期）→ SCHEMA_DRIFT
        self._write_partition("2022Q2", """
            SELECT '主体0' AS 主体, 1.0 AS 金额 FROM range(3) t(i)
        """)
        rc = run_incremental("2022Q2", db_path=self.db, data_dir=str(self.dir))
        self.assertEqual(rc, 2)

        con = self._connect()
        try:
            # 未装载：基线仍是 100000 行
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM 银行流水").fetchone()[0], 100000)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM partition_quarantined").fetchone()[0], 1)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
