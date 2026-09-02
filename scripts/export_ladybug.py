"""
scripts/export_ladybug.py
从语义层（obj_*/lnk_*）物化 LadybugDB 图谱 CSV（v2 Ontology 化）。

数据面变更：不再直读 data/*.parquet——节点来自 obj_*，边来自 lnk_*；
换数据源只改 core/ontology.py 声明，本脚本零改动。
语义层未构建时报错退出，提示先跑 python -m scripts.build_ontology。

产物（data/ladybug/）：
  nodes.csv              全部节点（name, type）：person/org/account/bid_project
  transfer_edges.csv     转账边（lnk_transfers，兼容旧字段名 from_id/to_id）
  overpass_paths.csv     过桥两跳路径（lnk_transfers 自连接，兼容旧字段名）
  calls_edges.csv        通话边（lnk_calls_to，代理键回连 obj_person 取 raw_name）
  co_located_edges.csv   同框边（lnk_co_located）
  owns_edges.csv         持有账户边（lnk_owns）
  involved_in_edges.csv  中标参与边（lnk_involved_in）
  time_window_edges.csv  时间窗碰撞边（lnk_time_window）

LadybugDB 可通过 ATTACH DuckDB 直接读（WSL/Linux 可用）；Windows 原生扩展为
坏二进制不可用，走 CSV 中转。COPY 路径一律正斜杠（反斜杠会被当转义序列）。
"""
from __future__ import annotations

from pathlib import Path

from core import Store

OUT = Path("data/ladybug")


def _exists(store: Store, table: str) -> bool:
    return store.query(
        "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_name = ?",
        (table,),
    )[0]["n"] > 0


def _copy(store: Store, sql: str, fname: str) -> int:
    """执行 COPY 导出 CSV（正斜杠路径），返回导出行数。"""
    path = (OUT / fname).as_posix()
    store.execute(f"COPY ({sql}) TO '{path}' (HEADER, DELIMITER ',')")
    return store.query(f"SELECT COUNT(*) AS n FROM ({sql})")[0]["n"]


def main():
    store = Store()
    missing = [t for t in ("obj_person", "obj_transaction", "lnk_transfers")
               if not _exists(store, t)]
    if missing:
        raise SystemExit(f"语义层缺失 {missing}：先跑 python -m scripts.build_ontology")

    OUT.mkdir(exist_ok=True)
    exported: dict[str, int] = {}

    # ---- 节点：person ∪ org ∪ account ∪ bid_project（raw_name/title 即图上名称）----
    exported["nodes.csv"] = _copy(store, """
        SELECT raw_name AS name, 'person' AS type FROM obj_person
        UNION ALL
        SELECT raw_name, 'org' FROM obj_org
        UNION ALL
        SELECT raw_name, 'account' FROM obj_account
        UNION ALL
        SELECT title, 'bid_project' FROM obj_bid_project
    """, "nodes.csv")

    # ---- 边：转账（lnk_transfers 的 from_account/to_account 即 raw_name）----
    exported["transfer_edges.csv"] = _copy(store, """
        SELECT from_account AS from_id, to_account AS to_id,
               CAST(amount AS DOUBLE) AS amount, date
        FROM lnk_transfers
    """, "transfer_edges.csv")

    # ---- 边：过桥两跳路径（Cypher MATCH 的 SQL 对照产物，兼容旧字段名）----
    exported["overpass_paths.csv"] = _copy(store, """
        SELECT a.from_account AS hop1, a.to_account AS bridge,
               b.to_account AS hop2, CAST(a.amount AS DOUBLE) AS amount
        FROM lnk_transfers a
        JOIN lnk_transfers b
          ON a.to_account = b.from_account AND a.from_account <> b.to_account
        LIMIT 1000
    """, "overpass_paths.csv")

    # ---- 边：通话（代理键回连 obj_person，保证节点引用一致）----
    if _exists(store, "lnk_calls_to"):
        exported["calls_edges.csv"] = _copy(store, """
            SELECT p1.raw_name AS from_id, p2.raw_name AS to_id, c.call_id
            FROM lnk_calls_to c
            JOIN obj_person p1 ON p1.person_id = c.from_person
            JOIN obj_person p2 ON p2.person_id = c.to_person
        """, "calls_edges.csv")

    # ---- 边：轨迹同框 ----
    if _exists(store, "lnk_co_located"):
        exported["co_located_edges.csv"] = _copy(store, """
            SELECT p1.raw_name AS from_id, p2.raw_name AS to_id,
                   l.location, l.date
            FROM lnk_co_located l
            JOIN obj_person p1 ON p1.person_id = l.person_1
            JOIN obj_person p2 ON p2.person_id = l.person_2
        """, "co_located_edges.csv")

    # ---- 边：持有账户（owner_raw 由链接声明直接携带）----
    if _exists(store, "lnk_owns"):
        exported["owns_edges.csv"] = _copy(store, """
            SELECT w.owner_raw AS from_id, a.raw_name AS to_id
            FROM lnk_owns w
            JOIN obj_account a ON a.account_id = w.account_id
        """, "owns_edges.csv")

    # ---- 边：中标参与（org → project）----
    if _exists(store, "lnk_involved_in"):
        exported["involved_in_edges.csv"] = _copy(store, """
            SELECT o.raw_name AS from_id, b.title AS to_id
            FROM lnk_involved_in i
            JOIN obj_org o ON o.org_id = i.org_id
            JOIN obj_bid_project b ON b.project_id = i.project_id
        """, "involved_in_edges.csv")

    # ---- 边：时间窗碰撞（bid_project → 资金主体，声明已携带 title/owner_raw）----
    if _exists(store, "lnk_time_window"):
        exported["time_window_edges.csv"] = _copy(store, """
            SELECT title AS from_id, owner_raw AS to_id, offset_days
            FROM lnk_time_window
        """, "time_window_edges.csv")

    print("LadybugDB 图谱 CSV 已从语义层导出：", OUT)
    for fname, n in exported.items():
        print(f"  {fname:<24} {n} 行")


if __name__ == "__main__":
    main()
