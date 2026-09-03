"""
scripts/export_ladybug.py
从语义层（obj_*/lnk_*）物化 LadybugDB 图谱 CSV（v2 Ontology 化）。

数据面变更：不再直读 data/*.parquet——节点来自 obj_*，边来自 lnk_*；
换数据源只改 core/ontology.py 声明，本脚本零改动。
语义层未构建时报错退出，提示先跑 python -m scripts.build_ontology。

权限与审计（REQ-011，P0 出口同源执行策略）：
  - 会话身份经命令行声明（--operator 必填；缺省 --operator system 全旁路，
    仅限本机内部使用），角色/等级经 --role/--clearance；
  - 导出前按 ontology/<pack>/policies.json 逐对象做对象级策略检查
    （fail-closed），越权即拒绝（AC5：落审计 + stderr 告警 + 退出码 2）；
  - 敏感属性列（property_policies default=deny 且无权）在导出 SQL 前
    按声明剔除/遮蔽（AC2）；本次导出列均为标识/关系列，不含敏感属性，
    _masked_columns 机制保证未来新增敏感列时自动防护；
  - 导出完成后落审计链 audit_chain：operator / purpose / destination /
    文件清单（AC3）。

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

import argparse
import sys
from pathlib import Path

from core import Store
from core.access import AccessContext, system_context
from core.policy import PolicyDeniedError, PolicyEngine

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


def _masked_columns(engine: PolicyEngine, ctx: AccessContext,
                    obj: str, columns: list[str]) -> list[str]:
    """返回该对象在当前会话下将被剔除/遮蔽的敏感列（AC2 防护清单）。"""
    return [p for p in columns
            if engine.property_rule(obj, p) and not engine.can_read_property(ctx, obj, p)]


def main(store: Store | None = None) -> int:
    ap = argparse.ArgumentParser(description="语义层 → LadybugDB 图谱 CSV 导出")
    ap.add_argument("--operator", default="system",
                    help="导出操作者（system=内部旁路，仅限本机）")
    ap.add_argument("--role", default="system", help="角色（见习/正兵/偏将/主办/human）")
    ap.add_argument("--clearance", type=int, default=99, help="权限等级")
    ap.add_argument("--purpose", default="", help="导出目的（落审计）")
    ap.add_argument("--destination", default="local-csv:data/ladybug",
                    help="目的地标签（落审计，如 airgap-usb/内网FTP）")
    args = ap.parse_args()

    ctx = (system_context() if args.operator == "system"
           else AccessContext(operator=args.operator, role=args.role,
                              clearance=args.clearance, purpose=args.purpose))
    engine = PolicyEngine()
    own_store = store is None
    store = store if store is not None else Store()
    missing = [t for t in ("obj_person", "obj_transaction", "lnk_transfers")
               if not _exists(store, t)]
    if missing:
        raise SystemExit(f"语义层缺失 {missing}：先跑 python -m scripts.build_ontology")

    # ---- 对象级策略检查（fail-closed；AC5 越权落审计+告警）----
    try:
        for obj in ("person", "org", "account", "bid_project",
                    "transaction", "call", "trackpoint"):
            engine.check_object(ctx, obj)
        for lnk in ("transfers", "calls_to", "co_located", "owns",
                    "involved_in", "time_window"):
            engine.check_link(ctx, lnk)
    except PolicyDeniedError as e:
        # AC5：越权尝试必须被审计记录并告警
        from core.audit import AuditChain
        AuditChain(store.conn).append(
            operator=ctx.operator,
            before=None,
            after={"action": "export_denied", "purpose": ctx.purpose,
                   "destination": args.destination, "reason": str(e)},
            source_row_ids=[],
            ontology_version="n/a")
        print(f"[告警] 导出被策略拒绝并已落审计：{e}", file=sys.stderr)
        if own_store:
            store.close()
        return 2

    OUT.mkdir(exist_ok=True)
    exported: dict[str, int] = {}
    masked: list[str] = []
    for obj, cols in (("person", ["raw_name", "id_card"]),
                      ("tipoff", ["content_raw", "reporter_raw"])):
        masked += [f"{obj}.{c}" for c in _masked_columns(engine, ctx, obj, cols)
                   if c in ("id_card", "content_raw", "reporter_raw")]

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

    # ---- AC3：导出落审计（operator / purpose / destination / 文件清单）----
    from core.audit import AuditChain
    AuditChain(store.conn).append(
        operator=ctx.operator,
        before=None,
        after={"action": "export", "purpose": ctx.purpose,
               "destination": args.destination,
               "role": ctx.role, "clearance": ctx.clearance,
               "files": dict(exported),
               "masked_columns": masked},
        source_row_ids=[],
        ontology_version="n/a")

    print("LadybugDB 图谱 CSV 已从语义层导出：", OUT)
    for fname, n in exported.items():
        print(f"  {fname:<24} {n} 行")
    if masked:
        print(f"  按属性策略剔除/遮蔽列：{masked}")
    print(f"  导出事件已落审计链（operator={ctx.operator}, "
          f"purpose={ctx.purpose or '-'}, destination={args.destination}）")
    if own_store:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
