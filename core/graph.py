"""
core/graph.py
L4 图库层：LadybugDB 真实集成（第 2 步：Q2 过桥 Cypher 化 + SQL 双轨一致性比对）。

数据入口（已实测，见 scripts/verify_ladybug.py）：
  DuckDB 计算 → 导出 CSV → COPY 进 LadybugDB → Cypher 多跳
  （ATTACH DuckDB 需运行时下载扩展，受限网络下不可得，故走 CSV 中转）

已验证能力：
  ✅ 建节点表 / 关系表      ✅ CSV 批量导入
  ✅ 多跳 MATCH             ✅ 变长跳 [*1..2]

红线不变：图库只出「关系路径」，不出定性结论；每条路径须回源 DuckDB 原始行。
"""
from __future__ import annotations

import csv
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class OverpassPath:
    """一条过桥路径（两跳：上游 → 桥 → 下游）。"""
    source: str           # 上游主体
    bridge: str           # 过桥方
    dest: str             # 下游主体
    amount_in: float      # 流入桥的金额
    amount_out: float     # 流出桥的金额
    engine: str           # "cypher" / "sql"
    source_rows: List[str] = field(default_factory=list)   # 溯源到原始行

    def to_dict(self) -> dict:
        return asdict(self)

    def key(self) -> tuple:
        """一致性比对用的规范化键（金额可能因浮点有微小差异，故不参与比对）。"""
        return (self.source, self.bridge, self.dest)


# ----------------------------------------------------------------------
# 图库后端
# ----------------------------------------------------------------------
class GraphBackend:
    """
    LadybugDB 后端封装。

    用法：
        g = GraphBackend("data/ladybug/investigation.lbug")
        g.build_from_duckdb(con)                  # DuckDB → CSV → 图库
        paths = g.overpass_two_hop()              # Q2 Cypher 多跳
    """

    def __init__(self, db_path: str = "data/ladybug/investigation.lbug",
                 buffer_pool_size: int = 0):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.buffer_pool_size = buffer_pool_size
        self._db = None
        self._conn = None
        self.available = self._check_import()

    # ---- 可用性 ----
    def _check_import(self) -> bool:
        try:
            import ladybug  # noqa: F401
            return True
        except ImportError:
            return False

    @property
    def conn(self):
        if not self.available:
            raise RuntimeError("未安装 ladybug：pip install ladybug")
        if self._conn is None:
            import ladybug as lb
            self._db = lb.Database(str(self.db_path),
                                   buffer_pool_size=self.buffer_pool_size)
            self._conn = lb.Connection(self._db)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._db = None

    # ---- 建图 ----
    def build_from_duckdb(self, conn, flow_table: str = "银行流水",
                          rebuild: bool = True) -> Dict[str, int]:
        """
        从 DuckDB 建图（导出 CSV → COPY 进图库）。

        数据入口语义层优先：存在 lnk_transfers（obj/lnk 语义表）则从语义层取边，
        否则回落 flow_table（L2 银行流水，列 主体/对方/金额/日期）。

        坑位记录（实测）：COPY 边表时若引用了节点表中不存在的节点，
        会抛 "Unable to find primary key value X" —— 因此必须
        ① 先导入全部节点（主体 ∪ 对方），② 再导入边。
        """
        if not self.available:
            return {"nodes": 0, "edges": 0, "skipped": True}

        c = getattr(conn, "conn", conn)
        table, (c_from, c_to, c_amt, c_date) = _flow_source(c, flow_table)
        rows = c.execute(
            f'SELECT "{c_from}", "{c_to}", "{c_amt}", "{c_date}" FROM "{table}"'
        ).fetchall()

        # 节点 = 主体 ∪ 对方（去重，保证边表引用的节点全部存在）
        names = sorted({r[0] for r in rows} | {r[1] for r in rows})
        tmp = Path(tempfile.mkdtemp(prefix="lbug_import_"))
        node_csv = tmp / "nodes.csv"
        edge_csv = tmp / "edges.csv"

        with open(node_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name"])
            for n in names:
                w.writerow([n])
        with open(edge_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["frm", "to", "amount", "tdate"])
            for a, b, amt, d in rows:
                w.writerow([a, b, float(amt), str(d)])

        if rebuild:
            # 重建表（DDL 不支持 IF NOT EXISTS 语义下的幂等清理，先 DROP）
            for stmt in ["DROP TABLE IF EXISTS TRANSFER", "DROP TABLE IF EXISTS Entity"]:
                try:
                    self.conn.execute(stmt)
                except Exception:
                    pass

        self.conn.execute(
            "CREATE NODE TABLE Entity(name STRING, PRIMARY KEY(name))"
        )
        self.conn.execute(
            "CREATE REL TABLE TRANSFER(FROM Entity TO Entity, amount DOUBLE, tdate STRING)"
        )
        # Windows 反斜杠路径会被 Cypher parser 当转义序列，COPY 语句必须用正斜杠
        self.conn.execute(f"COPY Entity FROM '{node_csv.as_posix()}' (HEADER=true)")
        self.conn.execute(f"COPY TRANSFER FROM '{edge_csv.as_posix()}' (HEADER=true)")

        # 清理临时 CSV
        node_csv.unlink(missing_ok=True)
        edge_csv.unlink(missing_ok=True)
        tmp.rmdir()

        return {"nodes": len(names), "edges": len(rows), "skipped": False}

    # ---- Q2：两跳过桥 ----
    def overpass_two_hop(self, exclude_self_loop: bool = True) -> List[OverpassPath]:
        """
        Q2 过桥识别：Cypher 两跳 MATCH。
        上游 → 过桥方 → 下游，三者互不相同（排除自环与直接往返）。
        """
        if not self.available:
            return []
        cypher = """
            MATCH (a:Entity)-[e1:TRANSFER]->(m:Entity)-[e2:TRANSFER]->(b:Entity)
            RETURN a.name, m.name, b.name, e1.amount, e2.amount, e1.tdate, e2.tdate
        """
        res = self.conn.execute(cypher)
        out: List[OverpassPath] = []
        while res.has_next():
            row = res.get_next()
            src, mid, dst, amt1, amt2, d1, d2 = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            if exclude_self_loop and len({src, mid, dst}) < 3:
                continue
            out.append(OverpassPath(
                source=src, bridge=mid, dest=dst,
                amount_in=float(amt1), amount_out=float(amt2),
                engine="cypher",
                source_rows=[f"TRANSFER({src}→{mid}@{d1})", f"TRANSFER({mid}→{dst}@{d2})"],
            ))
        return out

    def neighbors_within(self, subject: str, max_hops: int = 2) -> List[str]:
        """奇兵拓线：取主体的 N 跳内邻域（变长跳）。"""
        if not self.available:
            return []
        res = self.conn.execute(
            f"MATCH (a:Entity {{name:'{subject}'}})-[*1..{max_hops}]->(b:Entity) "
            f"RETURN DISTINCT b.name"
        )
        out = []
        while res.has_next():
            out.append(res.get_next()[0])
        return out


# ----------------------------------------------------------------------
# 数据入口：语义层优先（lnk_transfers），未构建语义层时回落 L2 银行流水
# ----------------------------------------------------------------------
def _flow_source(c, flow_table: str = "银行流水") -> tuple[str, tuple[str, str, str, str]]:
    """返回 (表名, (from列, to列, 金额列, 日期列))。"""
    has_sem = c.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'lnk_transfers'"
    ).fetchone()[0] > 0
    if has_sem:
        return "lnk_transfers", ("from_account", "to_account", "amount", "date")
    return flow_table, ("主体", "对方", "金额", "日期")


# ----------------------------------------------------------------------
# SQL 对照（同一问题的关系型解法，用于双轨一致性比对）
# ----------------------------------------------------------------------
def overpass_two_hop_sql(conn, flow_table: str = "银行流水") -> List[OverpassPath]:
    """
    Q2 过桥的 SQL 解法：流表自连接。
    与 Cypher 版互为校验 —— 两者结果必须一致，否则说明某一侧口径有误。
    数据入口与建图同源（_flow_source），保证双轨口径一致。
    """
    c = getattr(conn, "conn", conn)
    table, (c_from, c_to, c_amt, c_date) = _flow_source(c, flow_table)
    rows = c.execute(f"""
        SELECT a."{c_from}" AS src, a."{c_to}" AS mid, b."{c_to}" AS dst,
               a."{c_amt}" AS amt1, b."{c_amt}" AS amt2,
               a."{c_date}" AS d1, b."{c_date}" AS d2
        FROM "{table}" a
        JOIN "{table}" b ON a."{c_to}" = b."{c_from}"
        WHERE a."{c_from}" <> b."{c_to}"
          AND a."{c_from}" <> a."{c_to}"
          AND b."{c_from}" <> b."{c_to}"
    """).fetchall()
    return [
        OverpassPath(
            source=r[0], bridge=r[1], dest=r[2],
            amount_in=float(r[3]), amount_out=float(r[4]),
            engine="sql",
            source_rows=[f"{table}({r[0]}→{r[1]}@{r[5]})", f"{table}({r[1]}→{r[2]}@{r[6]})"],
        )
        for r in rows
    ]


# ----------------------------------------------------------------------
# 双轨一致性比对
# ----------------------------------------------------------------------
def compare_engines(cypher_paths: List[OverpassPath],
                    sql_paths: List[OverpassPath]) -> Dict[str, Any]:
    """
    比对图库与 SQL 两轨结果。
    一致 → 结果可信；不一致 → 标记差异，交由正兵复核（不自动采信任一侧）。
    """
    c_set = {p.key(): p for p in cypher_paths}
    s_set = {p.key(): p for p in sql_paths}
    only_cypher = sorted(set(c_set) - set(s_set))
    only_sql = sorted(set(s_set) - set(c_set))
    both = sorted(set(c_set) & set(s_set))

    return {
        "cypher_count": len(cypher_paths),
        "sql_count": len(sql_paths),
        "matched": len(both),
        "only_in_cypher": [list(k) for k in only_cypher],
        "only_in_sql": [list(k) for k in only_sql],
        "consistent": not only_cypher and not only_sql,
        "detail": [
            {
                "path": list(k),
                "cypher_amount": [c_set[k].amount_in, c_set[k].amount_out],
                "sql_amount": [s_set[k].amount_in, s_set[k].amount_out],
            }
            for k in both
        ],
        "note": "双轨一致才可信；不一致须正兵复核，AI 不自动采信任一侧",
    }
