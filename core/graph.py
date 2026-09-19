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
import re
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


# ----------------------------------------------------------------------
# P4：语义层统一关系图（跨边类型：资金/通话/持有/中标/同框）
#
# 与 LadybugDB 轨的关系：语义层图始终可用（纯离线、只读语义表），为关系研判
# 镜头的主轨，结果标 engine="semantic"；Ladybug 多关系 Cypher 后端为后续增强，
# 本节不依赖图库。缺边表/缺列按 REQ-G-003 结构降级：该边跳过并进 gaps，不崩。
# ----------------------------------------------------------------------

# link 名 → (起点列, 终点列, 行键列, 边类别)。列名以 bindings.json build_sql
# 实际输出为准（default 与 reqd_case 同构）。关系网络按无向遍历（资金/通话
# 关联本身是关系，反向追溯同样成立），SEdge 保留原方向供展示。
SEMANTIC_EDGE_SPECS: Dict[str, tuple] = {
    "transfers":   ("from_account_id", "to_account_id", "txn_id",     "fund"),
    "calls_to":    ("from_person",     "to_person",     "call_id",    "contact"),
    "owns":        ("account_id",      "owner_person",  "account_id", "org"),
    "involved_in": ("org_id",          "project_id",    "org_id",     "org"),
    "co_located":  ("person_1",        "person_2",      "track_id_1", "contact"),
}

# 代理键前缀 → 对象类型（与 objects.json key.prefix 固化一致）
NODE_PREFIX_TYPE: Dict[str, str] = {
    "person": "person",
    "account": "account",
    "org": "org",
    "project": "bid_project",
}

# 对象类型 → 名称属性（取节点展示名）
NODE_NAME_PROP: Dict[str, str] = {
    "person": "raw_name",
    "account": "raw_name",
    "org": "raw_name",
    "bid_project": "title",
}

# 对象类型 → 物化表主键列名（objects.json key.column 固化值；obj_* 表无统一
# "pk" 列，名称解析与证据引用 key_column 都按此映射取真实列）
NODE_PK_COLUMN: Dict[str, str] = {
    "person": "person_id",
    "account": "account_id",
    "org": "org_id",
    "bid_project": "project_id",
}

EDGE_KINDS = ("all", "fund", "contact", "org")

# 主体标识符白名单（自由文本入参第二道闸：查询本身参数化，此处先挡异常输入）
# 允许：中文、字母、数字、空格、_ - · * （）() 及代理键下划线；长度 ≤128
_SUBJECT_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9 _\-·*（）()]{1,128}$")


@dataclass
class SEdge:
    """语义层一条边（统一图形态）。"""
    src: str            # 起点节点 pk
    dst: str            # 终点节点 pk
    edge: str           # link 名（transfers/calls_to/...）
    edge_pk: str        # 边表行键值（证据溯源）
    key_column: str     # 边表行键列
    kind: str           # fund/contact/org
    reversed_traversal: bool = False   # 本次遍历是否沿原边反向

    def ref(self) -> dict:
        return {"kind": "edge", "ref": f"lnk_{self.edge}#{self.edge_pk}",
                "key_column": self.key_column}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SemanticGraph:
    """跨边类型统一关系图（无向邻接）。"""
    adj: Dict[str, List[SEdge]] = field(default_factory=dict)
    gaps: List[dict] = field(default_factory=list)   # 结构降级缺口（缺表/缺列）
    loaded_links: List[str] = field(default_factory=list)

    def add_edge(self, e: SEdge) -> None:
        self.adj.setdefault(e.src, []).append(e)
        self.adj.setdefault(e.dst, []).append(SEdge(
            src=e.dst, dst=e.src, edge=e.edge, edge_pk=e.edge_pk,
            key_column=e.key_column, kind=e.kind, reversed_traversal=True))

    def nodes(self) -> set:
        return set(self.adj)

    def neighborhood(self, start: str, depth: int
                     ) -> tuple[Dict[str, int], Dict[str, list]]:
        """BFS：返回 (节点→跳数, 节点→首达边链)。depth 内可达节点。"""
        hops = {start: 0}
        first_path: Dict[str, list] = {start: []}
        frontier = [start]
        for d in range(1, depth + 1):
            nxt = []
            for node in frontier:
                for e in self.adj.get(node, []):
                    if e.dst not in hops:
                        hops[e.dst] = d
                        first_path[e.dst] = first_path[node] + [e]
                        nxt.append(e.dst)
            frontier = nxt
        return hops, first_path

    def one_hop(self, node: str) -> Dict[str, SEdge]:
        """一跳邻居 → 首条关联边（多关联取一条，全部边由调用方另查）。"""
        out: Dict[str, SEdge] = {}
        for e in self.adj.get(node, []):
            out.setdefault(e.dst, e)
        return out

    def common_neighbors(self, a: str, b: str) -> Dict[str, tuple]:
        """共同邻居：{邻居pk: (从a到达边, 从b到达边)}。"""
        na, nb = self.one_hop(a), self.one_hop(b)
        return {n: (na[n], nb[n]) for n in na.keys() & nb.keys()}

    def paths(self, a: str, b: str, depth: int,
              max_paths: int = 20) -> List[List[SEdge]]:
        """两节点间简单路径枚举（DFS + 环剪枝 + 条数上限防爆）。"""
        results: List[List[SEdge]] = []

        def dfs(node: str, chain: List[SEdge], visited: set) -> None:
            if len(results) >= max_paths:
                return
            if len(chain) >= depth:
                return
            for e in self.adj.get(node, []):
                if e.dst in visited or len(results) >= max_paths:
                    continue
                if e.dst == b:
                    results.append(chain + [e])
                    continue
                dfs(e.dst, chain + [e], visited | {e.dst})

        dfs(a, [], {a})
        return results


def _table_name(ctx, prefix: str, name: str) -> str:
    """表名经 RuntimeContext 派生（换包不崩）；无 ctx 时回落默认前缀。"""
    if ctx is not None:
        return ctx.link(name) if prefix == "lnk_" else ctx.table(name)
    return f"{prefix}{name}"


def load_semantic_graph(store, ctx=None, edge_kinds: str = "all",
                        only_links: Optional[List[str]] = None
                        ) -> SemanticGraph:
    """
    从语义边表加载统一关系图。

    缺边表/缺列（CatalogException/BinderException）= 该数据源未接入，
    边跳过并写入 g.gaps（REQ-G-002/003 结构降级），其余边照常加载。
    端点外键为 NULL（LEFT JOIN 未命中归一）的边跳过。
    """
    if edge_kinds not in EDGE_KINDS:
        raise ValueError(f"edge_kinds={edge_kinds!r}，允许 {EDGE_KINDS}")
    g = SemanticGraph()
    for link_name, (src_col, dst_col, key_col, kind) in SEMANTIC_EDGE_SPECS.items():
        if only_links is not None and link_name not in only_links:
            continue
        if edge_kinds != "all" and kind != edge_kinds:
            continue
        table = _table_name(ctx, "lnk_", link_name)
        try:
            rows = store.query(
                f'SELECT "{key_col}", "{src_col}", "{dst_col}" FROM "{table}"')
        except Exception as e:
            # 结构降级：语义表/列缺失。其余真实错误（权限/只读护栏）照抛——
            # 只吞 Catalog/Binder 且消息指向语义表的异常。
            msg = str(e)
            is_structural = (
                type(e).__name__ in ("CatalogException", "BinderException")
                and ("obj_" in msg or "lnk_" in msg or "does not exist" in msg))
            if not is_structural:
                raise
            g.gaps.append({"link": link_name, "table": table,
                           "reason": f"{type(e).__name__}: {msg.splitlines()[0][:120]}"})
            continue
        n_edges = 0
        for r in rows:
            src, dst, pk = r[src_col], r[dst_col], r[key_col]
            if not src or not dst or not pk:
                continue
            g.add_edge(SEdge(src=src, dst=dst, edge=link_name, edge_pk=str(pk),
                             key_column=key_col, kind=kind))
            n_edges += 1
        g.loaded_links.append(link_name)
    return g


def node_type_of(pk: str) -> Optional[str]:
    """代理键前缀 → 对象类型（无法识别返回 None）。"""
    prefix = pk.split("_", 1)[0] if "_" in pk else ""
    return NODE_PREFIX_TYPE.get(prefix)


def load_node_names(store, pks: set, ctx=None) -> Dict[str, str]:
    """批量解析节点展示名（按前缀分组，参数化查询 obj_* 主键列+名称列）。"""
    groups: Dict[str, set] = {}
    for pk in pks:
        t = node_type_of(pk)
        if t:
            groups.setdefault(t, set()).add(pk)
    names: Dict[str, str] = {}
    for obj_type, keys in groups.items():
        name_prop = NODE_NAME_PROP[obj_type]
        pk_col = NODE_PK_COLUMN[obj_type]
        table = _table_name(ctx, "obj_", obj_type)
        try:
            placeholders = ", ".join(["?"] * len(keys))
            rows = store.query(
                f'SELECT "{pk_col}" AS pk, "{name_prop}" FROM "{table}" '
                f'WHERE "{pk_col}" IN ({placeholders})', tuple(keys))
        except Exception as e:
            if type(e).__name__ in ("CatalogException", "BinderException"):
                continue
            raise
        for r in rows:
            names[r["pk"]] = r[name_prop]
    return names


def resolve_subject(store, target: str, target_type: str = "auto",
                    ctx=None) -> Optional[dict]:
    """
    主体名/pk → {"pk","type","name"}。

    target 先经标识符白名单（防注入；查询本身参数化）。
    target_type=auto 时按 pk 精确命中优先，其次四类实体名称列精确匹配；
    多类型同名命中（如人名与账户名相同）→ ValueError 列候选，要求显式消歧。
    """
    target = (target or "").strip()
    if not target:
        return None
    if not _SUBJECT_RE.match(target):
        raise ValueError(
            f"主体标识 {target!r} 含非法字符（允许中英文/数字/空格/_-·*（），"
            f"长度 ≤128）")
    # 直接是代理键
    direct_type = node_type_of(target)
    if direct_type and (target_type in ("auto", direct_type)):
        name_prop = NODE_NAME_PROP[direct_type]
        pk_col = NODE_PK_COLUMN[direct_type]
        table = _table_name(ctx, "obj_", direct_type)
        rows = store.query(
            f'SELECT "{pk_col}" AS pk, "{name_prop}" AS nm FROM "{table}" '
            f'WHERE "{pk_col}" = ?', (target,))
        if rows:
            return {"pk": rows[0]["pk"], "type": direct_type,
                    "name": rows[0]["nm"]}
    candidates: List[dict] = []
    types_ = [target_type] if target_type in NODE_NAME_PROP else list(NODE_NAME_PROP)
    for obj_type in types_:
        name_prop = NODE_NAME_PROP[obj_type]
        pk_col = NODE_PK_COLUMN[obj_type]
        table = _table_name(ctx, "obj_", obj_type)
        try:
            rows = store.query(
                f'SELECT "{pk_col}" AS pk, "{name_prop}" AS nm FROM "{table}" '
                f'WHERE "{name_prop}" = ?', (target,))
        except Exception as e:
            if type(e).__name__ in ("CatalogException", "BinderException"):
                continue
            raise
        for r in rows:
            candidates.append({"pk": r["pk"], "type": obj_type, "name": r["nm"]})
    if not candidates:
        return None
    if len(candidates) > 1:
        kinds = sorted({c["type"] for c in candidates})
        if len(kinds) > 1:
            raise ValueError(
                f"主体 {target!r} 在多类实体中同名命中 {kinds}，"
                f"请用 target_type 显式消歧")
    return candidates[0]
