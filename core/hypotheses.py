"""
core/hypotheses.py
假设生成引擎：数据驱动映射 + 规则约束 + 人机协同 + 覆盖完整性。

假设 = 数据异常模式 × 规则约束 × 人机协同
- 数据驱动：auto_from_findings() 把异常扫描 findings 按模式库映射为候选假设
- 规则约束：≤5 条 / 四字段必备 / 超授权边界标受限(待授权) / 数据源缺失标降级
- 人机协同：add / remove / reorder / promote 受控接口，全程写审计
- 覆盖完整性：五维度覆盖度（<80% 报警）、间类缺口、假设证据冲突、枚举候补池
每条假设自带：所需证据 / 可调用数据源 / 对应程序 / 证伪条件 / 维度 / 间类 / 溯源行
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime


@dataclass
class Hypothesis:
    id: str
    description: str
    evidence_needed: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)
    procedure: str = ""
    falsification: str = ""
    status: str = "待推演"  # 待推演 / 受限(待授权)
    degraded: bool = False
    degrade_note: str = ""
    dimension: list[str] = field(default_factory=list)  # 资金/通讯/行为/关系/时间
    jian_types: list[str] = field(default_factory=list)  # 生/反/因/死/内间
    source_rows: list = field(default_factory=list)      # 溯源行（冲突检测用）

    def to_dict(self) -> dict:
        return asdict(self)


def _matches(a: str, b: str) -> bool:
    """授权/数据源匹配：精确相等或子串包含（'房产' ⊂ '房产车辆'）。

    注：早期版本用精确集合求交，'房产' 对 '房产车辆' 永远命中不了，
    导致 H5 的受限标记失效——子串匹配修复该缺口。
    """
    return a == b or a in b or b in a


def _strip_unavailable(s: str) -> str:
    """'房产车辆（未调取）' → '房产车辆'（去掉括号注记）。"""
    return s.split("（")[0].strip()


def _row_key(r) -> str:
    """溯源行归一化为可比字符串（dict/str 通吃）。"""
    if isinstance(r, dict):
        return json.dumps(r, sort_keys=True, ensure_ascii=False, default=str)
    return str(r)


class MiaoSuan:
    """庙算沙盘：假设 ≤5 条，自动证伪条件，知己强制非空"""

    MAX_HYPOTHESES = 5

    # 五大侦查维度（覆盖度模型）
    DIMENSIONS = ["资金", "通讯", "行为", "关系", "时间"]
    # 五间（间类覆盖检查）
    JIAN_ALL = ["生间", "反间", "因间", "死间", "内间"]
    # 维度覆盖度报警阈值
    DIMENSION_ALARM = 0.8

    # ---- 数据驱动：异常模式 → 假设模板（模式库，按维度标注） ----
    # keywords 命中 findings 的「候选虚处+依据」文本即映射；同描述幂等跳过。
    FINDING_PATTERNS: list[dict] = [
        {"keywords": ["整数", "存入"], "hypothesis": Hypothesis(
            id="H1", description="收受财物（异常整数现金存入）",
            evidence_needed=["银行流水", "言词证据"],
            data_sources=["银行流水", "招投标档案"],
            procedure="银行调取已批",
            falsification="流水无对价时间耦合则证伪",
            dimension=["资金", "时间"], jian_types=["生间"],
        )},
        {"keywords": ["过桥"], "hypothesis": Hypothesis(
            id="H4", description="财物通过第三方过桥",
            evidence_needed=["A建材公司完整流水", "冠字号码溯源"],
            data_sources=["银行流水", "工商信息"],
            procedure="需新增调取授权",
            falsification="若中间方与下游交易有合法商业背景则证伪",
            dimension=["资金", "时间"], jian_types=["反间"],
        )},
        {"keywords": ["频次", "突增"], "hypothesis": Hypothesis(
            id="H3", description="二人存在密切私下关系（通讯频次突增）",
            evidence_needed=["通话频次", "轨迹同框"],
            data_sources=["通话记录", "轨迹出行"],
            procedure="通话调取已批",
            falsification="通话/轨迹无异常则证伪",
            dimension=["通讯", "行为"], jian_types=["生间"],
        )},
        {"keywords": ["同框"], "hypothesis": Hypothesis(
            id="H3", description="二人存在密切私下关系（通讯频次突增）",
            evidence_needed=["通话频次", "轨迹同框"],
            data_sources=["通话记录", "轨迹出行"],
            procedure="通话调取已批",
            falsification="通话/轨迹无异常则证伪",
            dimension=["通讯", "行为"], jian_types=["生间"],
        )},
        {"keywords": ["利益关联"], "hypothesis": Hypothesis(
            id="H2", description="宏业公司因行贿获中标优势（利益关联）",
            evidence_needed=["中标评分表", "利益输送链"],
            data_sources=["工商信息", "招投标档案"],
            procedure="档案调取已批",
            falsification="中标评分正常且无关联则证伪",
            dimension=["关系"], jian_types=["因间"],
        )},
    ]

    # ---- 枚举空间（第二层：笛卡尔积候选池，永不闭合——正兵可传新维度） ----
    ENUM_SPACE: dict[str, list[str]] = {
        "主体": ["张卫国", "李志强", "A建材", "张卫国配偶"],
        "行为": ["现金收受", "购物卡", "转账", "过桥", "代持"],
        "时间": ["2018前", "2018-2024", "2024后"],
        "金额": ["小额多次", "大额单次"],
        "关系": ["直接", "间接"],
    }
    # 行为值 → 有检测器支撑的模板描述（其余行为=候补，待正兵注入或扩模式库）
    ENUM_BEHAVIOR_MAP: dict[str, str] = {
        "现金收受": "收受财物（异常整数现金存入）",
        "过桥": "财物通过第三方过桥",
    }

    def __init__(self):
        self.hypotheses: list[Hypothesis] = []
        self.ji: dict[str, str] = {}  # 知己栏（证据缺口 / 授权边界）
        self.audit: list[dict] = []   # 人机协同全程审计
        self.backlog: list[dict] = [] # 枚举候补池（未转正候选）
        self._enum_total = 0          # 最近一次枚举的组合总数

    def _log(self, action: str, detail: str = "") -> None:
        self.audit.append({
            "action": action, "detail": detail,
            "ts": datetime.now().isoformat(timespec="seconds"),
        })

    # ---------- 知己：强制非空 ----------
    def set_ji(self, gaps: list[str], auth_boundary: list[str]) -> None:
        if not gaps or not auth_boundary:
            raise ValueError("知己栏强制非空：必须填写证据缺口与授权边界")
        self.ji = {"证据缺口": "; ".join(gaps), "授权边界": "; ".join(auth_boundary)}

    # ---------- 假设管理（人机协同，全程审计） ----------
    def add(self, h: Hypothesis) -> None:
        if len(self.hypotheses) >= self.MAX_HYPOTHESES:
            raise RuntimeError(f"假设数已达上限 {self.MAX_HYPOTHESES}，请合并或删除")
        self.hypotheses.append(h)
        self._log("add", f"{h.id} {h.description}")

    def remove(self, hypothesis_id: str) -> Hypothesis:
        """删除假设（受控接口：不存在即报错，全程审计）。"""
        for i, h in enumerate(self.hypotheses):
            if h.id == hypothesis_id:
                popped = self.hypotheses.pop(i)
                self._log("remove", f"{popped.id} {popped.description}")
                return popped
        raise KeyError(f"假设不存在：{hypothesis_id}")

    def reorder(self, ordered_ids: list[str]) -> None:
        """按给定 id 顺序重排（必须与现有假设一一对应，全程审计）。"""
        current = [h.id for h in self.hypotheses]
        if len(set(ordered_ids)) != len(ordered_ids) or sorted(ordered_ids) != sorted(current):
            raise ValueError(f"重排序列表必须与现有假设一一对应：现有 {current}")
        by_id = {h.id: h for h in self.hypotheses}
        self.hypotheses = [by_id[i] for i in ordered_ids]
        self._log("reorder", " → ".join(ordered_ids))

    # ---------- 数据驱动：异常发现 → 候选假设 ----------
    def auto_from_findings(self, findings: list[dict],
                           patterns: list[dict] | None = None) -> list[Hypothesis]:
        """把异常扫描 findings 自动映射为候选假设（不依赖人工预置）。

        - 同描述已存在 → 幂等跳过
        - id 冲突（同 id 不同描述）→ 顺延编号 H6, H7...
        - 超过上限 → 记审计后停止（自动流程不抛异常炸管线）
        - finding 的 source_rows 随假设留存（供证据冲突检测）
        """
        added: list[Hypothesis] = []
        for f in findings:
            text = f"{f.get('候选虚处', '')}{f.get('依据', '')}"
            for p in (patterns or self.FINDING_PATTERNS):
                if not any(k in text for k in p["keywords"]):
                    continue
                tpl = p["hypothesis"]
                if any(h.description == tpl.description for h in self.hypotheses):
                    break  # 幂等
                new = replace(tpl, source_rows=list(f.get("source_rows", [])))
                ids = {h.id for h in self.hypotheses}
                if new.id in ids:
                    n = self.MAX_HYPOTHESES + 1
                    while f"H{n}" in ids:
                        n += 1
                    new.id = f"H{n}"
                try:
                    self.add(new)
                    added.append(new)
                except RuntimeError:
                    self._log("auto_skip_full", f"{new.id} {new.description}")
                break  # 一个 finding 只映射首个命中模式
        return added

    # ---------- 规则约束：受限 / 降级标记 ----------
    def build(self, available_data: list[str], unavailable: list[str]) -> list[Hypothesis]:
        """按授权边界与可用数据给假设打标（不自动删除任何假设）。

        available_data: 已掌握数据源名
        unavailable:    未调取（超出授权）的数据源名
        """
        if not self.ji:
            raise ValueError("请先调用 set_ji() 填写知己栏")
        avail = list(available_data)
        unav = list(unavailable)
        for h in self.hypotheses:
            # 规则：所需证据超出授权边界 → 受限(待授权)（子串匹配）
            if any(_matches(e, u) for e in h.evidence_needed for u in unav):
                if h.status != "受限(待授权)":
                    h.status = "受限(待授权)"
                    self._log("受限标记", h.id)
            # 规则：数据源缺失 → 降级标记（证据强度下降，假设保留）
            missing = [s for s in (_strip_unavailable(x) for x in h.data_sources)
                       if not any(_matches(s, a) for a in avail)]
            if missing:
                h.degraded = True
                h.degrade_note = f"缺数据源 {'、'.join(missing)}，证据降级（假设保留待补证）"
                self._log("降级标记", f"{h.id}: {h.degrade_note}")
        return self.hypotheses

    # ---------- 反遗漏规则 1：数据源覆盖 ----------
    def coverage(self, data_files: list[str]) -> dict:
        """覆盖度校验（反遗漏）：每个数据文件至少被 1 条假设引用。"""
        used: set[str] = set()
        for h in self.hypotheses:
            used.update(_strip_unavailable(s) for s in h.data_sources)
        unused = [f for f in data_files if f not in used]
        score = len(used & set(data_files)) / len(data_files) if data_files else 0
        return {"score": round(score * 100, 1), "unused": unused}

    # ---------- 反遗漏规则 2（F）：五维度覆盖度 ----------
    def dimension_coverage(self) -> dict:
        """维度覆盖 = 已覆盖维度 / 5；<80% 触发报警。"""
        covered = {d for h in self.hypotheses for d in h.dimension} & set(self.DIMENSIONS)
        missing = [d for d in self.DIMENSIONS if d not in covered]
        score = len(covered) / len(self.DIMENSIONS)
        return {
            "covered": sorted(covered), "missing": missing,
            "score": round(score, 2),
            "alarm": score < self.DIMENSION_ALARM,
            "alarm_text": ("假设覆盖不完整，建议补充数据或人工注入"
                           if score < self.DIMENSION_ALARM else ""),
        }

    # ---------- 反遗漏规则 3（H）：间类缺口 ----------
    def jian_coverage(self, expected: list[str] | None = None) -> dict:
        """每个间类至少被 1 条假设引用；缺 → 警告『对抗痕迹未覆盖』。"""
        expected = expected or self.JIAN_ALL
        covered = {j for h in self.hypotheses for j in h.jian_types}
        missing = [j for j in expected if j not in covered]
        return {"covered": [j for j in expected if j in covered],
                "missing": missing,
                "warnings": [f"间类未覆盖：{j}（对抗痕迹未覆盖）" for j in missing]}

    # ---------- 反遗漏规则 4（H）：证据冲突 ----------
    def conflict_check(self) -> list[str]:
        """假设间引用同一笔证据 → 提示『需合并或区分』。"""
        conflicts: list[str] = []
        hs = [h for h in self.hypotheses if h.source_rows]
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                a = {_row_key(r) for r in hs[i].source_rows}
                b = {_row_key(r) for r in hs[j].source_rows}
                inter = a & b
                if inter:
                    conflicts.append(
                        f"{hs[i].id} 与 {hs[j].id} 引用同一笔证据"
                        f"（{len(inter)} 行重叠），需合并或区分")
        return conflicts

    # ---------- 枚举空间（I）：笛卡尔积候选池 + 候补清单 ----------
    def enumerate_space(self, space: dict[str, list[str]] | None = None) -> dict:
        """五维组合展开候选池。

        - 行为值命中 ENUM_BEHAVIOR_MAP → 有检测器支撑（可转正，幂等去重）
        - 其余 → 候补池 backlog（按行为去重），正兵可 promote() 手动转正
        - 枚举空间永不闭合：传自定义 space 即可扩展维度
        """
        space = space or self.ENUM_SPACE
        keys = list(space)
        combos = [dict(zip(keys, vals)) for vals in itertools.product(*space.values())]
        self._enum_total = len(combos)
        self.backlog = []
        seen: set[str] = set()
        for idx, combo in enumerate(combos, 1):
            behavior = combo.get("行为", "")
            mapped = self.ENUM_BEHAVIOR_MAP.get(behavior)
            if mapped is None:
                key = f"无支撑::{behavior}"
                if key in seen:
                    continue
                seen.add(key)
                self.backlog.append({
                    "id": f"C{idx}", "combo": combo, "supported": False,
                    "maps_to": None, "reason": "无检测器命中，待正兵注入证据或扩模式库",
                })
            else:
                key = f"支撑::{behavior}"
                if key in seen:
                    continue
                seen.add(key)
                if any(h.description == mapped for h in self.hypotheses):
                    continue  # 已转正
                self.backlog.append({
                    "id": f"C{idx}", "combo": combo, "supported": True,
                    "maps_to": mapped, "reason": "有检测器支撑，可 promote() 转正",
                })
        self._log("enumerate", f"组合 {self._enum_total}，候补 {len(self.backlog)}")
        return {"total_combos": self._enum_total, "backlog": self.backlog}

    def promote(self, candidate_id: str) -> Hypothesis:
        """把候补池候选转正为假设（≤5 约束与 add() 一致，全程审计）。"""
        for i, c in enumerate(self.backlog):
            if c["id"] == candidate_id:
                ids = {h.id for h in self.hypotheses}
                n = 1
                while f"H{n}" in ids:
                    n += 1
                if c["supported"] and c["maps_to"]:
                    tpl = next(p["hypothesis"] for p in self.FINDING_PATTERNS
                               if p["hypothesis"].description == c["maps_to"])
                    h = replace(tpl, id=f"H{n}")
                else:
                    combo = c["combo"]
                    h = Hypothesis(
                        id=f"H{n}",
                        description=f"{combo.get('主体', '')}×{combo.get('行为', '')}"
                                    f"（枚举候补，无数据命中）",
                        evidence_needed=["待正兵明确所需证据"],
                        data_sources=["待正兵指定数据源"],
                        procedure="待正兵明确程序",
                        falsification="待正兵明确证伪条件",
                        dimension=["行为"],
                    )
                self.add(h)
                self.backlog.pop(i)
                self._log("promote", f"{h.id} ← {candidate_id} {h.description}")
                return h
        raise KeyError(f"候补不存在：{candidate_id}")

    # ---------- 覆盖完整性报告（E） ----------
    def report(self, data_files: list[str] | None = None) -> dict:
        """覆盖完整性量化指标：维度覆盖 + 数据源覆盖 + 间类缺口 + 证据冲突。"""
        r: dict = {"dimension_coverage": self.dimension_coverage()}
        if data_files:
            r["data_source_coverage"] = self.coverage(data_files)
        r["jian_coverage"] = self.jian_coverage()
        r["conflicts"] = self.conflict_check()
        r["enum"] = {"total_combos": self._enum_total, "backlog_size": len(self.backlog)}
        return r

    def to_dict(self) -> dict:
        return {
            "ji": self.ji,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "audit": self.audit,
            "backlog": self.backlog,
        }
