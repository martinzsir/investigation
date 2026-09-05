"""
core/object_set.py
REQ-029 ObjectSet 查询构造器：惰性链式 + AST 节点 + 不拼 SQL（注入防护）。

结构：
  Predicate AST：Eq/Ne/In/Gt/Lt/And/Or/Not（只接受 primitive 值，str 可含任意文本，
  注入防护在节点类型——不接受"包含 SQL 子句的特殊字符串"节点）。
  OntologyObjectSet(object_name, filters, hops, link_filters) frozen dataclass，
  链式 filter/search_around/with_link_filter 全部返回新副本（AC1 不可变）。
  构造过程不访问数据库（AC3 惰性），materialize() 才执行。
  explain() 打印为可读字符串（AC4）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# 允许作为 predicate literal 的值类型（原始 SQL 字符串注入不了）
_PRIMITIVE_TYPES = (str, int, float, bool, type(None))


# ----------------------------------------------------------------------
# Predicate AST
# ----------------------------------------------------------------------
class Predicate:
    """Predicate 基类（不可变、可 explain、可评估）。"""
    __slots__ = ()

    def explain(self, indent: int = 0) -> str:
        raise NotImplementedError

    def eval(self, row: dict) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class Eq(Predicate):
    field: str
    value: Any

    def __post_init__(self):
        if not isinstance(self.value, _PRIMITIVE_TYPES):
            raise TypeError(
                f"Eq.value 必须是原始类型 {_PRIMITIVE_TYPES}，"
                f"实际 {type(self.value).__name__}（AC5 注入防护：拒绝复杂对象/SQL 字符串节点）")

    def explain(self, indent=0):
        return "  " * indent + f"{self.field} == {self.value!r}"

    def eval(self, row: dict) -> bool:
        return row.get(self.field) == self.value


@dataclass(frozen=True)
class Ne(Predicate):
    field: str
    value: Any

    def __post_init__(self):
        if not isinstance(self.value, _PRIMITIVE_TYPES):
            raise TypeError(f"Ne.value 必须是原始类型")

    def explain(self, indent=0):
        return "  " * indent + f"{self.field} != {self.value!r}"

    def eval(self, row: dict) -> bool:
        return row.get(self.field) != self.value


@dataclass(frozen=True)
class In(Predicate):
    field: str
    values: tuple[Any, ...]

    def __post_init__(self):
        vs = tuple(self.values)
        for v in vs:
            if not isinstance(v, _PRIMITIVE_TYPES):
                raise TypeError(f"In.values 每个元素必须是原始类型")
        object.__setattr__(self, "values", vs)

    def explain(self, indent=0):
        return "  " * indent + f"{self.field} IN {list(self.values)}"

    def eval(self, row: dict) -> bool:
        return row.get(self.field) in self.values


@dataclass(frozen=True)
class Gt(Predicate):
    field: str
    value: Any

    def __post_init__(self):
        if not isinstance(self.value, (int, float)):
            raise TypeError("Gt.value 必须是数值")

    def explain(self, indent=0):
        return "  " * indent + f"{self.field} > {self.value!r}"

    def eval(self, row: dict) -> bool:
        try:
            return bool(row.get(self.field) > self.value)
        except TypeError:
            return False


@dataclass(frozen=True)
class Lt(Predicate):
    field: str
    value: Any

    def __post_init__(self):
        if not isinstance(self.value, (int, float)):
            raise TypeError("Lt.value 必须是数值")

    def explain(self, indent=0):
        return "  " * indent + f"{self.field} < {self.value!r}"

    def eval(self, row: dict) -> bool:
        try:
            return bool(row.get(self.field) < self.value)
        except TypeError:
            return False


class And(Predicate):
    __slots__ = ("children",)

    def __init__(self, *args):
        """允许 And(p1, p2, p3) 或 And((p1,p2)) 两种写法。"""
        if len(args) == 1 and isinstance(args[0], (tuple, list)):
            self.children = tuple(args[0])
        else:
            self.children = tuple(args)
        for c in self.children:
            if not isinstance(c, Predicate):
                raise TypeError("And.children 必须都是 Predicate（AC5 注入防护）")

    def __eq__(self, other):
        return isinstance(other, And) and self.children == other.children

    def __hash__(self):
        return hash(("And", self.children))

    def explain(self, indent=0):
        pad = "  " * indent
        lines = [pad + "AND("]
        for c in self.children:
            lines.append(c.explain(indent + 1))
        lines.append(pad + ")")
        return "\n".join(lines)

    def eval(self, row):
        return all(c.eval(row) for c in self.children)


class Or(Predicate):
    __slots__ = ("children",)

    def __init__(self, *args):
        if len(args) == 1 and isinstance(args[0], (tuple, list)):
            self.children = tuple(args[0])
        else:
            self.children = tuple(args)
        for c in self.children:
            if not isinstance(c, Predicate):
                raise TypeError("Or.children 必须都是 Predicate（AC5 注入防护）")

    def __eq__(self, other):
        return isinstance(other, Or) and self.children == other.children

    def __hash__(self):
        return hash(("Or", self.children))

    def explain(self, indent=0):
        pad = "  " * indent
        lines = [pad + "OR("]
        for c in self.children:
            lines.append(c.explain(indent + 1))
        lines.append(pad + ")")
        return "\n".join(lines)

    def eval(self, row):
        return any(c.eval(row) for c in self.children)


class Not(Predicate):
    __slots__ = ("child",)

    def __init__(self, child):
        if not isinstance(child, Predicate):
            raise TypeError("Not.child 必须是 Predicate（AC5 注入防护）")
        self.child = child

    def __eq__(self, other):
        return isinstance(other, Not) and self.child == other.child

    def __hash__(self):
        return hash(("Not", self.child))

    def explain(self, indent=0):
        pad = "  " * indent
        return pad + "NOT(\n" + self.child.explain(indent + 1) + "\n" + pad + ")"

    def eval(self, row):
        return not self.child.eval(row)


# ----------------------------------------------------------------------
# ObjectSet
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class OntologyObjectSet:
    object_name: str
    filters: tuple[Predicate, ...] = ()
    hops: tuple[str, int] | None = None   # (link_name, max_hops)
    link_filters: tuple[Predicate, ...] = ()

    # ---- 构造期（完全不碰 store，AC3） ----
    def filter(self, *predicates: Predicate) -> "OntologyObjectSet":
        for p in predicates:
            if not isinstance(p, Predicate):
                raise TypeError("filter() 只接受 Predicate AST 节点（AC5 注入防护）")
        return replace(self, filters=self.filters + tuple(predicates))

    def search_around(self, link_name: str, max_hops: int) -> "OntologyObjectSet":
        if not isinstance(max_hops, int) or max_hops < 1 or max_hops > 10:
            raise ValueError(f"search_around max_hops 必须是 1..10 整数，实际 {max_hops}")
        return replace(self, hops=(link_name, max_hops))

    def with_link_filter(self, *predicates: Predicate) -> "OntologyObjectSet":
        for p in predicates:
            if not isinstance(p, Predicate):
                raise TypeError("with_link_filter() 只接受 Predicate 节点")
        return replace(self, link_filters=self.link_filters + tuple(predicates))

    # ---- 元数据 ----
    @classmethod
    def from_objects(cls, object_name: str) -> "OntologyObjectSet":
        return cls(object_name=object_name)

    def explain(self) -> str:
        lines = [f"OntologyObjectSet(obj_{self.object_name})"]
        if self.filters:
            lines.append("filters:")
            for f in self.filters:
                lines.append(f.explain(1))
        if self.hops:
            lname, n = self.hops
            lines.append(f"search_around({lname}, max_hops={n})")
        if self.link_filters:
            lines.append("link_filters:")
            for f in self.link_filters:
                lines.append(f.explain(1))
        return "\n".join(lines)

    # ---- 执行期 ----
    def materialize(self, store, pack: str = "default") -> list[dict]:
        """触发实际查询：gateway.objects → predicate 过滤 → search_around 邻接扩展（环检测 AC2）。

        结果返回对象行列表（不拼 SQL，所有过滤走 Python 层 Predicate.eval）。
        """
        from core.gateway import OntologyReadGateway
        gw = OntologyReadGateway(store.conn, pack)
        rows = gw.objects(self.object_name)
        # 对象过滤
        if self.filters:
            flt = And(*self.filters)
            rows = [r for r in rows if flt.eval(r)]
        # search_around 邻接扩展（max_hops 内 DFS，AC2 环图有限步停止）
        if self.hops:
            lname, max_hops = self.hops
            try:
                edges = gw.links(lname)
            except Exception:
                edges = []
            seen = set()
            extra_rows: list[dict] = []
            # 找到对象 pk 列
            try:
                from core.ontology_loader import load_pack
                pk_map = {o.name: o.pk for o in load_pack(pack).objects}
                pk_col = pk_map.get(self.object_name, "name_raw")
            except Exception:
                pk_col = "name_raw"
            # from_col / to_col：链接 from_xxx / to_xxx，先拿第一行判定
            from_col = to_col = None
            if edges:
                cols = list(edges[0].keys())
                froms = [c for c in cols if c.startswith("from_")]
                tos = [c for c in cols if c.startswith("to_")]
                from_col = froms[0] if froms else "from_raw"
                to_col = tos[0] if tos else "to_raw"
            frontier_pks = [r.get(pk_col) for r in rows]
            for _ in range(max_hops):
                next_frontier = []
                for e in edges:
                    a, b = e.get(from_col), e.get(to_col)
                    if a in frontier_pks and b not in seen:
                        # 应用 link_filter
                        if self.link_filters:
                            lf = And(*self.link_filters)
                            if not lf.eval(e):
                                continue
                        seen.add(b)
                        next_frontier.append(b)
                        extra_rows.append({"_from_search_around": lname,
                                           pk_col: b, "_hop_edge": e})
                frontier_pks = next_frontier
                if not frontier_pks:
                    break
            rows = list(rows) + extra_rows
        return rows
