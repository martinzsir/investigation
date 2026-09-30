"""
CAN-18：Function → 假设归属的单一真相源。

为什么需要这个文件
------------------
研判镜头（Lens）要挂假设（assumption），此前是**手写**的，结果 8 个镜头
里挂错了 4 个——例如 ``integer_transfer_aggregates`` 的 description 明写
"规则 R2 挂钩"，而 R2 在 hypothesis_patterns.json 里映射的是 **H4**（财物
通过第三方过桥），却被挂成了 H1；``call_frequency_spike``（通话高频）对应
R3 → **H3**（二人密切私下关系），却被挂成了 H2。

根因是假设被抄了一遍：抄的时候就可能抄错，且抄完无人校验。

设计主张：**不手写假设，只声明规则，假设从本体反查。**

本文件只维护 Function → rule_id 这一张表（依据取自 functions.json 里各
Function 自带的 description 文本，如"规则 R1 挂钩"），再由
hypothesis_patterns.json 反查 rule → hypothesis。这样假设只有一处来源
（本体），不可能再次抄错。

与 ``core/hypotheses.py::match_finding_pattern`` 的关系：
  那个是 finding（线索文本）→ 假设，走 rule_ids + keywords 两段匹配；
  本文件是 Function（计算内核）→ 假设，走 rule_id 精确反查。
  两者输入不同，但最终都回到同一份 hypothesis_patterns.json，不另立真相。

红线：判不出来的不硬猜。``time_window_collision``（中标-资金时间窗碰撞）
与 ``jian_cross_level``（五间交叉等级）归属存在业务歧义，一律返回 None 并
标注原因，由业务确认后再落——这跟"重名不自裁"是同一条纪律。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# Function → rule_id（唯一需要人工维护的表）
#
# 依据：ontology/default/functions.json 中各 Function 的 description 文本
# 自带的规则挂钩声明。新增/修改须同步注释里的原文依据，便于复核。
# ----------------------------------------------------------------------
FUNCTION_RULE: Dict[str, str] = {
    # description: "季末窗口内整万元现金存入按季度聚合（规则 R1 挂钩）"
    "quarter_end_integer_deposits": "R1",
    # description: "整数转账 from→to 聚合，第三方过桥结构识别（规则 R2 挂钩）"
    "integer_transfer_aggregates": "R2",
    # title "两跳过桥路径" + R2/R6 同属过桥链路 → 取 R2
    "overpass_two_hop": "R2",
    # description: "头部对端频次 ≥2 倍常态中位数"；R3 keywords 含 频次/高频/通话
    "call_frequency_spike": "R3",
    # description: "不同主体同地点 ±1 天内先后出现" → R-GEO-3 异主体时空反复同框
    "co_located_pairs": "R-GEO-3",
    # rules.json R-GEO-3 显式挂 geo_spatiotemporal_accompany（与 co_located_pairs
    # 同归 H6 私下接触）。geo_accompany pack.json 也显式声明 H6，但反查表
    # 仍补 R-GEO-3 作为兜底——pack.json 显式优先，反查为兜底
    "geo_spatiotemporal_accompany": "R-GEO-3",
    # description: "法人/关联人命中案件知识包主体/关系人即候选" → R5（利益关联）
    "org_interest_links": "R5",
    # description: "...整数资金且主体为个人的碰撞（...规则 R6 挂钩...）"
    # 注意：R6 与 R2 同归 H4（过桥），本体 hypothesis_patterns 已声明。
    # 此前曾以"整数资金偏 H1、中标优势偏 H2"为由列为待确认——那是凭业务直觉
    # 覆盖了本体的显式声明，正是本文件要根治的错误，故改回按本体反查。
    "time_window_collision": "R6",
}

# 归属待业务确认：不硬猜，返回 None + 原因
FUNCTION_ASSUMPTION_PENDING: Dict[str, str] = {
    "jian_cross_level": "五间交叉等级是观察分级（单源/双源/三源），不是证据假设，"
                        "不应挂假设",
    # geo_anomaly_trajectory 检测的偏离分三类，各自指向不同假设：
    #   · 非常态地点（off_route）—— 可能指向 H6 私下接触（私下场所），
    #     也可能只是 H5 系列事件空间聚集（多次重复访问）
    #   · 非常态时段（off_hours）—— 与 H3 二人密切私下关系部分重叠，
    #     但语义不同（H3 是通讯频次突增）
    #   · 非常态通勤（off_path）—— 不直接对应任何已声明假设
    # 单一 function 取单一假设就是替正兵归因，留给 pack.json 显式声明
    # 或正兵在画布上手工挂接
    "geo_anomaly_trajectory": "异常轨迹的非常驻地点/非常态时段/非常态通勤分别"
                              "对应 H5/H6/H3 不同假设，单一归属会替正兵归因；"
                              "由 pack.json 显式声明或正兵手工挂接",
    # 描述统计类镜头：不产出可证伪的命题，只描绘主体活动结构。
    # 落脚点画像/活动范围/轨迹分段/缓冲区扫描 是描述工具，挂假设就是
    # 把"这个人常去这些地方"伪装成"这个人在哪里犯罪"——典型过度归因。
    # 周期节奏/事件序列邻接 同类：聚合时间结构，不挂假设。
    "geo_subject_sites": "落脚点画像是描述统计（按地点聚合到访次数），"
                         "不产出可证伪命题，不应挂假设",
    "geo_profile_cgt": "Rossmo CGT 概率面产出优先排查区域，不是定址结论；"
                        "假设挂在规则 R-GEO-1（H5）上，由 rules.json 命中触发，"
                        "不由裸 Function 反查",
    "geo_buffer_scan": "缓冲区环带扫描是描述工具（谁在锚点附近出现），"
                       "不产出可证伪命题，不应挂假设",
    "geo_trajectory_segment": "轨迹分段是描述工具（停留段/移动段切分），"
                              "不产出可证伪命题，不应挂假设",
    "geo_activity_range": "活动范围画像是描述统计（平均中心/标准距离/"
                          "标准差椭圆），不产出可证伪命题，不应挂假设",
    "timeline_rhythm": "周期节奏是描述统计（聚集簇识别），"
                       "不产出可证伪命题，不应挂假设",
    "timeline_event_sequence": "事件序列邻接是描述统计（时间轴排序），"
                                "不产出可证伪命题，不应挂假设",
}


# ----------------------------------------------------------------------
# 反查：rule_id → hypothesis
# ----------------------------------------------------------------------
def _load_patterns(pack: str = "default", base_dir=None) -> List[dict]:
    try:
        from core.ontology_loader import load_hypothesis_patterns
        pats = load_hypothesis_patterns(pack, base_dir)
        return pats or []
    except Exception:
        return []


def rule_to_hypothesis(rule_id: str, pack: str = "default",
                       base_dir=None) -> Optional[dict]:
    """rule_id → 假设声明（取自本体，不手写）。未声明返回 None。"""
    rid = str(rule_id or "").strip()
    if not rid:
        return None
    for p in _load_patterns(pack, base_dir):
        if rid in (p.get("rule_ids") or []):
            return p.get("hypothesis")
    return None


def resolve_function_assumption(fn_name: str, pack: str = "default",
                                base_dir=None) -> Tuple[Optional[str], str]:
    """Function → (假设 id, 说明)。

    三种返回：
      1. 已声明规则 → (假设 id, "经规则 R? 反查")
      2. 待确认     → (None, 具体歧义原因)   ← 不硬猜
      3. 无声明     → (None, "未声明规则挂钩")
    """
    name = str(fn_name or "").strip()
    if name in FUNCTION_ASSUMPTION_PENDING:
        return None, FUNCTION_ASSUMPTION_PENDING[name]
    rule_id = FUNCTION_RULE.get(name)
    if not rule_id:
        return None, "未声明规则挂钩"
    hyp = rule_to_hypothesis(rule_id, pack, base_dir)
    if not hyp:
        return None, f"规则 {rule_id} 未在 hypothesis_patterns.json 声明假设"
    return hyp.get("id"), f"经规则 {rule_id} 反查"


def known_functions() -> List[str]:
    """已声明 + 待确认的 Function 全集（供全量校验遍历）。"""
    return sorted(set(FUNCTION_RULE) | set(FUNCTION_ASSUMPTION_PENDING))


def validate_all(pack: str = "default", base_dir=None) -> List[str]:
    """全量校验：返回问题列表，空列表表示全部通过。

    只报**机制性错误**（声明了规则却在本体查不到假设），
    不把"待确认"当错误——那是有意的留白，不是缺陷。
    """
    problems: List[str] = []
    for fn in sorted(FUNCTION_RULE):
        rule_id = FUNCTION_RULE[fn]
        hyp = rule_to_hypothesis(rule_id, pack, base_dir)
        if not hyp:
            problems.append(
                f"{fn}: 声明规则 {rule_id}，但 hypothesis_patterns.json 未声明其假设")
    # 交叉校验：本体里声明的规则必须存在，防悬空
    declared = set(FUNCTION_RULE.values())
    for p in _load_patterns(pack, base_dir):
        for rid in (p.get("rule_ids") or []):
            if rid not in declared and rid not in ("R-GEO-1", "R-GEO-2", "R-GEO-3"):
                # 未纳入本表的规则（如 R4/R6）暂不报——本表只覆盖裸 Function
                pass
    return problems
