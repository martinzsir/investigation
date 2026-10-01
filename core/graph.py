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

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb


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
    # --- ITM-05 时态能力：两跳之间的时间间隔（天）---
    gap_days: Optional[float] = None    # 入账→出账相隔天数；None 表示不可判定
    gap_unknown: bool = False           # True = 日期缺失/不可解析，未做时间判定

    def to_dict(self) -> dict:
        return asdict(self)

    def key(self) -> tuple:
        """一致性比对用的规范化键（金额可能因浮点有微小差异，故不参与比对）。"""
        return (self.source, self.bridge, self.dest)


# ----------------------------------------------------------------------
# ITM-05：边的时态能力（过桥两跳间隔）
# ----------------------------------------------------------------------
def overpass_gap_days(d_in: Any, d_out: Any) -> Optional[float]:
    """两跳之间的时间间隔（天），**出账晚于入账**为正。

    不可判定（日期缺失 / 无法解析）时返回 None —— 这是刻意的：
    与 time_conflict 同款红线，**不静默取舍**。判不出来就如实说判不出来，
    由调用方决定是保留标注还是过滤，绝不在这里偷偷塞一个 0 或默认值。
    """
    from core.time_semantics import _coerce_dt

    a = _coerce_dt(d_in)
    b = _coerce_dt(d_out)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 86400.0


def _apply_gap_filter(paths: List[OverpassPath], max_gap_days: Optional[float],
                      d_in_of, d_out_of) -> List[OverpassPath]:
    """给过桥路径补算间隔并按阈值过滤（Cypher / SQL 双轨共用，保证口径一致）。

    max_gap_days=None → 不过滤，仅补算 gap（事实先暴露，行为不变）。
    不可判定的路径**一律保留**并标 gap_unknown=True，不静默丢弃。
    """
    out: List[OverpassPath] = []
    for p, din, dout in zip(paths, d_in_of, d_out_of):
        gap = overpass_gap_days(din, dout)
        if gap is None:
            p.gap_unknown = True
            out.append(p)
            continue
        p.gap_days = gap
        if max_gap_days is not None and gap > max_gap_days:
            continue
        if max_gap_days is not None and gap < -max_gap_days:
            # 出账早于入账超过阈值：不是"过桥"，是无关的反向流水
            continue
        out.append(p)
    return out


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
                          rebuild: bool = True, pack: str = "default") -> Dict[str, Any]:
        """
        语义层 → LadybugDB 通用建图的瘦封装（委托 core.ladybug_builder）。

        图模型（统一节点表 + 每条 lnk_* 一张 REL 表，声明驱动）：
          Entity(pk, type, name) 主键为对象代理键；
          TRANSFERS / CALLS_TO / OWNS / ... 边端点为代理键，边携带 edge_pk
          （lnk_<name>#<edge_pk> 证据引用）与声明属性。
        先 COPY 节点后 COPY 边、暂存原子替换、缺 ladybug/COPY 失败只 skipped
        不抛——全部纪律在 build_case_graph 内统一实现。

        历史参数 flow_table / rebuild 保留仅为向后兼容（旧裸流表直建路径已删除：
        图库只消费 obj_*/lnk_* 语义表，与检测器/MCP 同源同纪律）。
        pack 可传已装载的 OntologyPack 或包名。
        """
        if not self.available:
            return {"nodes": 0, "edges": 0, "skipped": True}

        from core.ladybug_builder import build_case_graph
        from core.ontology_loader import load_pack
        spec = load_pack(pack) if isinstance(pack, str) else pack
        store = _RawConnAdapter(conn) if isinstance(
            conn, duckdb.DuckDBPyConnection) else conn
        res = build_case_graph(store, spec, self.db_path)
        if res.get("skipped"):
            return {"nodes": 0, "edges": 0, "skipped": True,
                    "reason": res.get("reason", "")}
        return {"nodes": res["nodes"], "edges": res["edges"], "skipped": False,
                "node_types": res.get("node_types", []),
                "links": res.get("links", {})}

    # ---- Q2：两跳过桥 ----
    def overpass_two_hop(self, exclude_self_loop: bool = True,
                         max_gap_days: Optional[float] = None) -> List[OverpassPath]:
        """
        Q2 过桥识别：Cypher 两跳 MATCH。
        上游 → 过桥方 → 下游，三者互不相同（排除自环与直接往返）。

        ITM-05：两跳间隔此前只进 source_rows 文案，未参与判定——相隔三年的
        两笔转账也会被当成一条"过桥路径"。现补算 gap_days 并支持 max_gap_days
        过滤；默认 None 不过滤（行为不变），仅把事实暴露出来。
        """
        if not self.available:
            return []
        # 通用图：Entity 统一节点表（代理键主键 + name 展示名），资金边为
        # TRANSFERS REL（端点账户代理键；amount/date 为声明边属性）。
        # 路径点名回退到节点 name（账户原始名），与 SQL 轨输出契约保持一致。
        cypher = """
            MATCH (a:Entity)-[e1:TRANSFERS]->(m:Entity)-[e2:TRANSFERS]->(b:Entity)
            RETURN a.name, m.name, b.name, e1.`amount`, e2.`amount`,
                   e1.`date`, e2.`date`, e1.edge_pk, e2.edge_pk
        """
        res = self.conn.execute(cypher)
        out: List[OverpassPath] = []
        din_list: List[Any] = []
        dout_list: List[Any] = []
        while res.has_next():
            row = res.get_next()
            src, mid, dst, amt1, amt2, d1, d2 = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            if exclude_self_loop and len({src, mid, dst}) < 3:
                continue
            out.append(OverpassPath(
                source=src, bridge=mid, dest=dst,
                amount_in=float(amt1), amount_out=float(amt2),
                engine="cypher",
                source_rows=[f"lnk_transfers#{row[7]}", f"lnk_transfers#{row[8]}"],
            ))
            din_list.append(d1)
            dout_list.append(d2)
        return _apply_gap_filter(out, max_gap_days, din_list, dout_list)

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
# 只读查询通道（兼容 Store / ReadOnlyStore / 裸 duckdb 连接）
# ----------------------------------------------------------------------
class _RawConnAdapter:
    """裸 duckdb 连接 → 构建器所需的最小 store 形态（query 参数化 + execute）。

    build_case_graph 只用两处：information_schema 计数（带参数）与 COPY TO
    临时 CSV；生产路径（CLI/worker/MCP）传的都是 core.store.Store，适配器
    仅服务直传裸连接的测试/脚本。
    """

    def __init__(self, c: duckdb.DuckDBPyConnection):
        self._c = c

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cur = self._c.execute(sql, tuple(params or ()))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute(self, sql: str):
        return self._c.execute(sql)


def _query_rows(c, sql: str, **kw) -> List[Dict[str, Any]]:
    """统一只读查询，返回 dict 列表。

    py Function 经 ReadOnlyStore 代理调用时禁止访问 execute/conn（只读护栏），
    故 Store / ReadOnlyStore 一律走 query()：与 SQL 轨共用 _assert_readonly
    白名单，不绕过 REQ-003。

    注意顺序：裸 duckdb 连接**也有** query()，但返回 DuckDBPyRelation 而非
    list[dict]，故必须先按连接类型分流——否则 relation 当 list 下标访问会抛
    TypeError（建图/测试直传裸连接的路径会崩）。
    """
    if isinstance(c, duckdb.DuckDBPyConnection):
        cur = c.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    q = getattr(c, "query", None)
    if callable(q):
        return q(sql, **kw)
    cur = c.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ----------------------------------------------------------------------
# 数据入口：语义层优先（lnk_transfers），未构建语义层时回落 L2 银行流水
# ----------------------------------------------------------------------
def _flow_source(c, flow_table: str = "银行流水") -> tuple[str, tuple[str, str, str, str], bool]:
    """返回 (表名, (from列, to列, 金额列, 日期列), 是否语义层)。"""
    rows = _query_rows(
        c,
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_name = 'lnk_transfers'",
    )
    has_sem = bool(rows) and int(rows[0].get("c") or 0) > 0
    if has_sem:
        return "lnk_transfers", ("from_account", "to_account", "amount", "date"), True
    return flow_table, ("主体", "对方", "金额", "日期"), False


def has_semantic_flow(c) -> bool:
    """语义层 lnk_transfers 是否可用。

    供调用方在「语义层缺失」时决定降级还是回落直查源表——py Function 只读
    通道不得直查 L2 业务源表（REQ-003），须据此先判定再降级。
    """
    return _flow_source(c)[2]


# ----------------------------------------------------------------------
# SQL 对照（同一问题的关系型解法，用于双轨一致性比对）
# ----------------------------------------------------------------------
def overpass_two_hop_sql(conn, flow_table: str = "银行流水", *,
                         allow_unsafe_fallback: bool = True,
                         max_gap_days: Optional[float] = None) -> List[OverpassPath]:
    """
    Q2 过桥的 SQL 解法：流表自连接。
    与 Cypher 版互为校验 —— 两者结果必须一致，否则说明某一侧口径有误。
    数据入口与建图同源（_flow_source），保证双轨口径一致。

    只读护栏（修复 overpass_two_hop 经 py Function 调用必崩）：
      原先 c = getattr(conn, "conn", conn) + c.execute(...) 两步都会撞
      ReadOnlyStore 黑名单（conn / execute）——py Function 拿到的 store 是只读代理。
      现统一走 _query_rows()，与 SQL 轨共用只读白名单。

    allow_unsafe_fallback（默认 True，Store 调用方保持旧行为）：
      语义层 lnk_transfers 缺失时是否回落直查 L2 业务源表。直查违反 REQ-003，
      故走 query(unsafe=True) 调试通道——具名 operator + 理由 + 行数上限 + 审计落盘。
      py Function 传 False：只读通道不直查源表，由调用方降级留痕。

    max_gap_days（ITM-05）：
      两跳入账→出账的最大允许间隔（天）。None = 不过滤，仅补算 gap_days。
      与 Cypher 轨共用 _apply_gap_filter，保证双轨口径一致。
    """
    table, (c_from, c_to, c_amt, c_date), is_semantic = _flow_source(conn, flow_table)
    sql = f"""
        SELECT a."{c_from}" AS src, a."{c_to}" AS mid, b."{c_to}" AS dst,
               a."{c_amt}" AS amt1, b."{c_amt}" AS amt2,
               a."{c_date}" AS d1, b."{c_date}" AS d2
        FROM "{table}" a
        JOIN "{table}" b ON a."{c_to}" = b."{c_from}"
        WHERE a."{c_from}" <> b."{c_to}"
          AND a."{c_from}" <> a."{c_to}"
          AND b."{c_from}" <> b."{c_to}"
    """
    if is_semantic:
        rows = _query_rows(conn, sql)
    elif allow_unsafe_fallback:
        rows = conn.query(
            sql, unsafe=True, operator="graph.overpass",
            reason="语义层 lnk_transfers 缺失，Q2 过桥双轨校验回落直查 L2 流表",
        )
    else:
        # 只读通道不直查源表：降级为空，由调用方标 degraded 落健康度
        return []
    paths = [
        OverpassPath(
            source=r["src"], bridge=r["mid"], dest=r["dst"],
            amount_in=float(r["amt1"]), amount_out=float(r["amt2"]),
            engine="sql",
            source_rows=[f"{table}({r['src']}→{r['mid']}@{r['d1']})",
                         f"{table}({r['mid']}→{r['dst']}@{r['d2']})"],
        )
        for r in rows
    ]
    return _apply_gap_filter(paths, max_gap_days,
                             [r.get("d1") for r in rows],
                             [r.get("d2") for r in rows])


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


# ----------------------------------------------------------------------
# GraphGateway：Ladybug Cypher 主轨 + SemanticGraph 语义兜底（阶段 1）
# ----------------------------------------------------------------------
class GraphGateway:
    """关系三镜头（neighborhood/common_neighbors/paths）的统一双轨入口。

    主轨 ladybug
      边集由白名单 Cypher 从案件版本级 .lbug 的 REL 表**定向扫描**取得：
      ``MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.pk, b.pk, r.edge_pk``。
      REL 名只来自 SEMANTIC_EDGE_SPECS 常量（无用户输入插值），证据边引用
      即 REL 的 edge_pk 列；多跳算法（无向 BFS/共同邻居/简单路径 DFS）复用
      同一套 SemanticGraph。

      不复用 Cypher 变长 walk 做枚举的原因（实测 demoZ）：114 条平行通话边
      会让 3 跳 walk 爆出 262 万条且允许重节点，规模与简单路径语义均不可控
      （方案 §七：图轨做模式匹配、算法在 py 侧）。双轨因此**仅边来源不同**：
      ladybug 轨边来自 Cypher 扫 REL，semantic 轨边来自 lnk_* 表，图算法是
      同一份实现，结果严格同构——这正是双轨对拍可信的基础。

    回落 semantic（任何一关不过即回落，只降级不失败）
      ladybug 未安装 / 无图路径 / 图文件缺失 / manifest 行数对账陈旧 /
      开图或扫描异常 → load_semantic_graph 内存轨，diagnostics 标
      engine="semantic" + engine_fallback_reason。图缺失/陈旧**不**计入
      g.gaps（关系数据未必缺），degraded 仍只由语义边表结构缺口决定。

    图路径纪律（不靠猜）
      ① 显式 graph_path 优先：worker 按 case_dir+version 传
      cases/<cid>/graph/vN.lbug（lens_run/detect 同一口径）；
      ② 无显式路径时仅按 store.db_path 保守推导：cases/ 旁路同名版本图，
      或全库 investigation.duckdb → data/ladybug/investigation.lbug；
      ③ 推导不出（:memory: 等）→ 纯语义轨。
    """

    FALLBACK_GRAPH = Path("data/ladybug/investigation.lbug")
    _EDGE_SCAN_LIMIT = 200_000

    def __init__(self, store, pack: str = "default", *, ctx=None,
                 graph_path: Optional[str | Path] = None, base_dir=None,
                 cross_check: Optional[bool] = None):
        self.store = store
        if pack is None:
            pack = getattr(ctx, "pack", None) or "default"
        self.pack = pack
        self.ctx = ctx
        self.base_dir = (base_dir if base_dir is not None
                         else getattr(ctx, "base_dir", None))
        self.explicit_path = Path(graph_path) if graph_path else None
        # 线上对拍闸（方案 §1.3）：默认关；SUNZI_GRAPH_CROSSCHECK=1 开启抽样
        # 对拍。对拍只比**物理边集**（(link, edge_pk) 去重，反向邻接副本不算），
        # 不一致即弃图轨、以 semantic 为准并落 engine_mismatch 诊断。
        if cross_check is None:
            cross_check = os.environ.get("SUNZI_GRAPH_CROSSCHECK", "") == "1"
        self.cross_check = bool(cross_check)
        self._cache: Dict[str, SemanticGraph] = {}
        self.engine = "semantic"
        self.graph_path: Optional[Path] = None
        self.rel_tables: List[str] = []
        self.fallback_reason: Optional[str] = None
        self.edge_scan_error: Optional[str] = None
        self.mismatch: Optional[dict] = None
        self._lb_conn = None
        self._lb_db = None
        self._opened = False
        self._usable = False

    @staticmethod
    def _physical_edge_sigs(g: SemanticGraph) -> set:
        """图的物理边签名集合（邻接反向副本排除）。"""
        return {
            (e.edge, e.edge_pk)
            for edges in g.adj.values()
            for e in edges
            if not e.reversed_traversal
        }

    # ---- 开图四连闸（ladybug 可用 → 路径 → 文件 → manifest 对账）----
    def _ensure_graph(self) -> bool:
        if self._opened:
            return self._usable
        self._opened = True
        try:
            import ladybug as lb  # noqa: F401
        except ImportError:
            self.fallback_reason = "ladybug 未安装（仅 WSL/Linux 可用）"
            return False
        path = self.explicit_path or self._derive_path()
        if path is None:
            self.fallback_reason = "no_graph_path（未显式传图且无法从库路径推导）"
            return False
        self.graph_path = path
        if not path.exists():
            self.fallback_reason = f"graph_missing: {path}"
            return False
        try:
            from core.ladybug_builder import is_graph_current, load_manifest
            from core.ontology_loader import load_pack
            spec = load_pack(self.pack, base_dir=self.base_dir)
            ok, reason = is_graph_current(self.store, spec, path)
            if not ok:
                self.fallback_reason = f"graph_stale: {reason}"
                return False
            self.rel_tables = list(
                (load_manifest(path) or {}).get("rel_tables") or [])
            import ladybug as lb
            # 版本图不可变，只读打开，避免与重建/其他读会话互斥。
            self._lb_db = lb.Database(str(path), read_only=True)
            self._lb_conn = lb.Connection(self._lb_db)
            self._usable = True
            self.engine = "ladybug"
            return True
        except Exception as e:  # noqa: BLE001
            # 开图任何异常：回落语义轨，留原因（坏图不阻断研判）
            self.fallback_reason = (
                f"graph_open_failed: {type(e).__name__}: {str(e)[:160]}")
            self._close_lb()
            return False

    def _derive_path(self) -> Optional[Path]:
        """无显式路径时按 store.db_path 保守推导；推导不出返回 None。"""
        raw = getattr(self.store, "db_path", None)
        if not raw or raw == ":memory:":
            return None
        p = Path(str(raw))
        parts = p.parts
        if "cases" in parts:
            # /…/cases/<cid>/vN.duckdb → /…/cases/<cid>/graph/vN.lbug
            i = parts.index("cases")
            if len(parts) >= i + 3:
                return Path(*parts[:i + 2]) / "graph" / (p.stem + ".lbug")
        if p.name == "investigation.duckdb":
            if str(p.parent) in ("", "."):
                return self.FALLBACK_GRAPH
            return p.parent / "ladybug" / "investigation.lbug"
        return None

    def _allowed_links(self, edge_kinds: str) -> List[str]:
        return [
            name for name, (_s, _d, _k, kind) in SEMANTIC_EDGE_SPECS.items()
            if edge_kinds == "all" or kind == edge_kinds
        ]

    def _load_edges_via_cypher(self, edge_kinds: str) -> SemanticGraph:
        """白名单 REL 定向扫描 → 与语义轨同构的 SemanticGraph。

        白名单 REL 在图中缺表（构建时源 lnk_ 表缺失）按结构降级进 gaps，
        与 load_semantic_graph 的缺表语义逐字对齐（对拍时 gaps 也一致）。
        """
        g = SemanticGraph()
        for name in self._allowed_links(edge_kinds):
            rel = name.upper()
            if rel not in self.rel_tables:
                g.gaps.append({
                    "link": name, "table": f"lnk_{name}",
                    "reason": "graph_missing_rel: 版本图无该 REL 表"
                              "（构建时源语义表缺失）",
                })
                continue
            _src_col, _dst_col, key_col, kind = SEMANTIC_EDGE_SPECS[name]
            # REL 名为模块常量、LIMIT 为整型常量；入参只有开图时的代理键
            # 解析（resolve_subject 在调用方完成），Cypher 无主体名插值。
            res = self._lb_conn.execute(
                f"MATCH (a:Entity)-[r:{rel}]->(b:Entity) "
                f"RETURN a.pk, b.pk, r.edge_pk LIMIT {self._EDGE_SCAN_LIMIT}")
            while res.has_next():
                row = res.get_next()
                src, dst, edge_pk = row[0], row[1], row[2]
                if not src or not dst or not edge_pk:
                    continue
                g.add_edge(SEdge(
                    src=src, dst=dst, edge=name, edge_pk=str(edge_pk),
                    key_column=key_col, kind=kind))
            g.loaded_links.append(name)
        return g

    def view(self, edge_kinds: str = "all") -> SemanticGraph:
        """取本次查询的关系图（ladybug 主轨；任何一关失败回落语义轨）。"""
        if edge_kinds not in EDGE_KINDS:
            raise ValueError(f"edge_kinds={edge_kinds!r}，允许 {EDGE_KINDS}")
        if edge_kinds in self._cache:
            return self._cache[edge_kinds]
        g: Optional[SemanticGraph] = None
        if self._ensure_graph():
            try:
                g = self._load_edges_via_cypher(edge_kinds)
                if self.cross_check:
                    # 线上抽样对拍（方案 §1.3）：物理边集必须一致；不一致
                    # 弃图轨，以 semantic 轨结果为准并落 engine_mismatch。
                    g_sm = load_semantic_graph(
                        self.store, ctx=self.ctx, edge_kinds=edge_kinds)
                    sig_lb = self._physical_edge_sigs(g)
                    sig_sm = self._physical_edge_sigs(g_sm)
                    if sig_lb != sig_sm:
                        self.mismatch = {
                            "edge_kinds": edge_kinds,
                            "ladybug_only": sorted(sig_lb - sig_sm)[:20],
                            "semantic_only": sorted(sig_sm - sig_lb)[:20],
                            "ladybug_edge_count": len(sig_lb),
                            "semantic_edge_count": len(sig_sm),
                        }
                        self.fallback_reason = (
                            "engine_mismatch: 双轨物理边集不一致，"
                            "已以 semantic 轨为准"
                            f"（图独有 {len(sig_lb - sig_sm)}、"
                            f"语义独有 {len(sig_sm - sig_lb)}）")
                        self.engine = "semantic"
                        self._usable = False
                        self._close_lb()
                        g = g_sm
            except Exception as e:  # noqa: BLE001
                # 扫描期异常：本轨作废，回落语义轨（不抛给研判镜头）
                self.edge_scan_error = (
                    f"{type(e).__name__}: {str(e)[:160]}")
                self.engine = "semantic"
                self._usable = False
                self._close_lb()
                g = None
        if g is None:
            self.engine = "semantic"
            g = load_semantic_graph(self.store, ctx=self.ctx,
                                    edge_kinds=edge_kinds)
        self._cache[edge_kinds] = g
        return g

    def diagnostic(self, g: SemanticGraph) -> dict:
        """关系 Function diagnostics 载体（键与旧 _gap_diagnostic 兼容）。"""
        d: Dict[str, Any] = {
            "engine": self.engine,
            "loaded_links": list(g.loaded_links),
            "gaps": g.gaps,
            "is_degraded": bool(g.gaps),
        }
        if self.engine == "ladybug":
            d["graph_path"] = str(self.graph_path)
            d["rel_tables"] = list(self.rel_tables)
        else:
            d["engine_fallback_reason"] = (
                self.fallback_reason or "semantic_default")
        if self.edge_scan_error:
            d["edge_scan_error"] = self.edge_scan_error
        if self.mismatch is not None:
            d["engine_mismatch"] = self.mismatch
        return d

    def close(self) -> None:
        self._close_lb()

    def _close_lb(self) -> None:
        if self._lb_conn is not None:
            try:
                self._lb_conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._lb_conn = None
        self._lb_db = None

    def __enter__(self) -> "GraphGateway":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
