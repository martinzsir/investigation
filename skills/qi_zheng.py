"""skill: qi_zheng 奇正分工 —— 奇兵(AI)拓线 / 正兵(人)固证 双列。"""

from core import Store


def run(ctx: dict, store: Store | None = None) -> dict:
    store = store or Store()
    # 奇兵：用 DuckDB 批量碰撞（可在 L2 预聚合上跑，更快）
    q1 = store.query(
        "SELECT 项目, bid_date, deposit_date, offset_days, amount "
        "FROM read_parquet('data/Q1_time_window.parquet') "
        "ORDER BY offset_days"
    )
    return {
        "奇正分工": {
            "奇兵(AI)": [
                "Q1 时间窗碰撞（DuckDB 已算）",
                "Q2 过桥资金流向分析",
                "Q3 公示期通话频次 vs 常态",
                "Q4 跨案串并（共享过桥方）",
                "Q5 轨迹同框检测",
            ],
            "正兵(人)": [
                "调取 A公司完整流水（闭环缺口）",
                "现金冠字号码溯源",
                "核对中标评分表 / 评标专家",
                "固定通话记录原件（刻盘封存）",
                "张卫国/李志强言词取证（批准后）",
            ],
            "Q1_result": q1,
        }
    }
