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


def _dim_names(codes, pack: str = "default", base_dir=None) -> list[str]:
    """维度 code → 展示名（维度 code 化后，报警文案须给人看中文名）。

    base_dir 须透传：案件包的本体可能不在默认路径（快照/测试场景），
    不传会去默认路径找、找不到就回落 code，导致**换本体后报警文案露英文**。

    翻译失败回落 code 本身——宁可显示机器标识符，也不能因翻译失败丢信息。
    """
    try:
        from core.ontology_loader import load_dimension_labels
        labels = load_dimension_labels(pack, base_dir)
    except Exception:
        labels = {}
    return [labels.get(str(c)) or str(c) for c in (codes or [])]


# 本体声明 → Hypothesis 的合法字段集（过滤未知键，避免声明笔误炸装载）
_HYPOTHESIS_FIELDS = frozenset(
    f for f in Hypothesis.__dataclass_fields__
    if f != "source_rows")  # source_rows 由 findings 注入，不接受声明


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
    # REQ-D-003：回落默认只放侦查维度抽象类别，不含具体人名/地名（主体从案件数据派生）。
    ENUM_SPACE: dict[str, list[str]] = {
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

    def __init__(self, pack: str = "default", base_dir=None):
        self.pack = pack               # 维度展示名翻译需回查本体声明
        # base_dir：本体根（案件快照场景不在默认路径）。不传则走默认路径，
        # 旧调用零行为变化；传了才能正确装载非默认位置的本体声明。
        self.base_dir = base_dir
        self.hypotheses: list[Hypothesis] = []
        self.ji: dict[str, str] = {}  # 知己栏（证据缺口 / 授权边界）
        self.audit: list[dict] = []   # 人机协同全程审计
        self.backlog: list[dict] = [] # 枚举候补池（未转正候选）
        self._enum_total = 0          # 最近一次枚举的组合总数
        self._last_findings: list[dict] = []  # REQ-G-008：最近一次虚实扫描 findings（经验轨）
        # REQ-G-011/012/025：维度、枚举空间、假设模式库一并改读
        # ontology/<pack> 声明；声明文件缺失时回落类属性内置默认
        #（旧案件包/精简测试包零行为变化）。
        try:
            from core.ontology_loader import (load_dimensions, load_enum_space,
                                              load_hypothesis_patterns)
            from core.wujian import load_wujian
            dims = load_dimensions(pack, base_dir)
            if dims:
                self.DIMENSIONS = dims
            space = load_enum_space(pack, base_dir)
            if space:
                self.ENUM_SPACE = space
            # REQ-G-025：反常→假设的映射知识属领域层（与维度/枚举空间同级），
            # 不属 packs/ 手段层——镜头才是「怎么查」，这里是「什么算可疑」。
            pats = load_hypothesis_patterns(pack, base_dir)
            if pats:
                # 模式库引用本体标识符：object_types 已由装载器解析成
                # data_sources 中文显示名（jians.json source_names），
                # 这里只取 Hypothesis 合法字段，_object_types 等内部键自动滤除。
                self.FINDING_PATTERNS = [
                    {"rule_ids": p.get("rule_ids") or [],
                     "keywords": p.get("keywords") or [],
                     "hypothesis": Hypothesis(**{
                         k: v for k, v in p["hypothesis"].items()
                         if k in _HYPOTHESIS_FIELDS})}
                    for p in pats
                ]
            # P6：五间从已挂载词汇（packs/wujian）读取；无包保留类属性默认
            wj = load_wujian(pack)
            if wj is not None and wj.jian_order:
                self.JIAN_ALL = wj.jian_order
        except Exception:
            pass  # 装载失败保留类属性默认；loader 在 build 期会对正式包硬失败

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
        # REQ-G-008：留存原始 findings，供 dimension_coverage 经验轨口径
        self._last_findings = list(findings or [])
        for f in findings:
            text = f"{f.get('候选虚处', '')}{f.get('依据', '')}"
            # finding 携带的规则 id（R1/R2…）——主力关联，精确且可溯源
            f_rule = str(f.get("rule_id") or f.get("id") or "")
            for p in (patterns or self.FINDING_PATTERNS):
                # ① rule_ids 精确命中（优先于文本匹配）
                # ② rule_ids 未命中 → 回落 keywords 文本弱匹配
                rids = p.get("rule_ids") or []
                if f_rule and rids:
                    if f_rule not in rids:
                        continue
                elif not any(k in text for k in (p.get("keywords") or [])):
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
    def dimension_coverage(self, findings: list[dict] | None = None) -> dict:
        """维度覆盖 = 已覆盖维度 / 5。

        REQ-G-008：双轨口径——
          - 声明轨（declared）：假设里**声明**了哪些维度（理论覆盖）；
          - 经验轨（empirical）：虚实扫描实际命中的 finding 落在哪些维度（实证覆盖）。
        声明覆盖 ≠ 经验覆盖：假设写了维度但扫描无命中，属"有假设无证据"，须可见。
        REQ-G-009：报警阈值 < 改为 <=（4/5=80% 仍缺 1 维，应报警）；alarm_text 枚举缺维名。
        REQ-G-024：实证缺口独立报警（empirical_alarm/empirical_alarm_text）——
          声明缺口="压根没想到"（补假设/人工注入），实证缺口="想到了但没查到"
          （补数据/查检测器是否失效）；两者不共用 alarm，双轨数字可见且可行动。
        """
        if findings is None:
            findings = getattr(self, "_last_findings", None) or []

        def _dims(v) -> set:
            if v is None:
                return set()
            if isinstance(v, str):
                return {v} if v else set()
            return set(v)

        declared = {d for h in self.hypotheses for d in _dims(h.dimension)} & set(self.DIMENSIONS)
        declared_missing = [d for d in self.DIMENSIONS if d not in declared]
        empirical = {d for f in findings for d in _dims(f.get("dimension"))} & set(self.DIMENSIONS)
        empirical_missing = [d for d in self.DIMENSIONS if d not in empirical]

        score = len(declared) / len(self.DIMENSIONS)
        # G-009：4/5=0.80 仍缺 1 维 → 报警（< 改 <=）
        alarm = score <= self.DIMENSION_ALARM if declared_missing else False
        alarm_text = ""
        if alarm:
            alarm_text = (f"假设维度覆盖不完整（{len(declared)}/{len(self.DIMENSIONS)}），"
                          f"缺：{'、'.join(_dim_names(declared_missing, self.pack, self.base_dir))}；"
                          f"建议补充数据或人工注入")
        # G-024：实证缺口独立报警，不与声明轨共用 alarm/alarm_text
        empirical_alarm = bool(empirical_missing)
        empirical_alarm_text = ""
        if empirical_alarm:
            empirical_alarm_text = (
                f"实证维度覆盖不完整：扫描证据仅落 {len(empirical)}/{len(self.DIMENSIONS)} 维，"
                f"缺：{'、'.join(_dim_names(empirical_missing, self.pack, self.base_dir))}；"
                f"已声明假设但无证据产出的维度，"
                f"建议核查数据源或检测器是否失效（零命中规则见 data_absent 诊断）")
        return {
            # 展示名（维度 code 化后，报警文案/UI 需中文名，机器侧仍用 code）
            "missing_labels": _dim_names(declared_missing, self.pack, self.base_dir),
            "empirical_missing_labels": _dim_names(empirical_missing, self.pack, self.base_dir),
            # 兼容既有键（covered/missing/score/alarm/alarm_text 语义不变）
            "covered": sorted(declared), "missing": declared_missing,
            "score": round(score, 2), "alarm": alarm, "alarm_text": alarm_text,
            # G-008 双轨
            "declared_covered": sorted(declared),
            "declared_missing": declared_missing,
            "empirical_covered": sorted(empirical),
            "empirical_missing": empirical_missing,
            # G-024 实证轨独立报警
            "empirical_alarm": empirical_alarm,
            "empirical_alarm_text": empirical_alarm_text,
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
                    actor = combo.get("主体", "")
                    behavior = combo.get("行为", "")
                    head = f"{actor}×{behavior}" if actor else behavior
                    h = Hypothesis(
                        id=f"H{n}",
                        description=f"{head}（枚举候补，无数据命中）",
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
    def report(self, data_files: list[str] | None = None,
               findings: list[dict] | None = None) -> dict:
        """覆盖完整性量化指标：维度覆盖 + 数据源覆盖 + 间类缺口 + 证据冲突。

        findings：实证轨的**全量** findings。不传则用 _last_findings（建
        沙盘时那批）。两者可能不同——沙盘建立时往往只跑了虚实阶段，而
        奇正（R6/R7 时间维度）在后面才跑；不传全量会把已产出的证据漏算成
        "实证缺口"，误导补救动作（本例：time 报缺，实则已有 2 条命中）。
        """
        r: dict = {"dimension_coverage": self.dimension_coverage(findings)}
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


def locate_empirical_gaps(dc: dict, conn=None, pack: str = "default",
                          base_dir=None) -> dict:
    """实证缺口自动定位：把"缺哪一维"翻译成"该干什么"。

    为什么需要
    ----------
    dimension_coverage() 会报实证缺口，但只给一句文案建议：
    "建议核查数据源或检测器是否失效"。正兵看到"实证缺五维"仍不知下一步。

    实证缺口的成因在数据上是可分的，判据全部现成，无需新造：
      ① 数据未接入 —— 该维度对应的语义表零行（含表不存在）
      ② 检测器失效 —— 表有数据，但规则零命中且 zero_type 为
         empty_result_suspect / config_missing
      ③ 无规则覆盖 —— 表有数据、规则也跑了，但本维度没有规则声明
      该假设（属声明缺口，不是实证问题）

    三者补救动作完全不同：补数据 / 查检测器 / 补假设。混在一起报
    等于没报——这正是此前"报了但没人管"的原因。

    返回 {dimension_code: {"cause": ..., "action": ..., "detail": ...}}。
    conn=None（无库/未 BUILD）→ 只给 unknown，不猜。
    """
    from core.ontology_loader import load_dimensions

    missing = list(dc.get("empirical_missing") or [])
    if not missing:
        return {}

    # 维度 → 对象类型（dimensions.json 声明；换本体自动跟随）
    try:
        dims = load_dimensions(pack, base_dir)
    except Exception:
        dims = []
    obj_by_dim: dict[str, list[str]] = {}
    for d in dims:
        code = d.get("code") if isinstance(d, dict) else str(d)
        if isinstance(d, dict):
            obj_by_dim[code] = list(d.get("source_object_types") or [])

    # 维度 → 声明了该维度的规则 id
    rules_by_dim: dict[str, list[str]] = {}
    try:
        from core.ontology_loader import load_pack
        spec = load_pack(pack, base_dir=base_dir)
        for r in (spec.rules or {}).values():
            rd = getattr(r, "dimension", "") or ""
            if rd in missing:
                rules_by_dim.setdefault(rd, []).append(str(getattr(r, "id", "")))
    except Exception:
        pass

    # 规则零命中诊断（zero_type 由 rules._classify_zero 分类，落 detail JSON）
    zero_by_rule: dict[str, str] = {}
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT source, detail FROM run_diagnostic "
                "WHERE kind='rule_zero_hit'").fetchall()
            for src, det in rows:
                sid = str(src or "")
                if not sid.startswith("rule:"):
                    continue
                zt = ""
                try:
                    zt = str((json.loads(det) or {}).get("zero_type") or "")
                except Exception:
                    zt = ""
                if zt:
                    zero_by_rule[sid[len("rule:"):]] = zt
        except Exception:
            pass

    out: dict[str, dict] = {}
    if conn is None:
        # 无库连接（未 BUILD/只读降级）：既不查表也不查诊断 → 一律 unknown。
        # 不猜：凭声明就断言"数据缺失"或"检测器失效"会误导补救动作。
        return {dim: {"cause": "unknown",
                      "action": "需连接数据库后判定",
                      "detail": "无库连接，无法区分数据缺口与检测器失效"}
                for dim in missing}

    for dim in missing:
        objs = obj_by_dim.get(dim) or []
        tables = [f"obj_{o}" for o in objs]
        rows_by_tbl: dict[str, int] = {}
        if conn is not None:
            for t in tables:
                try:
                    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    rows_by_tbl[t] = int(n or 0)
                except Exception:
                    rows_by_tbl[t] = -1  # 表不存在

        # ① 数据未接入：全部相关表零行或不存在
        if rows_by_tbl and all(v <= 0 for v in rows_by_tbl.values()):
            out[dim] = {
                "cause": "data_absent",
                "action": "补数据接入",
                "detail": (f"维度「{dim}」相关语义表 "
                           f"{'、'.join(tables)} 均无数据行"),
                "tables": tables,
            }
            continue

        # ② 按规则零命中原因（zero_type）定位——比"表有没有数据"更准：
        #    对象表有数据不代表链路完整，R4 依赖 lnk_co_located（0 行）而
        #    obj_trackpoint 有 7 行，只看对象表会误判为"有数据但没命中"。
        dim_rules = rules_by_dim.get(dim, [])
        zt_by_rule = {r: zero_by_rule.get(r) or "" for r in dim_rules}
        failed = [r for r, z in zt_by_rule.items()
                  if z in ("empty_result_suspect", "config_missing")]
        absent = [r for r, z in zt_by_rule.items() if z == "data_absent"]
        if failed:
            out[dim] = {
                "cause": "detector_failed",
                "action": "查检测器",
                "detail": (f"维度「{dim}」有数据，但规则 "
                           f"{'、'.join(failed)} 零命中疑似匹配失效"
                           f"（zero_type={zt_by_rule[failed[0]]}）"),
                "rules": failed,
            }
            continue
        if absent:
            # 走到这里 failed 必为空；该维度有规则卡在数据缺失 →
            # 数据链路断了（对象表可能有数据，多为链接表未构建）
            out[dim] = {
                "cause": "data_absent",
                "action": "补数据接入",
                "detail": (f"维度「{dim}」对象表有数据，但规则 "
                           f"{'、'.join(absent)} 依赖的数据缺失"
                           f"（zero_type=data_absent，多为链接表未构建）"),
                "rules": absent,
            }
            continue

        # ③ 兜底：有数据、有规则声明，但既无命中也无失效诊断
        if not dim_rules:
            out[dim] = {
                "cause": "no_rule_for_dimension",
                "action": "补规则",
                "detail": f"维度「{dim}」无规则声明，无法产生该维度证据",
            }
        elif any(zt_by_rule.values()):
            # 规则跑了且落了诊断，但不是失效类（如 clean_scan=正常空）
            out[dim] = {
                "cause": "no_evidence_for_hypothesis",
                "action": "补假设或核对判据",
                "detail": (f"维度「{dim}」规则 "
                           f"{'、'.join(dim_rules)} 已执行但无证据产出"
                           f"（zero_type={','.join(sorted(set(filter(None, zt_by_rule.values())))) or '—'}），"
                           f"多为假设与判据口径不匹配"),
                "rules": dim_rules,
            }
        else:
            # 无零命中诊断记录 → 规则本轮未执行（不在扫描阶段/未启用）
            out[dim] = {
                "cause": "rule_not_run",
                "action": "确认规则是否纳入扫描",
                "detail": (f"维度「{dim}」规则 "
                           f"{'、'.join(dim_rules)} 本轮无执行记录"
                           f"（无 rule_zero_hit 诊断），"
                           f"可能未纳入当前扫描阶段或未启用"),
                "rules": dim_rules,
            }

    return out


def record_dimension_gaps(dc: dict, health) -> None:
    """把 dimension_coverage() 的双轨缺口落运行诊断（REQ-G-024）。

    两条缺口独立留痕、source 区分，指向不同补救动作：
      - miaosuan:dimension            声明缺口（"压根没想到"）→ 补假设 / 人工注入
      - miaosuan:dimension:empirical  实证缺口（"想到了但没查到"）→ 补数据 / 查检测器
    旧逻辑只在声明轨 alarm 分支内 record，声明 100% 时实证缺口被门控掉、
    健康度虚假 healthy；本函数两条分支互不依赖。
    health=None → NullRunHealth 空操作（兼容红线）。
    """
    from core.run_health import get_health  # 局部导入，与 core.metrics 惯例一致
    h = get_health(health)
    if dc.get("alarm"):
        h.record("coverage_gap", "warning",
                 source="miaosuan:dimension",
                 reason=dc.get("alarm_text") or "庙算维度覆盖缺口",
                 missing=dc.get("missing"),
                 empirical_missing=dc.get("empirical_missing"))
    if dc.get("empirical_alarm"):
        h.record("coverage_gap", "warning",
                 source="miaosuan:dimension:empirical",
                 reason=dc.get("empirical_alarm_text") or "庙算实证维度覆盖缺口",
                 missing=dc.get("empirical_missing"))


def record_empirical_gap_causes(dc: dict, conn=None, pack: str = "default",
                                base_dir=None, health=None) -> dict:
    """实证缺口 → 逐维定位 → 落诊断（可行动版）。

    与 record_dimension_gaps 的区别：那条只报"缺哪几维"，本条回答
    "每一维该干什么"——补数据 / 查检测器 / 补规则 / 补假设，并逐维
    落一条 miaosuan:gap:empirical:<dim> 诊断，正兵可按维处置。
    """
    from core.run_health import get_health

    located = locate_empirical_gaps(dc, conn=conn, pack=pack,
                                    base_dir=base_dir)
    if not located:
        return {}
    h = get_health(health)
    for dim, info in located.items():
        # kind 复用 coverage_gap（已在 run_health.KINDS 内）；按维度拆 source，
        # 与 record_dimension_gaps 的双轨 source 同族，正兵可按维处置。
        h.record("coverage_gap",
                 "warning" if info["cause"] != "unknown" else "info",
                 source=f"miaosuan:gap:empirical:{dim}",
                 reason=f"{info['detail']} → 建议：{info['action']}",
                 dimension=dim, cause=info["cause"], action=info["action"])
    return located
