"""
core/store.py
L1 Redis + L2 DuckDB + L3 Parquet 统一接口。

设计要点：
- L2 是单个 DuckDB 文件（investigation.duckdb），含预聚合表 agg_* 与物化视图
- L3 是分区 Parquet，通过 read_parquet(...) 直接查询，零 ETL
- L1 特征层用 dict 模拟（生产可换 redis-py），对外接口不变
"""

from pathlib import Path
import duckdb
from typing import Any


class Store:
    def __init__(self, root: str = "data", db_path: str = "investigation.duckdb"):
        self.root = Path(root)
        self.db_path = db_path
        self._conn = None
        self.l1: dict[str, Any] = {}  # 特征/五间命中热层（生产换 Redis）

    # ---------- L2 DuckDB ----------
    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(self.db_path)
        return self._conn

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行 SQL，返回字典列表（兼容 StarRocks 的 cursor 用法）"""
        rows = self.conn.execute(sql, params).fetchall()
        cols = [d[0] for d in self.conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.execute(sql, params)

    # ---------- L3 Parquet ----------
    def cold_scan(self, pattern: str, extra_where: str = "", params: tuple = ()) -> list[dict]:
        """直接扫分区 Parquet，零 ETL。生产可加分区裁剪谓词下推。"""
        sql = f"SELECT * FROM read_parquet('{self.root / pattern}')"
        if extra_where:
            sql += f" WHERE {extra_where}"
        return self.query(sql, params)

    # ---------- L1 特征层 ----------
    def set_feature(self, subject: str, ftype: str, value: Any, version: int = 1) -> None:
        self.l1[f"feature:{subject}:{ftype}"] = {"value": value, "version": version}

    def get_feature(self, subject: str, ftype: str) -> Any:
        return self.l1.get(f"feature:{subject}:{ftype}")

    def all_features(self, subject: str) -> dict[str, Any]:
        prefix = f"feature:{subject}:"
        return {k: v for k, v in self.l1.items() if k.startswith(prefix)}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
