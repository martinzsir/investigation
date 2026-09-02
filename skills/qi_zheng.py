"""skill: qi_zheng 奇正分工 —— 奇兵(AI)拓线 / 正兵(人)固证 双列。

v3（Ontology Function 化）：Q1 时间窗碰撞调用 Function time_window_collision
（lnk_time_window 语义链接，编译期声明），检测器不直接写 SQL。
"""

from core import Store
from core.functions import invoke_function


def run(ctx: dict, store: Store | None = None) -> dict:
    store = store or Store()
    q1 = invoke_function(store, "time_window_collision")["rows"]
    return {
        "奇正分工": {
            "奇兵(AI)": [
                "Q1 时间窗碰撞（function: time_window_collision）",
                "Q2 过桥资金流向分析（function: overpass_two_hop）",
                "Q3 公示期通话频次 vs 常态（function: call_frequency_spike）",
                "Q4 跨案串并（共享过桥方）",
                "Q5 轨迹同框检测（function: co_located_pairs）",
            ],
            "正兵(人)": [
                "调取 A公司完整流水（闭环缺口）",
                "现金冠字号码溯源",
                "核对中标评分表 / 评标专家",
                "固定通话记录原件（刻盘封存）",
                "张卫国/李志强言词取证（批准后）",
            ],
            "Q1_result": [dict(r) for r in q1],
        }
    }
