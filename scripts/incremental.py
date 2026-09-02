"""
scripts/incremental.py
季度增量：只扫新分区（如 银行流水_2024Q4.parquet），不重扫全量。
替代原 Flink 的职责；单机用 cron / 定时任务驱动即可。
"""

import argparse
from pathlib import Path
import duckdb

DB = "investigation.duckdb"


def main(quarter: str):
    new_path = Path("data") / f"银行流水_{quarter}.parquet"
    if not new_path.exists():
        print(f"[跳过] 新分区不存在：{new_path}")
        return

    con = duckdb.connect(DB)
    # 只算新增分区的预聚合，INSERT 进 L2（增量更新，分钟级）
    con.execute("""
        INSERT INTO agg_subject_month
        SELECT 主体, date_trunc('month', 日期::DATE), COUNT(*), SUM(金额)
        FROM read_parquet(?)
        GROUP BY 1, 2
    """, (str(new_path),))
    new_features = con.execute("""
        SELECT 主体, '整数存入', COUNT(*), SUM(金额)
        FROM read_parquet(?)
        WHERE CAST(金额 AS BIGINT) % 10000 = 0
        GROUP BY 主体
    """, (str(new_path),)).fetchall()
    print(f"[{quarter}] 增量完成，新增预聚合，新特征：", new_features)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", default="2024-Q4")
    args = ap.parse_args()
    main(args.quarter)
