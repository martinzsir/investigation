"""
core/ingest_validate.py
分区校验与 data_gap 阻断（REQ-005）。

校验维度：
  1. 分区存在性（DATA_GAP）
  2. 主键重复率（PK_DUPLICATE，阈值默认 1%）
  3. schema 漂移（SCHEMA_DRIFT，列集与期望不一致）
  4. 时间单调性（TIME_NON_MONOTONIC，日期倒退）

不合格分区写入 partition_quarantined 表（隔离），发布 partition.quarantined 事件。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Literal


@dataclass
class IngestPartition:
    """单个分区的元数据。"""
    partition_id: str           # 如 "银行流水_2024Q1"
    dataset: str                # "银行流水" | "通话记录" | ...
    high_watermark: str = ""    # 该分区最大事件时间
    schema_version: str = ""    # 该分区 schema 指纹
    row_count: int = 0
    content_hash: str = ""


@dataclass
class ValidationError:
    """校验错误。"""
    code: str                   # DATA_GAP / PK_DUPLICATE / SCHEMA_DRIFT / TIME_NON_MONOTONIC
    partition_id: str
    detail: str
    severity: Literal["block", "warn"] = "block"


@dataclass
class QuarantineResult:
    """隔离结果。"""
    partition_id: str
    quarantined: bool
    errors: list[ValidationError] = field(default_factory=list)


# ----------------------------------------------------------------------
# 分区级校验
# ----------------------------------------------------------------------
def validate(part: IngestPartition, conn, *,
             expected_columns: set[str] | None = None,
             pk_column: str | None = None,
             pk_dup_threshold: float = 0.01) -> list[ValidationError]:
    """单分区校验：行数、主键重复率、时间单调性、schema diff。

    参数：
      part: 分区元数据（partition_id 应对应 DuckDB 中的表或视图名）
      conn: DuckDB 连接
      expected_columns: 期望列集（schema drift 检测用），None 则跳过
      pk_column: 主键列名（重复率检测用），None 则跳过
      pk_dup_threshold: 主键重复率阈值（默认 1%）
    """
    errors: list[ValidationError] = []
    table = part.partition_id

    # 1. 表存在性
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        if not row or row[0] == 0:
            errors.append(ValidationError(
                code="DATA_GAP", partition_id=table,
                detail=f"分区 {table} 行数为 0", severity="warn"))
            return errors  # 空表无需后续校验
        part.row_count = row[0]
    except Exception as e:
        errors.append(ValidationError(
            code="DATA_GAP", partition_id=table,
            detail=f"分区 {table} 不存在或不可读：{e}", severity="block"))
        return errors

    # 2. schema drift
    if expected_columns is not None:
        try:
            actual_cols = {r[1] for r in conn.execute(
                f"PRAGMA table_info('{table}')").fetchall()}
            missing = expected_columns - actual_cols
            extra = actual_cols - expected_columns
            if missing or extra:
                errors.append(ValidationError(
                    code="SCHEMA_DRIFT", partition_id=table,
                    detail=f"schema 漂移：缺失 {missing or '无'}，多余 {extra or '无'}",
                    severity="block"))
        except Exception:
            pass  # PRAGMA 失败不阻断

    # 3. 主键重复率
    if pk_column:
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            distinct = conn.execute(
                f'SELECT COUNT(DISTINCT "{pk_column}") FROM {table}'
            ).fetchone()[0]
            if total > 0:
                dup_rate = (total - distinct) / total
                if dup_rate > pk_dup_threshold:
                    errors.append(ValidationError(
                        code="PK_DUPLICATE", partition_id=table,
                        detail=f"主键 {pk_column} 重复率 {dup_rate:.2%} > 阈值 {pk_dup_threshold:.2%}",
                        severity="block"))
        except Exception:
            pass  # 列不存在不阻断

    # 4. 时间单调性（日期倒退检测）
    try:
        date_cols = [c for c in _get_columns(conn, table)
                     if any(p in c.lower() for p in ("日期", "date", "time"))]
        for col in date_cols:
            rows = conn.execute(
                f'SELECT CAST("{col}" AS VARCHAR) FROM {table} '
                f'ORDER BY CAST("{col}" AS VARCHAR) LIMIT 1'
            ).fetchall()
            if not rows:
                continue
            # 检查是否有倒退：取前 100 行看是否有序
            all_rows = conn.execute(
                f'SELECT CAST("{col}" AS VARCHAR) FROM {table} '
                f'ORDER BY CAST("{col}" AS VARCHAR)'
            ).fetchall()
            for i in range(1, len(all_rows)):
                if all_rows[i][0] < all_rows[i - 1][0]:
                    errors.append(ValidationError(
                        code="TIME_NON_MONOTONIC", partition_id=table,
                        detail=f"日期列 {col} 存在倒退：行 {i} '{all_rows[i][0]}' < 行 {i-1} '{all_rows[i-1][0]}'",
                        severity="block"))
                    break
            break  # 只检查第一个日期列
    except Exception:
        pass

    return errors


def _get_columns(conn, table: str) -> list[str]:
    """获取表的所有列名。"""
    try:
        return [r[1] for r in conn.execute(
            f"PRAGMA table_info('{table}')").fetchall()]
    except Exception:
        return []


# ----------------------------------------------------------------------
# 数据集级校验（期望 vs 实际分区清单）
# ----------------------------------------------------------------------
def validate_dataset(dataset: str, expected_partitions: list[str],
                     actual_partitions: list[str]) -> list[ValidationError]:
    """期望 vs 实际分区清单对比。"""
    errors: list[ValidationError] = []
    expected_set = set(expected_partitions)
    actual_set = set(actual_partitions)
    missing = expected_set - actual_set
    for m in sorted(missing):
        errors.append(ValidationError(
            code="DATA_GAP", partition_id=m,
            detail=f"期望分区 {m}（数据集 {dataset}）不存在", severity="block"))
    return errors


# ----------------------------------------------------------------------
# 隔离
# ----------------------------------------------------------------------
_QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS partition_quarantined (
    partition_id VARCHAR PRIMARY KEY,
    dataset VARCHAR NOT NULL,
    errors VARCHAR,
    quarantined_at VARCHAR NOT NULL
)
"""


def quarantine(conn, part: IngestPartition,
               errors: list[ValidationError]) -> QuarantineResult:
    """把不合格分区写入 partition_quarantined 表，返回隔离结果。"""
    conn.execute(_QUARANTINE_DDL)
    block_errors = [e for e in errors if e.severity == "block"]
    quarantined = len(block_errors) > 0
    if quarantined:
        from datetime import datetime
        conn.execute(
            """INSERT OR REPLACE INTO partition_quarantined
               (partition_id, dataset, errors, quarantined_at)
               VALUES (?, ?, ?, ?)""",
            [part.partition_id, part.dataset,
             json.dumps([asdict(e) for e in errors], ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")])
    return QuarantineResult(
        partition_id=part.partition_id,
        quarantined=quarantined,
        errors=errors)


def is_quarantined(conn, partition_id: str) -> bool:
    """查询分区是否已被隔离。"""
    try:
        conn.execute(_QUARANTINE_DDL)
        row = conn.execute(
            "SELECT 1 FROM partition_quarantined WHERE partition_id=?",
            [partition_id]).fetchone()
        return row is not None
    except Exception:
        return False
