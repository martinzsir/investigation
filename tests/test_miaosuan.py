"""
tests/test_miaosuan.py
庙算假设引擎测试：
  A. 数据驱动：auto_from_findings 异常发现 → 候选假设（幂等 / 上限 / id 顺延 / 模式库）
  B. 人机协同：add / remove / reorder 受控接口 + 审计
  C. 规则约束：受限(子串匹配) / 数据源缺失降级 / H5 解除受限
  F. 五维度覆盖度（<80% 报警）
  G. 模式库扩展（频次突增/同框/利益关联 → H3/H2）
  H. 反遗漏规则 2/4（间类缺口 / 证据冲突）
  I. 枚举空间候选池（笛卡尔积 / 候补 / promote）
"""
import unittest

from core import MiaoSuan, Hypothesis

FINDINGS = [
    {"候选虚处": "季度末整数现金存入", "依据": "与工资非整数规律不符"},
    {"候选虚处": "第三方过桥结构", "依据": "宏业→A建材→配偶资金链"},
    {"候选虚处": "未知异常X", "依据": "无命中模式"},
]

EXT_FINDINGS = FINDINGS[:2] + [
    {"候选虚处": "招投标公示期通话频次突增", "依据": "张卫国→李志强 为常态数倍",
     "source_rows": [{"主体": "张卫国", "对端": "李志强", "次数": 114}]},
    {"候选虚处": "二人公示期轨迹同框", "依据": "相同地点 ±1 天先后出现",
     "source_rows": [{"主体a": "张卫国", "地点": "项目X"}]},
    {"候选虚处": "工商登记利益关联", "依据": "A建材法人系李志强妻弟",
     "source_rows": [{"主体": "A建材", "法人": "李志强妻弟"}]},
]


def _miao() -> MiaoSuan:
    m = MiaoSuan()
    m.set_ji(gaps=["缺口"], auth_boundary=["边界"])
    return m


def _full_miao() -> MiaoSuan:
    """H1(资金,时间/生间) H4(资金,时间/反间) H3(通讯,行为/生间) H2(关系/因间) H5(行为/内间)"""
    m = _miao()
    m.auto_from_findings(EXT_FINDINGS)
    m.add(Hypothesis(id="H5", description="张卫国隐匿财产",
                     evidence_needed=["房产", "车辆"],
                     data_sources=["房产车辆（未调取）"],
                     dimension=["行为"], jian_types=["内间"]))
    m.build(["银行流水", "通话记录", "招投标档案", "工商信息", "轨迹出行"], ["房产车辆"])
    return m


class TestDataDriven(unittest.TestCase):
    """A. 数据驱动：异常发现 → 候选假设"""

    def test_整数存入映射为H1(self):
        m = _miao()
        added = m.auto_from_findings([FINDINGS[0]])
        self.assertEqual([h.id for h in added], ["H1"])
        self.assertIn("存入", added[0].description)

    def test_过桥映射为H4(self):
        m = _miao()
        added = m.auto_from_findings([FINDINGS[1]])
        self.assertEqual([h.id for h in added], ["H4"])

    def test_四字段必备(self):
        m = _miao()
        h = m.auto_from_findings(FINDINGS)[0]
        for f in ("evidence_needed", "data_sources", "procedure", "falsification"):
            self.assertTrue(getattr(h, f), f"{f} 不得为空")

    def test_无模式命中则忽略(self):
        m = _miao()
        self.assertEqual(m.auto_from_findings([FINDINGS[2]]), [])

    def test_幂等同描述不重复生成(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)
        n = len(m.hypotheses)
        m.auto_from_findings(FINDINGS)
        self.assertEqual(len(m.hypotheses), n)

    def test_上限满员自动停止不抛异常(self):
        m = _miao()
        for i in range(5):
            m.add(Hypothesis(id=f"X{i}", description=f"占位{i}"))
        added = m.auto_from_findings(FINDINGS)
        self.assertEqual(added, [])
        self.assertTrue(any(a["action"] == "auto_skip_full" for a in m.audit))

    def test_id冲突顺延编号(self):
        m = _miao()
        m.add(Hypothesis(id="H1", description="别的假设"))
        added = m.auto_from_findings([FINDINGS[0]])
        self.assertEqual(added[0].id, "H6")  # MAX=5，首个空闲编号


class TestHumanCollaboration(unittest.TestCase):
    """B. 人机协同：add / remove / reorder 受控接口 + 审计"""

    def test_remove_存在(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)
        popped = m.remove("H4")
        self.assertEqual(popped.id, "H4")
        self.assertEqual([h.id for h in m.hypotheses], ["H1"])
        self.assertTrue(any(a["action"] == "remove" for a in m.audit))

    def test_remove_不存在报错(self):
        m = _miao()
        with self.assertRaises(KeyError):
            m.remove("H9")

    def test_reorder_合法(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)  # [H1, H4]
        m.reorder(["H4", "H1"])
        self.assertEqual([h.id for h in m.hypotheses], ["H4", "H1"])
        self.assertTrue(any(a["action"] == "reorder" for a in m.audit))

    def test_reorder_非法列表报错(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)  # [H1, H4]
        for bad in (["H1"], ["H1", "H4", "H9"], ["H1", "H1"]):
            with self.assertRaises(ValueError):
                m.reorder(bad)


class TestRuleConstraints(unittest.TestCase):
    """C. 规则约束：受限(子串) / 降级 / 解除受限"""

    def test_受限子串匹配修复(self):
        """'房产' ⊂ '房产车辆'：旧版精确求交命中不了，此为修复回归。"""
        m = _miao()
        m.add(Hypothesis(id="H5", description="隐匿财产",
                         evidence_needed=["房产", "车辆"],
                         data_sources=["房产车辆（未调取）"]))
        m.build(["银行流水"], ["房产车辆"])
        self.assertEqual(m.hypotheses[0].status, "受限(待授权)")

    def test_数据源缺失标降级不删除(self):
        m = _miao()
        m.add(Hypothesis(id="H3", description="密切私下关系",
                         evidence_needed=["通话频次", "轨迹同框"],
                         data_sources=["通话记录", "轨迹出行"]))
        m.build(["银行流水", "通话记录"], [])  # 缺轨迹出行
        h = m.hypotheses[0]
        self.assertTrue(h.degraded)
        self.assertIn("轨迹出行", h.degrade_note)
        self.assertEqual([x.id for x in m.hypotheses], ["H3"])  # 假设保留

    def test_数据齐全不降级(self):
        m = _miao()
        m.add(Hypothesis(id="H1", description="收受财物",
                         evidence_needed=["银行流水"], data_sources=["银行流水"]))
        m.build(["银行流水"], [])
        self.assertFalse(m.hypotheses[0].degraded)

    def test_解除受限(self):
        m = _miao()
        m.add(Hypothesis(id="H5", description="隐匿财产",
                         evidence_needed=["房产"], data_sources=["房产车辆"]))
        m.build(["银行流水", "房产车辆"], [])  # 已调取 → 不受限
        self.assertEqual(m.hypotheses[0].status, "待推演")

    def test_知己为空build报错(self):
        m = MiaoSuan()
        with self.assertRaises(ValueError):
            m.build([], [])

    def test_审计完整覆盖关键动作(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)
        m.add(Hypothesis(id="H5", description="隐匿财产",
                         evidence_needed=["房产"], data_sources=["房产车辆"]))
        m.build(["银行流水"], ["房产车辆"])
        actions = [a["action"] for a in m.audit]
        for expect in ("add", "受限标记", "降级标记"):
            self.assertIn(expect, actions)
        for a in m.audit:
            self.assertIn("ts", a)


class TestDimensionCoverage(unittest.TestCase):
    """F. 五维度覆盖度（资金/通讯/行为/关系/时间）"""

    def test_假设携带维度字段(self):
        m = _miao()
        h = m.auto_from_findings([EXT_FINDINGS[0]])[0]
        self.assertEqual(h.dimension, ["资金", "时间"])
        self.assertEqual(h.jian_types, ["生间"])

    def test_五维全覆盖无报警(self):
        m = _full_miao()
        dc = m.dimension_coverage()
        self.assertEqual(dc["score"], 1.0)
        self.assertFalse(dc["alarm"])
        self.assertEqual(dc["missing"], [])

    def test_缺维度触发报警(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)  # 仅 H1/H4 → 资金+时间
        dc = m.dimension_coverage()
        self.assertEqual(dc["score"], 0.4)
        self.assertTrue(dc["alarm"])
        self.assertIn("假设覆盖不完整", dc["alarm_text"])
        # 报警进入 report
        self.assertTrue(m.report()["dimension_coverage"]["alarm"])

    def test_报警阈值边界(self):
        m = _miao()
        # H1/H4(资金,时间) + H3(通讯,行为) = 4/5 → 0.8 不触发（阈值严格小于）
        m.auto_from_findings(FINDINGS[:2] + [EXT_FINDINGS[2]])
        dc = m.dimension_coverage()
        self.assertEqual(dc["score"], 0.8)
        self.assertFalse(dc["alarm"])


class TestPatternLibrary(unittest.TestCase):
    """G. 模式库扩展：通讯/行为/关系检测器 → H3/H2"""

    def test_频次突增映射H3(self):
        m = _miao()
        added = m.auto_from_findings([EXT_FINDINGS[2]])
        self.assertEqual([h.id for h in added], ["H3"])
        self.assertIn("通讯", added[0].dimension)

    def test_同框与频次幂等同假设(self):
        m = _miao()
        m.auto_from_findings([EXT_FINDINGS[2], EXT_FINDINGS[3]])  # 频次 + 同框
        self.assertEqual([h.id for h in m.hypotheses], ["H3"])  # 同描述只留一条

    def test_利益关联映射H2(self):
        m = _miao()
        added = m.auto_from_findings([EXT_FINDINGS[4]])
        self.assertEqual([h.id for h in added], ["H2"])
        self.assertEqual(added[0].dimension, ["关系"])

    def test_溯源行随发现留存(self):
        m = _miao()
        h = m.auto_from_findings([EXT_FINDINGS[2]])[0]
        self.assertEqual(h.source_rows, EXT_FINDINGS[2]["source_rows"])


class TestJianAndConflict(unittest.TestCase):
    """H. 反遗漏规则 2/4：间类缺口 + 证据冲突"""

    def test_间类缺口警告(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)  # H1 生间 + H4 反间
        jc = m.jian_coverage()
        for j in ("因间", "死间", "内间"):
            self.assertIn(j, jc["missing"])
        self.assertTrue(jc["warnings"])

    def test_间类全覆盖无警告(self):
        m = _full_miao()
        jc = m.jian_coverage(expected=["生间", "反间", "因间", "内间"])
        self.assertEqual(jc["missing"], [])
        self.assertEqual(jc["warnings"], [])

    def test_证据冲突检测(self):
        m = _miao()
        rows = [{"行": 1, "金额": 100000}]
        m.add(Hypothesis(id="H1", description="甲", source_rows=rows))
        m.add(Hypothesis(id="H4", description="乙", source_rows=[{"行": 1, "金额": 100000}]))
        conflicts = m.conflict_check()
        self.assertEqual(len(conflicts), 1)
        self.assertIn("H1 与 H4", conflicts[0])
        self.assertIn("需合并或区分", conflicts[0])

    def test_证据无冲突(self):
        m = _miao()
        m.add(Hypothesis(id="H1", description="甲", source_rows=[{"行": 1}]))
        m.add(Hypothesis(id="H4", description="乙", source_rows=[{"行": 2}]))
        self.assertEqual(m.conflict_check(), [])


class TestEnumeration(unittest.TestCase):
    """I. 枚举空间候选池：笛卡尔积 / 候补 / promote"""

    def test_枚举组合总数(self):
        m = _miao()
        r = m.enumerate_space()
        self.assertEqual(r["total_combos"], 4 * 5 * 3 * 2 * 2)  # 240

    def test_候补池按行为去重(self):
        m = _miao()
        m.auto_from_findings(FINDINGS)  # H1/H4 已转正
        m.enumerate_space()
        # 支撑型（现金收受/过桥）已转正 → 不在候补；无支撑型按行为去重 = 3
        self.assertEqual([c["combo"]["行为"] for c in m.backlog],
                         ["购物卡", "转账", "代持"])

    def test_候补转正_有支撑(self):
        m = _miao()
        m.enumerate_space()  # 空假设：支撑型进候补
        cand = next(c for c in m.backlog if c["supported"])
        h = m.promote(cand["id"])
        self.assertEqual(h.description, cand["maps_to"])
        self.assertTrue(any(a["action"] == "promote" for a in m.audit))

    def test_候补转正_无支撑占位(self):
        m = _miao()
        m.enumerate_space()
        cand = next(c for c in m.backlog if not c["supported"])
        h = m.promote(cand["id"])
        self.assertIn("枚举候补", h.description)
        self.assertEqual(h.evidence_needed, ["待正兵明确所需证据"])

    def test_promote_不存在报错(self):
        m = _miao()
        with self.assertRaises(KeyError):
            m.promote("C999")

    def test_枚举空间可扩展(self):
        m = _miao()
        r = m.enumerate_space({"行为": ["购物卡", "虚拟币"]})
        self.assertEqual(r["total_combos"], 2)
        self.assertEqual({c["combo"]["行为"] for c in m.backlog}, {"购物卡", "虚拟币"})


if __name__ == "__main__":
    unittest.main()
