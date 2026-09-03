"""
core/rule_dsl.py
REQ-026 规则 DSL（组合与时序）。AST-only，绝不拼 SQL（自由 SQL 禁令）。

AST 节点：
  {"all": [...]} / {"any": [...]} / {"not": node} / {"rule": "RID"}
  / {"rule": "RID", "within_days": N}

流程：
  parse(ast_dict)   → validate（rule 存在、depth ≤ 5、within_days 正整数）并返回 DslNode 树
  DslNode.explain() → 可读伪代码计划（AC6）
  DslNode.evaluate(store, pack) → 先取叶子 rules 对应的 findings 集合，按 all/any/not 组合；
                                  within_days 取 finding source_rows 的日期 min/max 做窗口比较。
                                  返回 {hit: bool, findings:[...], plan: str}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date, datetime as _dt
from typing import Any

from core.ontology_loader import load_pack

MAX_DEPTH = 5
DATE_KEYS = ("日期", "中标公示日", "举报日期", "发布日期", "date", "DATE", "day")


def _parse_date(v: Any) -> _date | None:
    if v is None:
        return None
    if isinstance(v, _date):
        return v
    if isinstance(v, _dt):
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _finding_date_range(f: dict) -> tuple[_date | None, _date | None]:
    """从 finding.source_rows 扫描日期列；返回 (min_d, max_d)；缺则 (None,None)。"""
    rows = f.get("source_rows") or []
    dates = []
    for r in rows:
        if isinstance(r, dict):
            for k in DATE_KEYS:
                if k in r:
                    d = _parse_date(r[k])
                    if d is not None:
                        dates.append(d)
                        break
    if not dates:
        return None, None
    return min(dates), max(dates)


# ----------------------------------------------------------------------
# AST 节点
# ----------------------------------------------------------------------
class DslNode:
    def depth(self) -> int:
        raise NotImplementedError
    def leaf_rules(self) -> set[str]:
        raise NotImplementedError
    def explain(self, indent: int = 0) -> str:
        raise NotImplementedError
    def evaluate(self, store, pack: str,
                 finding_by_id: dict[str, list[dict]]) -> bool:
        raise NotImplementedError


@dataclass
class RuleRef(DslNode):
    rule_id: str
    within_days: int | None = None

    def depth(self): return 1
    def leaf_rules(self): return {self.rule_id}

    def explain(self, indent=0):
        pad = "  " * indent
        if self.within_days:
            return f"{pad}WITHIN({self.rule_id}, {self.within_days}d)"
        return f"{pad}{self.rule_id}"

    def evaluate(self, store, pack, finding_by_id) -> bool:
        findings = finding_by_id.get(self.rule_id) or []
        if not findings:
            return False
        if self.within_days is None:
            return True  # 有无命中；过滤在组合层
        # within_days：把每个 finding 的日期范围暂存到全局供组合层 cross_within 检查
        # 本节点只是"是否命中规则本体"。within_days 语义在组合节点内交叉检查。
        return True


@dataclass
class All(DslNode):
    children: list[DslNode] = field(default_factory=list)
    def depth(self): return 1 + max((c.depth() for c in self.children), default=0)
    def leaf_rules(self):
        s = set()
        for c in self.children: s |= c.leaf_rules()
        return s
    def explain(self, indent=0):
        pad = "  " * indent
        return pad + "AND(\n" + "\n".join(c.explain(indent+1) for c in self.children) + "\n" + pad + ")"
    def evaluate(self, store, pack, fb):
        return all(c.evaluate(store, pack, fb) for c in self.children)


@dataclass
class Any(DslNode):
    children: list[DslNode] = field(default_factory=list)
    def depth(self): return 1 + max((c.depth() for c in self.children), default=0)
    def leaf_rules(self):
        s = set()
        for c in self.children: s |= c.leaf_rules()
        return s
    def explain(self, indent=0):
        pad = "  " * indent
        return pad + "OR(\n" + "\n".join(c.explain(indent+1) for c in self.children) + "\n" + pad + ")"
    def evaluate(self, store, pack, fb):
        return any(c.evaluate(store, pack, fb) for c in self.children)


@dataclass
class Not(DslNode):
    child: DslNode
    def depth(self): return 1 + self.child.depth()
    def leaf_rules(self): return self.child.leaf_rules()
    def explain(self, indent=0):
        pad = "  " * indent
        return pad + "NOT(\n" + self.child.explain(indent+1) + "\n" + pad + ")"
    def evaluate(self, store, pack, fb):
        return not self.child.evaluate(store, pack, fb)


# ----------------------------------------------------------------------
# parse / evaluate 入口
# ----------------------------------------------------------------------
def _parse_node(obj: Any, known_rules: set[str], path: str) -> DslNode:
    if not isinstance(obj, dict):
        raise ValueError(f"DSL AST 节点必须是 dict（路径 {path}）")
    keys = set(obj.keys())
    if "rule" in keys:
        rid = obj["rule"]
        if rid not in known_rules:
            raise ValueError(
                f"DSL rule_ref '{rid}' 未在规则手册（路径 {path}），可用 {sorted(known_rules)}")
        wd = obj.get("within_days")
        if wd is not None:
            if not isinstance(wd, int) or wd <= 0:
                raise ValueError(
                    f"DSL within_days 必须是正整数（路径 {path}.within_days={wd!r}）")
        return RuleRef(rid, wd)
    if "all" in keys:
        arr = obj["all"]
        if not isinstance(arr, list) or not arr:
            raise ValueError(f"DSL all 必须是非空列表（路径 {path}）")
        return All([_parse_node(x, known_rules, f"{path}.all[{i}]")
                    for i, x in enumerate(arr)])
    if "any" in keys:
        arr = obj["any"]
        if not isinstance(arr, list) or not arr:
            raise ValueError(f"DSL any 必须是非空列表（路径 {path}）")
        return Any([_parse_node(x, known_rules, f"{path}.any[{i}]")
                    for i, x in enumerate(arr)])
    if "not" in keys:
        return Not(_parse_node(obj["not"], known_rules, f"{path}.not"))
    raise ValueError(f"DSL AST 未知类型，键={sorted(keys)}；期望 all/any/not/rule（路径 {path}）")


def parse(ast_dict: dict, pack: str = "default") -> DslNode:
    """解析并校验 DSL。抛 ValueError：
       - 超深度（AC5 MAX_DEPTH=5）；
       - rule_ref 指向不存在规则；
       - within_days 非正整数；
       - 未知 AST 键。
    """
    known = set(load_pack(pack).rules.keys())
    node = _parse_node(ast_dict, known, "<root>")
    d = node.depth()
    if d > MAX_DEPTH:
        raise ValueError(
            f"DSL AST 深度 {d} 超过 MAX_DEPTH={MAX_DEPTH}（防止表达式炸弹 AC5）")
    return node


class CallPlan:
    """DSL 编译后的调用计划：leaf_rules（传给 run_rules 的 ID 集合） + 可读 explain。"""
    def __init__(self, node: DslNode):
        self.root = node
        self.leaf_ids: list[str] = sorted(node.leaf_rules())
        self.explain_text = node.explain()
    def explain(self) -> str:
        return self.explain_text


def compile(node: DslNode) -> CallPlan:
    return CallPlan(node)


def _cross_check_within_days(node: DslNode, finding_by_id: dict[str, list[dict]]) -> bool:
    """AC4：within_days=N 的 RuleRef 必须与其"兄弟节点"（同 all 下其他 finding）的日期距离 ≤N。

    实现：对每个 All 节点，收集其中 RuleRef 且有 within_days=K 的 ruleA，以及其同层
    所有其他 RuleRef ruleB，找 A.dates 与 B.dates 间最小日差；任一分组不满足即 False。
    """
    def walk(n: DslNode) -> bool:
        if isinstance(n, All):
            # 抽取本层 RuleRef + 其 finding date range
            refs_with_range = []
            for c in n.children:
                if isinstance(c, RuleRef):
                    flist = finding_by_id.get(c.rule_id) or []
                    rmin, rmax = None, None
                    for f in flist:
                        mn, mx = _finding_date_range(f)
                        if mn and (rmin is None or mn < rmin): rmin = mn
                        if mx and (rmax is None or mx > rmax): rmax = mx
                    refs_with_range.append((c, rmin, rmax))
            # 对每对 (a, b) 其中 a.within_days 非 None：检查最小日差 ≤ a.within_days
            for i, (a, a_mn, a_mx) in enumerate(refs_with_range):
                if a.within_days is None:
                    continue
                # AC4：日期缺失——保守判为"不通过"（绝不把缺口当通过）
                if a_mn is None:
                    return False
                for j, (b, b_mn, b_mx) in enumerate(refs_with_range):
                    if i == j:
                        continue
                    if b_mn is None:
                        return False
                    # 日差：两区间最小距离
                    if a_mn <= b_mx and b_mn <= a_mx:  # 重叠
                        mindiff = 0
                    elif a_mx < b_mn:
                        mindiff = (b_mn - a_mx).days
                    else:
                        mindiff = (a_mn - b_mx).days
                    if mindiff > a.within_days:
                        return False
            # 下钻
            return all(walk(c) for c in n.children)
        if isinstance(n, Any):
            return any(walk(c) for c in n.children)
        if isinstance(n, Not):
            return walk(n.child)
        return True
    return walk(node)


def evaluate(store, node: DslNode, pack: str = "default") -> dict:
    """运行 DSL：先 run_rules 取叶 finding，再组合 + within_days 窗。

    返回 {hit, findings (命中的原始 finding 子集), plan, degraded_note}。
    """
    plan = compile(node)
    # run_rules 只跑叶规则（避免无关开销）
    from core.rules import run_rules
    all_findings = run_rules(store, stage=None, pack=pack, rule_ids=plan.leaf_ids)
    by_id: dict[str, list[dict]] = {}
    for f in all_findings:
        by_id.setdefault(f["rule_id"], []).append(f)
    # 先组合
    h = node.evaluate(store, pack, by_id)
    if not h:
        return {"hit": False, "findings": [], "plan": plan.explain(),
                "degraded_note": None}
    # 再查 within_days 窗口（日期缺 => 视为"日期未知"，不通过 AC4 保守处理）
    ok = _cross_check_within_days(node, by_id)
    if not ok:
        return {"hit": False, "findings": [], "plan": plan.explain(),
                "degraded_note": "within_days 不满足（或至少一条 finding 缺可用日期列，DSL 日期未知默认不通过 AC4）"}
    # 返回参与命中的原始 findings（叶级所有命中）
    involved = []
    seen = set()
    for rid in plan.leaf_ids:
        for f in by_id.get(rid, []):
            key = (f["rule_id"], f.get("依据"), len(f.get("source_rows") or []))
            if key in seen: continue
            seen.add(key)
            involved.append(f)
    return {"hit": True, "findings": involved, "plan": plan.explain(),
            "degraded_note": None}
