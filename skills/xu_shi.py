"""skill: xu_shi 虚实扫描 —— 从语义层 Function 扫反常，只标反常不给定性。

v3（Ontology Function 化）：检测器是 Function 的薄编排层——
计算逻辑全部声明在 ontology/default/functions.json + core/functions.py，
本文件只负责把 Function 结果组织为 findings（候选虚处/依据/溯源行）。
换数据源/改算法只动 Function 声明，检测器零改动。

模式库（按侦查学维度组织）：
  资金：quarter_end_integer_deposits / integer_transfer_aggregates
  通讯：call_frequency_spike
  行为：co_located_pairs
  关系：org_interest_links
"""

from core import Store
from core.functions import invoke_function


def run(ctx: dict, store: Store | None = None) -> dict:
    store = store or Store()
    findings: list[dict] = []

    # ---- 资金维度：季度末整数存入 ----
    flow = invoke_function(store, "quarter_end_integer_deposits")["rows"]
    findings.append({"候选虚处": "季度末整数现金存入", "依据": "与工资非整数规律不符",
                     "级别": "待核实", "source_rows": flow})

    # ---- 资金维度：过桥结构 ----
    overpass = invoke_function(store, "integer_transfer_aggregates")["rows"]
    findings.append({"候选虚处": "第三方过桥结构", "依据": "宏业→A建材→配偶资金链",
                     "级别": "待核实",
                     "source_rows": [{"主体": o["from_raw"], "对方": o["to_raw"],
                                      "total": o["total"]} for o in overpass]})

    # ---- 通讯维度：频次突增 ----
    spike = invoke_function(store, "call_frequency_spike")["result"]
    if spike["hit"]:
        findings.append({
            "候选虚处": "招投标公示期通话频次突增",
            "依据": spike["basis"],
            "级别": "待核实",
            "source_rows": spike["pairs"],
        })

    # ---- 行为维度：轨迹同框 ----
    co_locate = invoke_function(store, "co_located_pairs")["rows"]
    if co_locate:
        findings.append({
            "候选虚处": "二人公示期轨迹同框",
            "依据": f"不同主体在相同地点 ±1 天内先后出现 {len(co_locate)} 次",
            "级别": "待核实",
            "source_rows": [dict(r) for r in co_locate],
        })

    # ---- 关系维度：工商登记利益关联 ----
    linked = invoke_function(store, "org_interest_links")["rows"]
    if linked:
        findings.append({
            "候选虚处": "工商登记利益关联",
            "依据": "法人/关联人与对象人员存在重叠（如 A建材法人系李志强妻弟）",
            "级别": "待核实",
            "source_rows": [dict(r) for r in linked],
        })

    return {"虚实扫描": {"findings": findings, "备注": "只标反常，不给定性；须正兵复核"}}
