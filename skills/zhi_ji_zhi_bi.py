"""skill: zhi_ji_zhi_bi 双向盘点 —— 彼己双栏，知己强制非空。"""

from core import MiaoSuan


def run(ctx: dict, miao: MiaoSuan | None = None) -> dict:
    # 若外部未构造 MiaoSuan，则按 ctx 重建（保证知己非空）
    if miao is None:
        miao = MiaoSuan()
        miao.set_ji(ctx.get("证据缺口", ["现金来源无法溯源"]), ctx.get("授权边界", ["不可查房产车辆"]))

    bi = {
        "彼（张卫国）": {
            "身份": "住建局副局长，分管招投标",
            "关系": "宏业公司 7 个项目在其分管领域中标",
            "资产线索": "2019 年起季度末整数现金存入 8-10 万",
            "行为": "招投标期与李志强通话加密、轨迹同框",
        },
        "己（我方）": {
            "已握线索": "现金存入与中标时间窗重合；过桥资金链；轨迹同框",
            "证据缺口": miao.ji.get("证据缺口", "未填"),
            "授权边界": miao.ji.get("授权边界", "未填"),
        },
    }
    return {"双向盘点": bi}
