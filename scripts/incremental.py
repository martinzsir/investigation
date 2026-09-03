"""
scripts/incremental.py
季度增量入口（REQ-004 / REQ-005 / REQ-018）：

  新分区 parquet
    → 注册视图
    → REQ-005 分区校验（不合格隔离，退出码 2）
    → 装载入 L2 源表（INSERT INTO <dataset>）
    → 事件 source.partition.arrived
    → REQ-018 受影响范围计算
    → REQ-004 语义层增量物化（行级 diff，版本时钟推进）
    → 事件 ontology.materialized

退出码：
  0 = 成功（含空影响集跳过）
  1 = 分区文件缺失（PartitionMissingError；修复旧版静默跳过退出码 0 的问题）
  2 = 分区校验失败、已隔离

命名约定（REQ-004 AC6）：与 data/gen_sim 共用 {数据集}_{yyyy}Q{n}.parquet，
如 data/银行流水_2024Q4.parquet（--quarter 2024Q4）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import duckdb

DB = "investigation.duckdb"

# 银行流水期望列集（schema drift 检测用）
EXPECTED_COLUMNS = {"主体", "对方", "金额", "日期"}


class PartitionMissingError(FileNotFoundError):
    """分区文件不存在（REQ-004 AC3：退出码非 0，不得静默跳过）。"""


# 已应用分区台账（REQ-004 AC4：同分区同内容重复应用 → 跳过，不重复装载）
_APPLIED_DDL = """
CREATE TABLE IF NOT EXISTS meta_applied_partition (
    partition_id VARCHAR PRIMARY KEY,
    dataset VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    row_count BIGINT,
    high_watermark VARCHAR,
    build_id VARCHAR,
    applied_at VARCHAR NOT NULL
)
"""


def _already_applied(con, partition_id: str, content_hash: str) -> bool:
    con.execute(_APPLIED_DDL)
    row = con.execute(
        "SELECT content_hash FROM meta_applied_partition WHERE partition_id=?",
        [partition_id]).fetchone()
    return row is not None and row[0] == content_hash


def _mark_applied(con, part, build_id: str | None) -> None:
    from datetime import datetime
    con.execute(_APPLIED_DDL)
    con.execute(
        """INSERT OR REPLACE INTO meta_applied_partition
           (partition_id, dataset, content_hash, row_count, high_watermark,
            build_id, applied_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [part.partition_id, part.dataset, part.content_hash, part.row_count,
         part.high_watermark, build_id,
         datetime.now().isoformat(timespec="seconds")])


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_incremental(quarter: str, *, dataset: str = "银行流水",
                    db_path: str = DB, data_dir: str = "data",
                    expected_columns: set[str] | None = None) -> int:
    """返回退出码（0/1/2）。"""
    data_path = Path(data_dir)
    new_path = data_path / f"{dataset}_{quarter}.parquet"
    if not new_path.exists():
        raise PartitionMissingError(
            f"新分区不存在：{new_path}（命名约定 {{数据集}}_{{yyyy}}Q{{n}}.parquet）")

    con = duckdb.connect(db_path)
    view_name = f"{dataset}_{quarter}".replace("-", "_")
    try:
        # ---- 1) 注册分区视图（DDL 不能用 prepared parameter，路径转义后内联）----
        safe_path = str(new_path).replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * FROM read_parquet('{safe_path}')")

        # ---- 2) REQ-005 分区校验 ----
        from core.ingest_validate import IngestPartition, validate, quarantine
        part = IngestPartition(
            partition_id=view_name, dataset=dataset,
            high_watermark="", schema_version="", row_count=0,
            content_hash=_file_sha256(new_path))

        # 幂等（AC4）：同分区同内容已应用过 → 跳过，不重复装载、不重建
        if _already_applied(con, view_name, part.content_hash):
            print(f"[{quarter}] 分区 {view_name} 已应用（content_hash 一致），跳过")
            return 0

        errors = validate(part, con,
                          expected_columns=expected_columns or EXPECTED_COLUMNS,
                          pk_column=None)  # 银行流水无严格 PK
        if errors:
            result = quarantine(con, part, errors)
            if result.quarantined:
                print(f"[隔离] 分区 {view_name} 校验失败，已隔离：", file=sys.stderr)
                for e in errors:
                    print(f"  {e.code}: {e.detail}", file=sys.stderr)
                _publish_quarantine(con, part, errors)
                return 2

        # 校验通过后回填水标（漂移分区可能缺日期列，不能在校验前查）
        row = con.execute(
            f"SELECT COUNT(*), MAX(CAST(日期 AS VARCHAR)) FROM {view_name}").fetchone()
        part.row_count = row[0] or 0
        part.high_watermark = row[1] or ""

        # ---- 3) 装载入 L2 源表（列显式对齐，防 schema 漂移错位）----
        cols = ", ".join(f'"{c}"' for c in sorted(expected_columns or EXPECTED_COLUMNS))
        con.execute(
            f"INSERT INTO {dataset} ({cols}) "
            f"SELECT {cols} FROM {view_name}")

        # ---- 4) 事件总线 + 增量重建 ----
        from core.event_bus import EventBus
        from core.ontology import rebuild_from_partition
        bus = EventBus(con)
        bus.publish("source.partition.arrived", {
            "partition_id": view_name,
            "dataset": dataset,
            "row_count": part.row_count,
            "high_watermark": part.high_watermark,
            "content_hash": part.content_hash,
        }, actor="scripts.incremental")

        plan, stats = rebuild_from_partition(
            con, part, bus=bus, actor="scripts.incremental")

        # 登记已应用分区（幂等台账），含本次构建 build_id
        _mark_applied(con, part, stats.get("build_id"))

        if plan.mode == "skip" or stats.get("plan_mode") == "skip":
            print(f"[{quarter}] 无受影响对象，跳过重建（不产生物化事件）")
        else:
            print(f"[{quarter}] 增量完成：模式={plan.mode} "
                  f"重写行数={stats.get('rewritten_rows')} "
                  f"对象={list(stats.get('objects', {}))} "
                  f"链接={list(stats.get('links', {}))} "
                  f"受影响规则={plan.affected_rules}")
        return 0
    finally:
        con.close()


def _publish_quarantine(con, part, errors) -> None:
    try:
        from core.event_bus import EventBus
        bus = EventBus(con)
        bus.publish("partition.quarantined", {
            "partition_id": part.partition_id,
            "dataset": part.dataset,
            "errors": [{"code": e.code, "detail": e.detail,
                        "severity": e.severity} for e in errors],
        }, actor="scripts.incremental")
    except Exception:
        pass  # 事件失败不改变隔离结论


def main() -> int:
    ap = argparse.ArgumentParser(description="季度分区增量重建语义层")
    ap.add_argument("--quarter", default="2024Q4",
                    help="季度标识，如 2024Q4（文件名：银行流水_2024Q4.parquet）")
    ap.add_argument("--dataset", default="银行流水")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    try:
        return run_incremental(args.quarter, dataset=args.dataset,
                               db_path=args.db, data_dir=args.data_dir)
    except PartitionMissingError as e:
        print(f"[缺失] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
