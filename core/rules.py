"""
core/rules.py
自然语言规则手册（Rulebook）执行器：rules.json 的确定性执行轨。

分工（红线）：
  - rule_text（自然语言判据）是规则主体：分析师写、LLM 编排时经 rule_list 读取解释、
    随线索落产物可审计；本模块**不解析自然语言**；
  - function + params 是唯一机器挂钩：本模块只调只读 Function + 命中判定，
    确定性来自 Function（SQL 白名单/py 注册实现），LLM 不碰计算、不写 SQL。

产出 findings 与 skills/xu_shi 原 finding 同构（候选虚处/依据/级别/source_rows），
额外携带 rule_id/rule_text/dimension/jian_types/assumption 供溯源与线索编排。
"""
from __future__ import annotations

from core.ontology_loader import load_pack


def catalog(pack: str = "default") -> list[dict]:
    """规则手册目录（MCP rule_list 消费，只读声明不碰数据）。"""
    return [r.to_dict() for r in load_pack(pack).rules.values()]


def run_rules(store, stage: str | None = "xu_shi", pack: str = "default") -> list[dict]:
    """按 stage 跑规则；命中的产出 findings。stage=None 跑全部阶段。"""
    from core.functions import FunctionExecutor

    spec = load_pack(pack)
    fx = FunctionExecutor(store, pack)
    findings: list[dict] = []
    for r in spec.rules.values():
        if stage and r.stage != stage:
            continue
        out = fx.invoke(r.function, dict(r.params))
        if r.hit_when == "result_hit":
            result = out.get("result") or {}
            hit = bool(result.get("hit"))
            source_rows = result.get("pairs") or []
            basis = result.get("basis") or r.basis_text
        else:
            rows = out.get("rows") or []
            hit = len(rows) > 0
            source_rows = rows
            basis = r.basis_text
        if not hit:
            continue
        findings.append({
            "rule_id": r.id,
            "候选虚处": r.title,
            "依据": basis,
            "级别": "待核实",
            "source_rows": source_rows,
            "rule_text": r.rule_text,
            "dimension": r.dimension,
            "jian_types": list(r.jian_types),
            "assumption": r.assumption,
        })
    return findings
