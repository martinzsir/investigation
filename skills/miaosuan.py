"""skill: miaosuan 庙算沙盘 —— 假设 ≤5 条，自动证伪条件。

四层覆盖完整性机制（2026-09-02 对齐参考设计）：
  1. 数据驱动：auto_from_findings() 把虚实扫描发现按模式库（5 维度）自动映射为候选假设
  2. 规则约束：≤5 条 / 四字段必备 / 超授权边界标受限 / 数据源缺失标降级（build 内置）
  3. 反遗漏：五维度覆盖度双轨（声明轨 ≤80% 报警 G-009；实证轨缺口独立报警
     G-024——"想到了但没查到"，落 miaosuan:dimension:empirical 诊断）+
     间类缺口 + 证据冲突 + 枚举候补池
  4. 人机协同：正兵手动 add / remove / reorder / promote，全程审计
"""

from core.hypotheses import MiaoSuan, Hypothesis

# 默认异常发现（与 skills/xu_shi.py 的 findings 同构；真实场景由虚实扫描传入）
_DEFAULT_FINDINGS = [
    {"候选虚处": "季度末整数现金存入", "依据": "与工资非整数规律不符",
     "source_rows": [{"主体": "张卫国", "金额": 100000}]},
    {"候选虚处": "第三方过桥结构", "依据": "宏业→A建材→配偶资金链",
     "source_rows": [{"主体": "宏业建设", "对方": "A建材", "total": 4600000}]},
    {"候选虚处": "招投标公示期通话频次突增", "依据": "张卫国→李志强 通话为常态数倍",
     "source_rows": [{"主体": "张卫国", "对端": "李志强", "次数": 114}]},
    {"候选虚处": "二人公示期轨迹同框", "依据": "相同地点 ±1 天先后出现",
     "source_rows": [{"主体a": "张卫国", "主体b": "李志强", "地点": "项目X"}]},
    {"候选虚处": "工商登记利益关联", "依据": "A建材法人系李志强妻弟",
     "source_rows": [{"主体": "A建材", "法人": "李志强妻弟"}]},
]


def run(ctx: dict) -> dict:
    miao = MiaoSuan()
    miao.set_ji(
        gaps=ctx.get("证据缺口", ["现金来源无法溯源", "A公司流水缺口"]),
        auth_boundary=ctx.get("授权边界", ["不可查房产车辆", "不可直接接触对象"]),
    )

    # ---- 第 1 层：数据驱动（异常发现 → 候选假设，模式库自动映射 H1/H4/H3/H2）----
    miao.auto_from_findings(ctx.get("虚实发现", _DEFAULT_FINDINGS))

    # ---- 第 4 层：人机协同（正兵手动补充，add 接口；H5 受限演示）----
    miao.add(Hypothesis(
        id="H5", description="张卫国隐匿财产",
        evidence_needed=["房产", "车辆"], data_sources=["房产车辆（未调取）"],
        procedure="待批", falsification="资产与合法收入匹配则证伪",
        dimension=["行为"], jian_types=["内间"],
    ))

    # ---- 第 2 层：规则约束（≤5 / 受限 / 降级，build 内置）----
    avail = ctx.get("可用数据", ["银行流水", "通话记录", "招投标档案",
                                "工商信息", "轨迹出行"])
    miao.build(avail, ctx.get("未调取", ["房产车辆"]))

    # ---- 第 3 层：枚举空间（笛卡尔积候选池 + 候补清单）----
    miao.enumerate_space(ctx.get("枚举空间"))

    # ---- 第 4 层：人机协同（正兵调整优先级，reorder 接口）----
    miao.reorder(ctx.get("假设顺序", ["H1", "H2", "H3", "H4", "H5"]))

    return {"庙算基线": miao.to_dict(), "覆盖度校验": miao.report(avail)}
