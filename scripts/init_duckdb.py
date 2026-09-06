"""
scripts/init_duckdb.py
初始化 L2（DuckDB 单文件）：
- 建预聚合表 agg_subject_month（主体×月）
- 建物化视图 mv_quarterly_integer_deposits
- 从 L3 Parquet 分区加载数据，零 ETL
替代原 StarRocks 温层的全部职责。

REQ-G-014：冷层业务表不再硬编码，改由 ontology 案件包推导——
物理表名取 bindings.object_bindings[].source.table，列名取 source.columns 的源列，
列类型由 objects.json 属性值类型映射（与语义层 TYPE_SQL 同口径）。
预聚合表依赖具体业务列，属温层优化，保持手工。
**红线 AC4**：本脚本不 import core（避免冷层→语义层循环依赖），类型映射为独立最小常量。
"""

import json
import re
from pathlib import Path
import duckdb

DB = "investigation.duckdb"
DATA = Path(__file__).parent.parent / "data"
ONTOLOGY = Path(__file__).parent.parent / "ontology" / "default"

# 与 core.ontology.TYPE_SQL 同口径的独立最小映射（脚本层不得 import core，AC4）
_TYPE_SQL = {
    "string": "VARCHAR", "integer": "BIGINT", "decimal": "DOUBLE",
    "date": "DATE", "boolean": "BOOLEAN", "timestamp": "TIMESTAMP",
    "duration_days": "INTEGER", "enum": "VARCHAR",
}
_CJK = re.compile(r"[一-鿿]")


def _derived_cold_tables(ontology_dir: Path = ONTOLOGY) -> dict[str, dict[str, str]]:
    """从 objects.json + bindings.json 推导冷层业务表 {表名: {源列: 列类型}}。

    仅取结构化 source 绑定、且表名含中文的业务冷层表（跳过 core 自建的 ASCII 蛇形
    内部表，如 clue_disposal_status——其建表/主键/ON CONFLICT 由 core.lineage 负责）。
    同一表被多个对象绑定时取列并集。
    """
    objects = json.loads((ontology_dir / "objects.json").read_text(encoding="utf-8"))
    bindings = json.loads((ontology_dir / "bindings.json").read_text(encoding="utf-8"))
    prop_types = {o["name"]: o.get("properties", {})
                  for o in objects.get("objects", [])}
    tables: dict[str, dict[str, str]] = {}
    for b in bindings.get("object_bindings", []):
        src = b.get("source")
        if not src:
            continue
        table = src.get("table")
        if not table or not _CJK.search(str(table)):
            continue  # 仅业务冷层表；core 内部表（ASCII 蛇形）不在此建
        oprops = prop_types.get(b.get("object"), {})
        cols = tables.setdefault(table, {})
        for alias, raw_col in (src.get("columns") or {}).items():
            cols.setdefault(raw_col, _TYPE_SQL.get(_base_type(oprops.get(alias, "string")),
                                                   "VARCHAR"))
    return tables


def _base_type(decl) -> str:
    """属性声明可为 string 或映射 {type|composite|data_element}（REQ-D-013/002）。"""
    if isinstance(decl, dict):
        return str(decl.get("type", "string"))
    return str(decl)


def _pack_dirs() -> list[tuple[str, Path]]:
    """全部案件包 [(包名, ontology/<包> 目录)]；default 在前。"""
    out = [("default", ONTOLOGY)]
    for d in sorted(ONTOLOGY.parent.iterdir()):
        if d.is_dir() and d.name != "default" and (d / "bindings.json").exists():
            out.append((d.name, d))
    return out


def main():
    con = duckdb.connect(DB)
    # 建冷层视图（直接挂 Parquet，等同 StarRocks 外表）
    # 视图会把路径持久化进库文件——必须用相对路径（相对进程 CWD=项目根），
    # 否则 Windows/WSL 两侧只有建库那侧能读（export_ladybug.py 同款风格）。
    con.execute("CREATE OR REPLACE VIEW v_flow AS SELECT * FROM read_parquet('data/银行流水.parquet')")
    con.execute("CREATE OR REPLACE VIEW v_calls AS SELECT * FROM read_parquet('data/通话记录.parquet')")

    # 业务表（REQ-G-014 声明推导）：表名/列名/列类型均来自 ontology 案件包。
    # 用 CTAS 实体表而非视图：apply_org_to_duckdb 需要 ALTER TABLE 加 canonical_org_* 列，
    # 视图只能走 ALTER VIEW，无法承载写入。
    # - Parquet 存在：CTAS 真实数据，再 ALTER 补齐声明但 parquet 缺的列（旧版单列向后兼容）；
    # - Parquet 缺失：按推导出的列类型建空表（新数据源仅在 bindings 声明即可建表，AC1）。
    # 多案件包（REQ-D 业务测试案例）：default 包数据在 data/ 根，其他包在 data/<包名>/。
    for pack_name, onto_dir in _pack_dirs():
        data_dir = DATA if pack_name == "default" else DATA / pack_name
        for name, col_types in _derived_cold_tables(onto_dir).items():
            p = data_dir / f"{name}.parquet"
            if p.exists():
                con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM read_parquet(\'{p.as_posix()}\')')
                actual = {row[0] for row in con.execute(f'DESCRIBE "{name}"').fetchall()}
                for col, typ in col_types.items():
                    if col not in actual:
                        con.execute(f'ALTER TABLE "{name}" ADD COLUMN "{col}" {typ}')
            else:
                cols_ddl = ", ".join(f'"{c}" {t}' for c, t in col_types.items())
                con.execute(f'CREATE OR REPLACE TABLE "{name}" ({cols_ddl})')

    # 预聚合表：主体×月（温层核心，替代 StarRocks mv，保持手工——依赖具体业务列）
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
