"""skill: xu_shi 虚实扫描 —— 规则手册驱动（rules.json），只标反常不给定性。

v4（Rulebook）：检测器是自然语言规则的薄编排层——
判据文本声明在 ontology/default/rules.json（rule_text，分析师可读写、LLM 可读取解释），
确定性计算绑定只读 Function（function + params，SQL 白名单/py 注册实现）。
本文件零业务逻辑：换规则、调阈值只改 rules.json，检测器不改代码。

规则手册（按侦查学维度组织，stage=xu_shi）：
  资金：R1 季度末整数现金存入 / R2 整数转账聚合（过桥结构）
  通讯：R3 招投标公示期通话频次突增
  行为：R4 二人公示期轨迹同框
  关系：R5 工商登记利益关联
"""

from core import Store
from core.rules import run_rules


def run(ctx: dict, store: Store | None = None, health=None) -> dict:
    store = store or Store()
    findings = run_rules(store, stage="xu_shi", health=health)
    return {"虚实扫描": {
        "findings": findings,
        "备注": "只标反常，不给定性；须正兵复核；每条依据规则见 rule_id/rule_text（AI辅助推演，非证据）",
    }}
