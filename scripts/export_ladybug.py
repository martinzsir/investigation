"""
scripts/export_ladybug.py
从 DuckDB 预聚合结果物化 LadybugDB 边表（转账/过桥关系）。
LadybugDB 可通过 ATTACH DuckDB 直接读，这里演示导出 Cypher-ready CSV。
"""

from pathlib import Path
import duckdb

DB = "investigation.duckdb"
OUT = Path("data/ladybug")
OUT.mkdir(exist_ok=True)


def main():
    con = duckdb.connect(DB)
    # 转账边（主体 → 对方，带金额/日期）
    con.execute(f"""
        COPY (
            SELECT 主体 AS from_id, 对方 AS to_id, 金额, 日期
            FROM read_parquet('data/银行流水.parquet')
            WHERE CAST(金额 AS BIGINT) % 10000 = 0
        ) TO '{OUT / 'transfer_edges.csv'}' (HEADER, DELIMITER ',')
    """)
    # 过桥两跳路径（对应 Q2，Cypher 可直接 MATCH）
    con.execute(f"""
        COPY (
            SELECT a.主体 AS hop1, a.对方 AS bridge, b.对方 AS hop2, a.金额
            FROM read_parquet('data/银行流水.parquet') a
            JOIN read_parquet('data/银行流水.parquet') b
              ON a.对方 = b.主体 AND a.主体 <> b.对方
            WHERE CAST(a.金额 AS BIGINT) % 10000 = 0 AND CAST(b.金额 AS BIGINT) % 10000 = 0
            LIMIT 1000
        ) TO '{OUT / 'overpass_paths.csv'}' (HEADER, DELIMITER ',')
    """)
    print("LadybugDB 边表已导出：", OUT)
    con.close()


if __name__ == "__main__":
    main()
