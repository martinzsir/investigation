"""
core/anomaly_channel.py
REQ-G-019 异常线索通道：把"我们没能看到什么"表达成平行于正常线索的待核实条目。

设计（见 .trae/documents/REQ-G/REQ-G.md REQ-G-019 与实施计划 Step 19）：
  - 从 run_diagnostic 中可转化的静默/缺口类诊断生成异常线索：
      · rule_zero_hit 且 zero_type=empty_result_suspect（疑似失效/被规避；
        data_absent/config_missing/clean_scan 不转化——缺数据与显式 clean 不应提示"空白异常"）
      · function_empty_degraded（函数空转/结构降级）
      · coverage_gap（庙算维度覆盖缺口）
  - 异常线索与正常 finding **同构**（rule_id/候选虚处/依据/级别/source_rows/dimension/...），
    source_rows 恒为空列表（无证据行），另携 diagnostic_ids 溯源回 run_diagnostic。
  - **强制标记（不可被覆盖）**："级别" 恒为 "待核实"、needs_human_review 恒为 True、
    is_anomaly 恒为 True。机器不擅自下"排除/clean"结论——只有人能区分"数据缺失"与"刻意规避"。

红线（REQ-G-019 AC3）：异常线索**绝不**参与五间交叉等级计算、绝不升格。
  交叉等级为"非空即命中"，若异常也贡献命中，则"缺得越多等级越高"。
  结构上：异常线索只是内存/产物中的 dict，绝不写入 obj_*/lnk_* 语义表；
  任何混合线索流在送入交叉/升格前必须经 non_anomaly()/partition() 过滤。
"""
from __future__ import annotations

from typing import Any

from core.run_health import get_health

# 可转化为异常线索的诊断类型；rule_zero_hit 仅 empty_result_suspect 子类转化
_ANOMALY_KINDS = {"rule_zero_hit", "function_empty_degraded", "coverage_gap"}
_WHOLE_CASE = "全局"  # 无明确主体的缺口归属"全局"，仍按主体聚合输出


def _is_eligible(row: dict) -> bool:
    kind = row.get("kind")
    if kind not in _ANOMALY_KINDS:
        return False
    if kind == "rule_zero_hit":
        d = row.get("detail") if isinstance(row.get("detail"), dict) else {}
        return d.get("zero_type") == "empty_result_suspect"
    return True


def _subject_of(row: dict) -> str:
    d = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    return str(d.get("subject") or _WHOLE_CASE)


def _dimension_of(row: dict) -> str | None:
    d = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    dim = d.get("dimension")
    if dim:
        return str(dim)
    if row.get("kind") == "coverage_gap":
        missing = d.get("missing") or d.get("declared_missing")
        if isinstance(missing, list) and missing:
            return "、".join(str(x) for x in missing)
    return None


def clue_from_diagnostic(row: dict) -> dict | None:
    """把一条 run_diagnostic 行转成与 finding 同构的异常线索；不可转化返回 None。"""
    if not _is_eligible(row):
        return None
    kind = row["kind"]
    d = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    rid = row.get("run_id", "")
    seq = row.get("seq", "?")
    diag_id = f"{rid}#{seq}"
    subject = _subject_of(row)

    rule_id = f"ANOMALY-{kind}-{seq}"
    if kind == "rule_zero_hit":
        r = d.get("rule_id", "?")
        title = f"[异常·待核实] 规则 {r} 零命中（有扫描数据，疑似失效或被规避）"
        basis = row.get("reason") or f"规则 {r} 本运行零命中且存在扫描数据"
        rule_text = (f"规则 {r} 在本运行零命中、但输入侧存在扫描数据（empty_result_suspect）。"
                     "此为'空白'信号：可能规则失效，也可能对象刻意规避；"
                     "机器不判定为无异常（clean），须人工区分数据缺失与规避。")
        out_dim = _dimension_of(row)
    elif kind == "function_empty_degraded":
        fn = d.get("function", "?")
        title = f"[异常·待核实] 计算 {fn} 空转/结构降级"
        basis = row.get("reason") or f"函数 {fn} 输入非零而输出为空或结构缺失"
        rule_text = (f"计算 {fn} 出现输入非零但输出为空/结构降级（function_empty_degraded）。"
                     "该维度结论本次不可得，缺口须人工核实，不以缺失误判为正常。")
        out_dim = _dimension_of(row)
    else:  # coverage_gap
        missing = d.get("missing") or d.get("declared_missing") or []
        title = f"[异常·待核实] 维度覆盖缺口：{'、'.join(map(str, missing)) or '未知'}"
        basis = row.get("reason") or "庙算维度覆盖不足，部分维度无任何线索支撑"
        rule_text = ("庙算五维覆盖出现缺口（coverage_gap）：被声明的侦查维度无对应线索支撑。"
                     "覆盖缺口不等于无异常，须人工补充侦查或确认数据边界。")
        out_dim = _dimension_of(row)

    return {
        # ---- 与正常 finding 同构字段 ----
        "rule_id": rule_id,
        "候选虚处": title,
        "依据": basis,
        "级别": "待核实",            # 强制：异常线索恒为待核实
        "source_rows": [],           # 同构携带；异常无证据行，溯源走 diagnostic_ids
        "rule_text": rule_text,
        "dimension": out_dim,
        "jian_types": [],
        "assumption": "",
        "is_degraded": True,
        # ---- G-019 强制标记 ----
        "is_anomaly": True,
        "needs_human_review": True,
        # ---- 溯源 / 归类 ----
        "anomaly_kind": kind,
        "diagnostic_ids": [diag_id],
        "subject": subject,
        **({"zero_type": d.get("zero_type")} if kind == "rule_zero_hit" else {}),
    }


def emit_anomaly_clues(health: Any, *, run_id: str | None = None,
                       record: bool = True) -> list[dict]:
    """扫描 run_diagnostic，把可转化诊断聚合成异常线索（按主体去重合并）。

    返回异常线索列表（finding 同构）。record=True 时落一条 anomaly_clue_emitted 诊断留痕。
    绝不写入 obj_*/lnk_* 语义表——异常不参与交叉（红线）。
    """
    h = get_health(health)
    rows = h.rows(run_id) if hasattr(h, "rows") else []
    clues: list[dict] = []
    for row in rows:
        c = clue_from_diagnostic(row)
        if c is not None:
            clues.append(c)
    merged = _merge_by_subject(clues)
    if record and merged:
        h.record("anomaly_clue_emitted", "warning",
                 source="anomaly_channel",
                 reason=f"产出 {len(merged)} 条异常线索（待核实，不参与交叉）",
                 count=len(merged),
                 subjects=sorted({c["subject"] for c in merged}),
                 kinds=sorted({c["anomaly_kind"] for c in merged}))
    return merged


def _merge_by_subject(clues: list[dict]) -> list[dict]:
    """同一主体的多条异常诊断合并为一条异常线索（AC4 按主体聚合）。"""
    by_subject: dict[str, dict] = {}
    order: list[str] = []
    for c in clues:
        s = c["subject"]
        if s not in by_subject:
            by_subject[s] = dict(c)
            by_subject[s]["候选虚处"] = f"[异常·待核实] 主体「{s}」存在 {0} 处空白信号"
            by_subject[s]["_parts"] = []
            order.append(s)
        agg = by_subject[s]
        agg["_parts"].append({"rule_id": c["rule_id"], "anomaly_kind": c["anomaly_kind"],
                              "依据": c["依据"], "diagnostic_ids": c["diagnostic_ids"]})
        agg["diagnostic_ids"] = sorted(set(agg["diagnostic_ids"]) | set(c["diagnostic_ids"]))
        agg.setdefault("anomaly_kinds", set()).add(c["anomaly_kind"])
    out: list[dict] = []
    for s in order:
        agg = by_subject[s]
        n = len(agg["_parts"])
        agg["候选虚处"] = f"[异常·待核实] 主体「{s}」存在 {n} 处空白信号"
        agg["依据"] = "；".join(p["依据"] for p in agg["_parts"])
        agg["anomaly_kinds"] = sorted(agg.pop("anomaly_kinds"))
        agg["anomaly_count"] = n
        agg.pop("_parts", None)
        # 强制标记不可被合并污染
        agg["级别"] = "待核实"
        agg["needs_human_review"] = True
        agg["is_anomaly"] = True
        out.append(agg)
    return out


def group_by_subject(clues: list[dict]) -> dict[str, list[dict]]:
    """按主体分组输出（AC4）。"""
    grouped: dict[str, list[dict]] = {}
    for c in clues:
        grouped.setdefault(c.get("subject", _WHOLE_CASE), []).append(c)
    return grouped


def is_anomaly(item: dict) -> bool:
    return bool(item.get("is_anomaly"))


def non_anomaly(items: list[dict]) -> list[dict]:
    """红线过滤器：送入五间交叉/升格计数前，剔除异常线索。"""
    return [it for it in items if not is_anomaly(it)]


def partition(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """把混合线索流拆为 (正常线索, 异常线索)。"""
    normal, anomalies = [], []
    for it in items:
        (anomalies if is_anomaly(it) else normal).append(it)
    return normal, anomalies
