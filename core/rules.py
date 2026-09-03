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
REQ-025：支持 exclusive_group 互斥抑制，末段调用 _suppress_overlaps()。
REQ-027：每条 finding 附 threshold_method / threshold_value / is_degraded。
"""
from __future__ import annotations

from core.ontology_loader import load_pack


def catalog(pack: str = "default") -> list[dict]:
    """规则手册目录（MCP rule_list 消费，只读声明不碰数据）。"""
    return [r.to_dict() for r in load_pack(pack).rules.values()]


def _evaluate_single(rule, fx, out):
    """单规则评估 → (hit: bool, source_rows, basis)。"""
    if rule.hit_when == "result_hit":
        result = out.get("result") or {}
        return (bool(result.get("hit")),
                result.get("pairs") or [],
                result.get("basis") or rule.basis_text)
    rows = out.get("rows") or []
    return (len(rows) > 0, rows, rule.basis_text)


def _suppress_overlaps(findings: list[dict], rules: dict) -> list[dict]:
    """REQ-025 AC1/AC3：exclusive_group 内 primary 命中 → 非 primary 入 suppressed_log。

    返回 findings（主列表，已抑制项移除）；每条原 finding 挂 suppressed_log 列表。
    """
    if not findings:
        return findings
    by_id = {f["rule_id"]: f for f in findings}
    # step 1: 按 group 分 primary_hit 组号集合
    groups_primary_hit: set[str] = set()
    for f in findings:
        r = rules.get(f["rule_id"])
        if r and r.exclusive_group and r.primary_rule:
            groups_primary_hit.add(r.exclusive_group)
    # step 2: 对每条 finding，组内 primary 命中且本 finding 非 primary 则抑制
    suppressed_log: list[dict] = []
    kept: list[dict] = []
    for f in findings:
        r = rules.get(f["rule_id"])
        if (r and r.exclusive_group in groups_primary_hit
                and not r.primary_rule
                and r.overlap_resolution == "drop_if_primary_hit"):
            # 找到本 group 里命中的 primary rule
            primary_id = next((
                rr.id for gid, rr in rules.items()
                if rr.exclusive_group == r.exclusive_group
                and rr.primary_rule and rr.id in by_id
            ), None)
            entry = {
                "rule_id": f["rule_id"],
                "suppressed_by_group": r.exclusive_group,
                "suppressed_by_rule": primary_id,
                "reason": f"exclusive_group='{r.exclusive_group}' 已由 primary="
                          f"{primary_id} 命中；overlap_resolution={r.overlap_resolution}",
                "级别": f["级别"],
                "候选虚处": f["候选虚处"],
                "source_rows": f.get("source_rows") or [],
            }
            suppressed_log.append(entry)
            f["_suppressed"] = True
            f["suppressed_reason"] = entry["reason"]
        else:
            kept.append(f)
    # step 3: 每条保留 finding 附整体 suppressed_log（审计一致，产物完整）
    for f in kept:
        # 副本（避免重复引用）
        f["suppressed_log"] = [dict(x) for x in suppressed_log]
    return kept


def run_rules(store, stage: str | None = "xu_shi", pack: str = "default",
              rule_ids: "set[str] | list[str] | None" = None) -> list[dict]:
    """按 stage 跑规则；命中的产出 findings。stage=None 跑全部阶段。

    rule_ids 非空时只重算指定规则（REQ-016 增量重算：无关规则结果不变）。
    """
    from core.functions import FunctionExecutor
    # REQ-027 阈值策略（可选：没装 threshold 模块时兼容）
    try:
        from core.threshold import resolve_rule_params
        _has_threshold = True
    except Exception:
        _has_threshold = False

    spec = load_pack(pack)
    fx = FunctionExecutor(store, pack)
    wanted = set(rule_ids) if rule_ids else None
    findings: list[dict] = []
    for r in spec.rules.values():
        if stage and r.stage != stage:
            continue
        if wanted is not None and r.id not in wanted:
            continue
        params = dict(r.params)
        if _has_threshold:
            params, method, value, degraded = resolve_rule_params(
                store, r.id, params)
        else:
            method, value, degraded = "absolute_hardcoded", None, False
        out = fx.invoke(r.function, params)
        hit, source_rows, basis = _evaluate_single(r, fx, out)
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
            # REQ-027：阈值元数据
            "threshold_method": method,
            "threshold_value": value,
            "is_degraded": degraded,
        })
    findings = _suppress_overlaps(findings, spec.rules)
    return findings
