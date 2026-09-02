"""skill: yong_jian 五间交叉 —— 单源=观察 / 双源=线索 / 三源=可立案依据候选。

v3（Ontology Function 化）：交叉判定为 Function jian_cross_level
（core.functions 实现，语义代理表非空即命中；未建模数据源诚实标缺口）。
"""

from core import Store
from core.functions import invoke_function


def run(ctx: dict, store: Store | None = None) -> dict:
    store = store or Store()
    r = invoke_function(store, "jian_cross_level")["result"]
    return {
        "用间交叉": {
            "rows": r["rows"],
            "命中间类": r["命中间类"],
            "交叉等级": r["交叉等级"],
            "规则": r["规则"],
        }
    }
