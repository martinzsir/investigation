"""
core/semantic_graph_export.py
声明驱动：OntologyPack（links.json endpoints + objects.json key/name_property）
→ 属性图行集（节点 / 边），同时供给两条出口：

  1. core/ladybug_builder.py   语义层 → CSV → LadybugDB 建图（代理键节点/边）
  2. scripts/export_ladybug.py 审计 CSV 导出（人读点名，历史 golden 兼容）

红线：
  - 不写业务 SQL——节点取 key.column/name_property，边取 endpoints 声明列，
    新增 link 只需在 links.json 声明 endpoints，本模块零改动即出节点/边；
  - 边端点一律走 endpoints.ref 指向的代理键（lnk 表上的 *_id 列），不匹配
    原始文本；LEFT JOIN 未归一（端点 NULL）的边不进图；
  - runtime link（decision_for / image_for_*）无 endpoints，按端点对象主键列
    合成声明（读到表才建，读不到跳过），不为建图触发任何写动作。
"""
from __future__ import annotations

from dataclasses import dataclass

# properties 值类型 → LadybugDB(Kùzu) 列类型。边属性只承载，不参与建图端点评定。
KUZU_TYPE = {
    "string": "STRING",
    "integer": "INT64",
    "decimal": "DOUBLE",
    "date": "DATE",
    "boolean": "BOOLEAN",
}


@dataclass(frozen=True)
class EdgeDecl:
    """一条 link 的属性图投影声明（完全由 links.json 推导，无业务约定）。"""
    link: str
    src_obj: str
    dst_obj: str
    src_col: str          # lnk 表上的起点代理键列
    dst_col: str          # lnk 表上的终点代理键列
    edge_key: str | None  # lnk 行键列（证据引用）；None → src#dst 合成
    attr_cols: tuple[str, ...]         # 边属性 + extra 透传列（去重保序）
    attr_types: dict[str, str]        # attr 列 → Kùzu 类型（extra 一律 STRING）
    runtime: bool

    @property
    def table(self) -> str:
        return f"lnk_{self.link}"

    @property
    def rel_table(self) -> str:
        """Kùzu REL 表名 = link 名大写（标识符合法：蛇形字母数字）。"""
        return self.link.upper()


def _q(ident: str) -> str:
    """DuckDB 双引号界定标识符。"""
    return f'"{ident}"'


def object_key_column(otype) -> str:
    return otype.key.column if getattr(otype, "key", None) else otype.pk


def edge_decls(pack) -> list[EdgeDecl]:
    """从 pack 声明生成全部可入图的边投影（pack.links 顺序，稳定）。

    - 编译期 link：读 endpoints（两端点 ref 必备；缺 ref 跳过——文本列不成实体边）；
    - runtime link：无 endpoints，按 from_obj/to_obj 主键列合成（无属性/行键）。
    """
    objs = {o.name: o for o in pack.objects}
    out: list[EdgeDecl] = []
    for l in pack.links:
        ep = l.endpoints or {}
        if ep.get("from") and ep.get("to"):
            fref = ep["from"].get("ref")
            tref = ep["to"].get("ref")
            if not fref or not tref:
                # 端点是裸文本列（无代理键）：属性图无法挂实体节点，跳过。
                continue
            prop_cols = list(l.properties.keys())
            extra_cols = [c for c in ep.get("extra", []) if c not in prop_cols]
            attrs = tuple(prop_cols + extra_cols)
            types = {c: KUZU_TYPE.get(l.properties[c], "STRING")
                     for c in prop_cols}
            types.update({c: "STRING" for c in extra_cols})
            out.append(EdgeDecl(
                link=l.name, src_obj=fref["object"], dst_obj=tref["object"],
                src_col=ep["from"]["col"], dst_col=ep["to"]["col"],
                edge_key=ep.get("edge_key"), attr_cols=attrs, attr_types=types,
                runtime=False))
        elif l.runtime:
            src_o, dst_o = objs.get(l.from_obj), objs.get(l.to_obj)
            if src_o is None or dst_o is None:
                continue
            out.append(EdgeDecl(
                link=l.name, src_obj=l.from_obj, dst_obj=l.to_obj,
                src_col=object_key_column(src_o), dst_col=object_key_column(dst_o),
                edge_key=None, attr_cols=(), attr_types={}, runtime=True))
    return out


def graph_object_types(pack) -> list[str]:
    """图节点对象类型：全部边端点引用到的对象（pack.objects 顺序去重）。"""
    decls = edge_decls(pack)
    referenced = set()
    for d in decls:
        referenced.add(d.src_obj)
        referenced.add(d.dst_obj)
    return [o.name for o in pack.objects if o.name in referenced]


def node_sql(obj_type: str, pack, table_name: str | None = None) -> str | None:
    """某对象类型 → 节点行集 SQL：(pk, type, name)。对象未声明返回 None。"""
    o = next((x for x in pack.objects if x.name == obj_type), None)
    if o is None:
        return None
    tbl = table_name or f"obj_{obj_type}"
    pk_col = object_key_column(o)
    return (
        f"SELECT {_q(pk_col)} AS pk, '{obj_type}' AS type, "
        f"{_q(o.name_property)} AS name FROM {_q(tbl)} "
        f"WHERE {_q(pk_col)} IS NOT NULL"
    )


def declared_node_union_sql(pack, quote_table=None) -> str:
    """全声明节点 UNION ALL（name, type）——供 export_ladybug 审计 CSV。

    quote_table 可选回调把 obj_<type> 映射成实际表名（RuntimeContext 换包）。
    """
    parts = []
    for t in graph_object_types(pack):
        o = next(x for x in pack.objects if x.name == t)
        tbl = quote_table(t) if quote_table else f"obj_{t}"
        parts.append(
            f"SELECT {_q(o.name_property)} AS name, '{t}' AS type "
            f"FROM {_q(tbl)} WHERE {_q(o.name_property)} IS NOT NULL")
    return " UNION ALL ".join(parts)


def edge_sql(decl: EdgeDecl) -> str:
    """边投影 SQL：edge_pk, src_pk, dst_pk [, 边属性/extra…]。

    edge_pk 优先取声明的行键列（endpoints.edge_key），缺省用 src#dst 合成
    （time_window / runtime 边：端点对本身唯一）。端点 NULL（LEFT JOIN 未归一）
    的边不进图。
    """
    cols = [f"{_q(decl.src_col)} AS src_pk", f"{_q(decl.dst_col)} AS dst_pk"]
    if decl.edge_key:
        cols.append(f"{_q(decl.edge_key)} AS edge_pk")
    else:
        cols.append(
            f"CONCAT_WS('#', {_q(decl.src_col)}, {_q(decl.dst_col)}) AS edge_pk")
    for c in decl.attr_cols:
        cols.append(f"{_q(c)}")
    where = (f"WHERE {_q(decl.src_col)} IS NOT NULL "
             f"AND {_q(decl.dst_col)} IS NOT NULL")
    return f"SELECT {', '.join(cols)} FROM {_q(decl.table)} l {where}"


def edge_audit_sql(decl: EdgeDecl, pack) -> str:
    """审计导出口径：端点代理键 JOIN obj_* 解析为点名（from_id/to_id）+ 透传列。

    与历史 scripts/export_ladybug._edge_sql 同语义（golden 兼容），仅改为由
    EdgeDecl + objects 声明驱动。
    """
    objs = {o.name: o for o in pack.objects}
    joins: list[str] = []
    selects: list[str] = []
    for i, (obj_name, key_col) in enumerate(
            ((decl.src_obj, decl.src_col), (decl.dst_obj, decl.dst_col)), start=1):
        o = objs[obj_name]
        alias = f"n{i}"
        joins.append(
            f"JOIN obj_{obj_name} {alias} "
            f"ON {alias}.{object_key_column(o)} = l.{key_col}")
        selects.append(f"{alias}.{o.name_property} AS {'from_id' if i == 1 else 'to_id'}")
    cols = list(decl.attr_cols)
    return (
        f"SELECT {', '.join(selects + [f'l.{c}' for c in cols])} "
        f"FROM {decl.table} l " + " ".join(joins)).strip()
