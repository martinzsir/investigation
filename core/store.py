"""
core/store.py
L1 Redis + L2 DuckDB + L3 Parquet 统一接口。

设计要点：
- L2 是单个 DuckDB 文件（investigation.duckdb），含预聚合表 agg_* 与物化视图
- L3 是分区 Parquet，通过 read_parquet(...) 直接查询，零 ETL
- L1 特征层：feat_l1 持久化表（core.features.FeatureStore）+ 进程内 dict 热缓存

REQ-003 直查拦截：
- 默认路径禁止经 query() 直读中文业务源表（银行流水/通话记录/...），
  检测器/技能一律消费 obj_*/lnk_* 语义层（见 core.gateway.OntologyReadGateway）；
- 显式 unsafe=True 调试通道：必须提供 operator + reason，强制审计落盘
  （meta_unsafe_query）并套 max_rows 行数上限；
- execute() 为写/DDL 路径（ActionExecutor 唯一写入口语义不变），不套只读拦截。
"""

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb


# 受保护的 L2 业务源表（L3 Parquet 经 read_parquet 读取，表名在字符串字面量内，
# 不算直查表引用；冷扫描请用 cold_scan / unsafe 通道并留痕）
_FORBIDDEN_TABLES = (
    "银行流水", "通话记录", "招投标档案", "工商信息",
    "轨迹出行", "公开OSINT", "举报材料",
)

_UNSAFE_DDL = """
CREATE TABLE IF NOT EXISTS meta_unsafe_query (
    audit_id VARCHAR PRIMARY KEY,
    operator VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    sql_text TEXT NOT NULL,
    max_rows INTEGER NOT NULL,
    row_count INTEGER,
    created_at VARCHAR NOT NULL
)
"""


class DirectSourceAccessError(PermissionError):
    """默认读路径直查业务源表被拦截（REQ-003）。"""


def _strip_sql_literals(sql: str) -> str:
    """去掉单引号字符串字面量内容（read_parquet('data/银行流水.parquet') 不算表引用）。"""
    return re.sub(r"'[^']*'", "''", sql or "")


def touches_forbidden_table(sql: str) -> list[str]:
    """SQL 中作为标识符出现的受保护源表（已剔除字符串字面量）。"""
    stripped = _strip_sql_literals(sql)
    return [t for t in _FORBIDDEN_TABLES if t in stripped]


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

    def query(self, sql: str, params: tuple = (), *,
              unsafe: bool = False, reason: str | None = None,
              operator: str | None = None, max_rows: int = 1000) -> list[dict]:
        """执行只读 SQL，返回字典列表（兼容 StarRocks 的 cursor 用法）。

        unsafe=True 为显式调试通道（REQ-003）：
          - 必须同时提供 operator（具名操作者）与 reason（理由），否则 ValueError；
          - 每次调用落 meta_unsafe_query 审计表；
          - 自动套 max_rows 上限（默认 1000），防全表拖库。
        """
        if unsafe:
            if not reason or not operator:
                raise ValueError(
                    "unsafe 直查必须提供 operator（具名操作者）与 reason（用途理由）")
            if not isinstance(max_rows, int) or max_rows <= 0:
                raise ValueError("max_rows 必须是正整数")
            sql = self._wrap_limit(sql, max_rows)
            rows = self._fetch(sql, params)
            self._audit_unsafe(operator, reason, sql, max_rows, len(rows))
            return rows

        bad = touches_forbidden_table(sql)
        if bad:
            raise DirectSourceAccessError(
                f"直查业务源表被拦截：{bad}。检测器/技能只能读 obj_*/lnk_* 语义层"
                f"（经 core.gateway.OntologyReadGateway）；确需直查请用 "
                f"query(..., unsafe=True, operator=..., reason=...) 调试通道并留痕。")
        return self._fetch(sql, params)

    def _fetch(self, sql: str, params: tuple) -> list[dict]:
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.execute(sql, params)

    @staticmethod
    def _wrap_limit(sql: str, max_rows: int) -> str:
        """无 LIMIT 的查询包一层上限；已有 LIMIT 保持原样。"""
        if re.search(r"\bLIMIT\b", sql or "", flags=re.IGNORECASE):
            return sql
        return f"SELECT * FROM ({sql.rstrip(';')}) LIMIT {max_rows}"

    def _audit_unsafe(self, operator: str, reason: str,
                      sql: str, max_rows: int, row_count: int) -> None:
        self.conn.execute(_UNSAFE_DDL)
        self.conn.execute(
            """INSERT INTO meta_unsafe_query
               (audit_id, operator, reason, sql_text, max_rows, row_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [uuid.uuid4().hex, operator, reason, sql, max_rows, row_count,
             datetime.now().isoformat(timespec="seconds")])

    # ---------- L3 Parquet ----------
    def cold_scan(self, pattern: str, extra_where: str = "", params: tuple = (),
                  *, operator: str | None = None, reason: str | None = None,
                  max_rows: int = 1000) -> list[dict]:
        """直接扫分区 Parquet，零 ETL。生产可加分区裁剪谓词下推。

        属源数据直读（采样预演/导出工具用）：须走 unsafe 留痕通道。
        """
        sql = f"SELECT * FROM read_parquet('{self.root / pattern}')"
        if extra_where:
            sql += f" WHERE {extra_where}"
        if operator is None:
            # 无具名操作者时按只读语义执行（read_parquet 表名在字面量内，不触拦截），
            # 但不享受审计留痕；生产调用方应显式传 operator/reason。
            return self._fetch(sql, params)
        return self.query(sql, params, unsafe=True, reason=reason or "cold_scan",
                          operator=operator, max_rows=max_rows)

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
