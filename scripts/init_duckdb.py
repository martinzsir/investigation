"""
scripts/init_duckdb.py
初始化 L2（DuckDB 单文件）：
- 建预聚合表 agg_subject_month（主体×月）
- 建物化视图 mv_quarterly_integer_deposits
- 从 L3 Parquet 分区加载数据，零 ETL
替代原 StarRocks 温层的全部职责。
"""

from pathlib import Path
import duckdb

DB = "investigation.duckdb"
DATA = Path(__file__).parent.parent / "data"


def main():
    con = duckdb.connect(DB)
    # 建冷层视图（直接挂 Parquet，等同 StarRocks 外表）
    # 视图会把路径持久化进库文件——必须用相对路径（相对进程 CWD=项目根），
    # 否则 Windows/WSL 两侧只有建库那侧能读（export_ladybug.py 同款风格）。
    con.execute("CREATE OR REPLACE VIEW v_flow AS SELECT * FROM read_parquet('data/银行流水.parquet')")
    con.execute("CREATE OR REPLACE VIEW v_calls AS SELECT * FROM read_parquet('data/通话记录.parquet')")

    # 业务表：技能与实体对齐都以「业务表名」为契约（银行流水/工商信息/...），
    # 此处把 L3 Parquet 统一挂成同名表。
    # 用 CTAS 实体表而非视图：apply_org_to_duckdb 需要 ALTER TABLE 加 canonical_org_* 列，
    # 视图只能走 ALTER VIEW，无法承载写入。
    # 注：2000 亿行场景应改为「视图 + 旁路映射表 join」避免物化开销，此处演示数据直接 CTAS。
    for name in ["银行流水", "通话记录", "招投标档案", "工商信息", "轨迹出行"]:
        p = DATA / f"{name}.parquet"
        if not p.exists():
            continue
        con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM read_parquet(\'{p.as_posix()}\')')

    # ── 后期接入型数据源：缺文件时按约定 schema 建空表（保证语义层编译不中断），
    #    数据到达后重跑 init_duckdb 即可自动替换；旧版本「仅 内容」列 parquet 向后兼容。 ──
    _FALLBACK_SCHEMAS = {
        "公开OSINT": """CREATE OR REPLACE TABLE "公开OSINT" (
            主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR
        )""",
        "举报材料": """CREATE OR REPLACE TABLE "举报材料" (
            举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR
        )""",
    }
    for name, ddl in _FALLBACK_SCHEMAS.items():
        p = DATA / f"{name}.parquet"
        if p.exists():
            # 真实数据优先，但列可能少于 schema（向后兼容单列表）：
            # 先用 parquet 列建表，再 ALTER 补齐缺失列（不影响已存数据），
            # 这样「内容」旧版本 parquet 也能兼容。
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM read_parquet(\'{p.as_posix()}\')')
            # 拿实际列集：DESCRIBE 列顺序 (column_name, column_type, null, key, default, extra)
            actual = {row[0] for row in con.execute(f'DESCRIBE "{name}"').fetchall()}
            expected_types = {
                "公开OSINT": {"主体":"VARCHAR","公开信息":"VARCHAR","发布日期":"DATE","来源":"VARCHAR",
                              "采集时间":"TIMESTAMP","保留天数":"INTEGER"},
                "举报材料": {"举报日期":"DATE","分类":"VARCHAR","被举报人":"VARCHAR","举报人":"VARCHAR","内容":"VARCHAR"},
            }[name]
            for col, typ in expected_types.items():
                if col not in actual:
                    con.execute(f'ALTER TABLE "{name}" ADD COLUMN "{col}" {typ}')
        else:
            con.execute(ddl)

    # 预聚合表：主体×月（温层核心，替代 StarRocks mv）
    con.execute("""
        CREATE OR REPLACE TABLE agg_subject_month AS
        SELECT 主体, date_trunc('month', 日期::DATE) AS ym, COUNT(*) AS cnt, SUM(金额) AS total
        FROM v_flow
        GROUP BY 主体, date_trunc('month', 日期::DATE)
    """)

    # 物化视图：季度整数存入（侦查模式库）
    con.execute("""
        CREATE OR REPLACE VIEW mv_quarterly_integer_deposits AS
        SELECT 主体, date_trunc('quarter', 日期::DATE) AS q, COUNT(*) AS cnt, SUM(金额) AS total
        FROM v_flow
        WHERE CAST(金额 AS BIGINT) % 10000 = 0
        GROUP BY 主体, date_trunc('quarter', 日期::DATE)
    """)

    # Q1 时间窗碰撞明细：中标公示日 ±20 天 vs 整数现金存入（物化为 L2 表，供奇兵复用）
    con.execute(f"""
        CREATE OR REPLACE TABLE Q1_time_window AS
        SELECT b.项目 AS 项目, b.中标公示日::DATE AS bid_date, f.日期::DATE AS deposit_date,
               date_diff('day', b.中标公示日::DATE, f.日期::DATE) AS offset_days,
               CAST(f.金额 AS BIGINT) AS amount
        FROM read_parquet('{(DATA / '招投标档案.parquet').as_posix()}') b
        JOIN read_parquet('{(DATA / '银行流水.parquet').as_posix()}') f
          ON f.主体 = '张卫国' AND CAST(f.金额 AS BIGINT) % 10000 = 0
         AND f.日期::DATE BETWEEN b.中标公示日::DATE - 20 AND b.中标公示日::DATE + 20
        ORDER BY offset_days
    """)
    con.execute(f"COPY Q1_time_window TO '{(DATA / 'Q1_time_window.parquet').as_posix()}' (FORMAT 'parquet')")

    print("L2 DuckDB 初始化完成：", DB)
    print("预聚合行数：", con.execute("SELECT COUNT(*) FROM agg_subject_month").fetchone()[0])
    print("Q1 时间窗碰撞条数：", con.execute("SELECT COUNT(*) FROM Q1_time_window").fetchone()[0])
    print("季度整数存入：")
    for r in con.execute("SELECT * FROM mv_quarterly_integer_deposits ORDER BY q").fetchall():
        print("  ", r)
    con.close()


if __name__ == "__main__":
    main()
