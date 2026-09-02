"""
scripts/q2_overpass_cypher.py
第 2 步交付：Q2 过桥从 DuckDB SQL 改为 Cypher 多跳，并做双轨一致性比对。

流程：
  DuckDB 银行流水 → 建图(CSV 中转) → Cypher 两跳 MATCH ─┐
                                  └─ SQL 自连接 ────────┴→ 一致性比对 → 输出

用法：
    pip install ladybug
    python -m scripts.q2_overpass_cypher
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                    # noqa: E402
from core.graph import (                                  # noqa: E402
    GraphBackend, overpass_two_hop_sql, compare_engines,
)

OUT = ROOT / "output" / "q2_overpass_cypher.json"


def main() -> int:
    store = Store()
    store.execute(
        "CREATE OR REPLACE TABLE 银行流水 AS "
        "SELECT * FROM read_parquet('data/银行流水.parquet')"
    )
    n_rows = store.query("SELECT COUNT(*) AS c FROM 银行流水")[0]["c"]
    print(f"数据：银行流水 {n_rows} 行")

    g = GraphBackend("data/ladybug/investigation.lbug")
    if not g.available:
        print("❌ 未安装 ladybug，请先：pip install ladybug")
        return 1

    # 1. 建图
    stat = g.build_from_duckdb(store)
    print(f"建图：节点 {stat['nodes']} 个，边 {stat['edges']} 条")

    # 2. Cypher 两跳过桥
    cypher_paths = g.overpass_two_hop()
    print(f"\n=== Q2 过桥 · Cypher 两跳（{len(cypher_paths)} 条）===")
    for p in cypher_paths:
        print(f"  {p.source} → {p.bridge} → {p.dest}  "
              f"({p.amount_in:,.0f} → {p.amount_out:,.0f})")

    # 3. SQL 自连接对照
    sql_paths = overpass_two_hop_sql(store)
    print(f"\n=== Q2 过桥 · SQL 自连接（{len(sql_paths)} 条）===")
    for p in sql_paths:
        print(f"  {p.source} → {p.bridge} → {p.dest}  "
              f"({p.amount_in:,.0f} → {p.amount_out:,.0f})")

    # 4. 一致性比对
    cmp_res = compare_engines(cypher_paths, sql_paths)
    print(f"\n=== 双轨一致性比对 ===")
    print(f"  Cypher {cmp_res['cypher_count']} 条 / SQL {cmp_res['sql_count']} 条 "
          f"/ 匹配 {cmp_res['matched']} 条")
    print(f"  仅 Cypher 有：{cmp_res['only_in_cypher'] or '无'}")
    print(f"  仅 SQL 有：{cmp_res['only_in_sql'] or '无'}")
    print(f"  结论：{'✅ 双轨一致' if cmp_res['consistent'] else '⚠ 存在差异，须正兵复核'}")

    # 5. 奇兵拓线：变长跳邻域
    print(f"\n=== 奇兵拓线 · 2 跳邻域 ===")
    for subj in ["宏业建设", "张卫国"]:
        nb = g.neighbors_within(subj, max_hops=2)
        print(f"  {subj} → {nb}")

    # 落盘
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "graph_build": stat,
        "cypher_paths": [p.to_dict() for p in cypher_paths],
        "sql_paths": [p.to_dict() for p in sql_paths],
        "comparison": cmp_res,
        "redline": "AI 不出定性；路径仅为候选，须正兵固证 + 法定程序",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 已导出：{OUT}")

    g.close()
    store.close()
    return 0 if cmp_res["consistent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
