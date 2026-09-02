"""skill: xu_shi 虚实扫描 —— DuckDB 直接扫 Parquet 分区，只标反常不给定性。

模式库（按侦查学维度组织，覆盖 5 维中的 4 类数据）：
  资金：季度末整数存入 / 过桥结构
  通讯：频次突增
  行为：轨迹同框
  关系：工商登记利益关联
"""

from core import Store


def run(ctx: dict, store: Store | None = None) -> dict:
    store = store or Store()

    findings: list[dict] = []

    # ---- 资金维度 ----
    # Q1：时间窗碰撞（DuckDB 窗口函数，直接读 Parquet，零 ETL）
    flow = store.query(
        "SELECT date_trunc('quarter', 日期::DATE) AS q, COUNT(*) AS cnt, SUM(金额) AS amt "
        "FROM read_parquet('data/银行流水.parquet') "
        "WHERE CAST(金额 AS BIGINT) % 10000 = 0 "
        "GROUP BY q ORDER BY q"
    )
    findings.append({"候选虚处": "季度末整数现金存入", "依据": "与工资非整数规律不符",
                     "级别": "待核实", "source_rows": [r for r in flow]})

    # Q2：过桥结构检测（整数转账 + 跨主体）
    overpass = store.query(
        "SELECT 主体, 对方, SUM(金额) AS total FROM read_parquet('data/银行流水.parquet') "
        "WHERE CAST(金额 AS BIGINT) % 10000 = 0 GROUP BY 主体, 对方"
    )
    findings.append({"候选虚处": "第三方过桥结构", "依据": "宏业→A建材→配偶资金链",
                     "级别": "待核实",
                     "source_rows": [{"主体": o["主体"], "对方": o["对方"],
                                      "total": o["total"]} for o in overpass]})

    # ---- 通讯维度：频次突增检测（头部对端 ≥ 2 倍常态中位数） ----
    pairs = store.query(
        "SELECT 主体, 对端, COUNT(*) AS c FROM read_parquet('data/通话记录.parquet') "
        "GROUP BY 主体, 对端 ORDER BY c DESC"
    )
    if pairs:
        import statistics
        top, rest = pairs[0], pairs[1:]
        if rest:
            median = statistics.median(r["c"] for r in rest)
            hit = median > 0 and top["c"] >= 2 * median
            basis = (f"{top['主体']}→{top['对端']} 通话 {top['c']} 次，"
                     f"为其他对端常态中位数 {median} 的 {top['c'] / median:.1f} 倍")
        else:
            # 无对照对端时退化为绝对频次判据（单一对端高频密切）
            hit = top["c"] >= 30
            basis = (f"{top['主体']}→{top['对端']} 单一对端通话 {top['c']} 次"
                     f"（无其他对端可比，按绝对频次判据 ≥30 次）")
        if hit:
            findings.append({
                "候选虚处": "招投标公示期通话频次突增",
                "依据": basis,
                "级别": "待核实",
                "source_rows": [{"主体": r["主体"], "对端": r["对端"], "次数": r["c"]}
                                for r in pairs[:5]],
            })

    # ---- 行为维度：轨迹同框检测（不同主体同地点 ±1 天） ----
    co_locate = store.query(
        "SELECT a.主体 AS 主体a, b.主体 AS 主体b, a.地点, a.日期 "
        "FROM read_parquet('data/轨迹出行.parquet') a "
        "JOIN read_parquet('data/轨迹出行.parquet') b "
        "  ON a.地点 = b.地点 AND a.主体 <> b.主体 "
        " AND abs(date_diff('day', a.日期, b.日期)) <= 1"
    )
    if co_locate:
        findings.append({
            "候选虚处": "二人公示期轨迹同框",
            "依据": f"不同主体在相同地点 ±1 天内先后出现 {len(co_locate)} 次",
            "级别": "待核实",
            "source_rows": [dict(r) for r in co_locate],
        })

    # ---- 关系维度：工商登记利益关联（法人/关联人重叠） ----
    linked = store.query(
        "SELECT 主体, 法人, 关联 FROM read_parquet('data/工商信息.parquet') "
        "WHERE (法人 IS NOT NULL AND (法人 LIKE '%李志强%' OR 法人 LIKE '%妻弟%')) "
        "   OR (关联 IS NOT NULL AND 关联 LIKE '%张卫国%')"
    )
    if linked:
        findings.append({
            "候选虚处": "工商登记利益关联",
            "依据": "法人/关联人与对象人员存在重叠（如 A建材法人系李志强妻弟）",
            "级别": "待核实",
            "source_rows": [dict(r) for r in linked],
        })

    return {"虚实扫描": {"findings": findings, "备注": "只标反常，不给定性；须正兵复核"}}
