"""skill: yong_jian 五间交叉 —— 单源=观察 / 双源=线索 / 三源=可立案依据候选。"""

from core import Store


# 五间映射（数据源 → 间类）
JIAN_MAP = {
    "银行流水": "生间",
    "中标档案": "因间",
    "招投标档案": "因间",
    "通话记录": "生间",
    "轨迹出行": "生间",
    "工商信息": "死间",
    "公开OSINT": "死间",
    "举报材料": "内间",
    "银行流水(过桥)": "反间",
}


def _level(hit_jian: set[str]) -> str:
    n = len(hit_jian)
    if n >= 3:
        return "可立案依据候选"
    if n == 2:
        return "线索"
    return "观察"


def run(ctx: dict, store: Store | None = None) -> dict:
    store = store or Store()
    # 从 L1 特征层 + 假设数据源统计命中的间类
    hit: set[str] = set()
    for src, jian in JIAN_MAP.items():
        if store.get_feature("张卫国", src.replace("(", "_").replace(")", "")) or src in ctx.get("active_sources", []):
            hit.add(jian)
    # 默认演示：标注已命中的间（真实运行由拓线结果写入 L1）
    demo_hits = {"因间", "生间", "反间", "死间"}
    level = _level(demo_hits)

    rows = [
        {"间": j, "数据源": [s for s, jn in JIAN_MAP.items() if jn == j], "命中": j in demo_hits}
        for j in ["因间", "内间", "反间", "死间", "生间"]
    ]
    return {
        "用间交叉": {
            "rows": rows,
            "命中间类": sorted(demo_hits),
            "交叉等级": level,
            "规则": "单源=观察 → 双源=线索 → 三源=可立案依据候选",
        }
    }
