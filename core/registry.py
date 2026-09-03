"""
core/registry.py
统一技能注册表 + skill_invoke() 调用接口。

设计目标（第 3 项）：
  把五子技能统一封装成 skill_invoke() 接口
  → 输入假设(MiaoSuan) → 输出带血缘的线索(LineageClue)
  → 供正兵操作台统一调用，而非各自 run(ctx, store)

核心概念：
  SkillSpec   : 一个子技能的元数据（id / 调用标识 / 所消费的间类 / 数据依赖）
  LineageClue : 一条带血缘的线索（假设溯源 + 数据溯源 + 间类归属 + 处置状态）
  SkillRegistry: 全局注册表，子技能在此登记
  skill_invoke : 统一入口，按 skill_id 分发，返回 [LineageClue]

Lineage(血缘) = 假设链(assumption_chain) + 数据行(source_rows) + 间类(jian_types)
这是「奇兵只拓线不出定性」的强制落地：每条线索都能追到
「哪条假设推出来 + 用了哪些原始行」。

处置状态（正兵跟踪用，AI 不自动改变定性）：
  status = 待查 / 查证中 / 已排除 / 已固证
  仅在「已固证 且 法定程序完备」的前提下，正兵显式调用 set_filed() 标为 已立案
  —— 这是红线字段，AI 严禁自行置为「已立案」。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional


# ----------------------------------------------------------------------
# 处置状态常量（状态机合法迁移见 ClueStatusMachine）
# ----------------------------------------------------------------------

class ClueStatus:
    PENDING = "待查"        # 初始状态：奇兵产出，待正兵处置
    VERIFYING = "查证中"     # 正兵已接手核查
    EXCLUDED = "已排除"      # 经查证不成立（需填 note）
    CONFIRMED = "已固证"     # 经查证成立，形成稳定证据
    FILED = "已立案"        # 仅「已固证 + 法定程序完备」可由正兵显式置位

    # 允许的目标状态集合
    ALLOWED = {PENDING, VERIFYING, EXCLUDED, CONFIRMED, FILED}

    # 可由 AI / 自动化设定的状态（不含 已立案，那是受控红线）
    MACHINE_SETTABLE = {PENDING, VERIFYING, EXCLUDED, CONFIRMED}


class ClueStatusMachine:
    """线索处置状态迁移校验。"""

    # 合法迁移表：current -> {allowed next}
    _TRANSITIONS = {
        ClueStatus.PENDING:   {ClueStatus.VERIFYING, ClueStatus.EXCLUDED, ClueStatus.CONFIRMED},
        ClueStatus.VERIFYING: {ClueStatus.PENDING, ClueStatus.EXCLUDED, ClueStatus.CONFIRMED},
        ClueStatus.EXCLUDED:  {ClueStatus.PENDING},        # 排除后可因新证据重开
        ClueStatus.CONFIRMED: {ClueStatus.EXCLUDED, ClueStatus.FILED},
        ClueStatus.FILED:     set(),                        # 终态
    }

    @classmethod
    def can_transition(cls, current: str, target: str) -> bool:
        return target in cls._TRANSITIONS.get(current, set())

    @classmethod
    def validate(cls, current: str, target: str) -> None:
        if target not in ClueStatus.ALLOWED:
            raise ValueError(f"非法处置状态：{target}，允许 {sorted(ClueStatus.ALLOWED)}")
        if not cls.can_transition(current, target):
            raise ValueError(f"非法状态迁移：{current} → {target}（见 ClueStatusMachine）")


@dataclass
class StatusAuditEntry:
    """单条状态变更审计记录。"""
    from_status: str
    to_status: str
    operator: str          # 操作人/主体；AI 自动化时应为 "system" 或具体技能
    note: str = ""
    timestamp: str = field(default_factory=lambda: _now())

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------

@dataclass
class SkillSpec:
    """子技能元数据。注册时声明，运行时供调度器读取。"""
    skill_id: str                     # 调用标识，如 "xu_shi"
    name: str                         # 中文名
    stage: str                        # 所属阶段：庙算/知己/虚实/奇正/用间/全胜
    consumes_jian: list[str] = field(default_factory=list)  # 消费的间类
    data_deps: list[str] = field(default_factory=list)       # 依赖的数据源
    handler: Optional[Callable] = None                        # 实际执行函数

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("handler", None)
        return d


@dataclass
class LineageClue:
    """
    带血缘的线索 —— 技能输出的标准单元。

    字段说明：
      assumption_chain : 这条线索由哪些假设(H1..Hn)推演而来；空 = 自动发现(无假设驱动)
      source_rows      : 溯源到原始数据的行标识（file + row_id / sql 主键）
      jian_types       : 归属的间类（因间/生间/反间/死间/内间），用于用间交叉升格
     定性策略          : 定性字段不允许由 AI 填写，仅记录「待正兵核实」
      status          : 处置状态（待查/查证中/已排除/已固证/已立案）
      audit_log       : 状态变更审计链，保证处置过程可追溯
      note            : 正兵备注（排除理由 / 固证要点等）
    """
    clue_id: str = field(default_factory=lambda: f"clue_{uuid.uuid4().hex[:8]}")
    skill_id: str = ""                                    # 产出该线索的技能
    title: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    assumption_chain: list[str] = field(default_factory=list)   # ["H1", "H4"]
    source_rows: list[dict[str, Any]] = field(default_factory=list)
    jian_types: list[str] = field(default_factory=list)
    needs_human_review: bool = True                        # 默认一律需人工复核
    定性_policy: str = "AI 不给出定性，须言词证据+法定程序"
    # ---- 处置状态（正兵跟踪，新增）----
    status: str = ClueStatus.PENDING
    audit_log: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    # ------------------------------------------------------------------
    # 状态变更（唯一入口，禁止直接赋值 status —— 保证审计链完整）
    # ------------------------------------------------------------------
    def set_status(self, target: str, operator: str = "正兵", note: str = "",
                   audit_chain=None) -> "LineageClue":
        """
        迁移处置状态。operator 标识操作主体；AI/自动化请传具体技能名或 "system"。
        已立案 为受控终态，须通过 set_filed() 显式置位，不走此方法。

        audit_chain：传入 core.audit.AuditChain 实例时，同步落持久哈希链；
                     None 时仅写内存 audit_log（向后兼容）。
        """
        if target == ClueStatus.FILED:
            raise ValueError("「已立案」为受控红线状态，须调用 set_filed()，禁止经 set_status 设置")
        ClueStatusMachine.validate(self.status, target)
        self.audit_log.append(StatusAuditEntry(self.status, target, operator, note).to_dict())
        if audit_chain is not None:
            audit_chain.append(
                operator=operator,
                before={"status": self.status},
                after={"status": target, "note": note},
                source_row_ids=[json.dumps(r, ensure_ascii=False, default=str)
                                if not isinstance(r, str) else r
                                for r in self.source_rows],
                ontology_version=audit_chain.current_ontology_version())
        self.status = target
        if note:
            self.note = note
        return self

    def set_filed(self, operator: str, legal_basis: str,
                  audit_chain=None) -> "LineageClue":
        """
        受控置位「已立案」：前置条件为 已固证 且 法定程序完备。
        legal_basis 记录法定依据（案号/审批文号），纳入审计链。

        audit_chain：传入 core.audit.AuditChain 实例时，同步落持久哈希链。
        """
        if self.status not in (ClueStatus.CONFIRMED, ClueStatus.FILED):
            raise ValueError(f"「已立案」须由「已固证」迁移，当前状态={self.status}")
        note = f"法定程序完备：{legal_basis}"
        self.audit_log.append(StatusAuditEntry(self.status, ClueStatus.FILED, operator, note).to_dict())
        if audit_chain is not None:
            audit_chain.append(
                operator=operator,
                before={"status": self.status},
                after={"status": ClueStatus.FILED, "legal_basis": legal_basis},
                source_row_ids=[json.dumps(r, ensure_ascii=False, default=str)
                                if not isinstance(r, str) else r
                                for r in self.source_rows],
                ontology_version=audit_chain.current_ontology_version())
        self.status = ClueStatus.FILED
        self.note = note
        return self

    def is_active(self) -> bool:
        """是否仍处于「需跟进」状态（已排除/已立案 为终态，不再占用正兵注意力）。"""
        return self.status not in (ClueStatus.EXCLUDED, ClueStatus.FILED)

    def summary(self) -> str:
        return (
            f"[{self.skill_id}] {self.title} "
            f"| 假设={self.assumption_chain} 间={self.jian_types} "
            f"| 溯源{len(self.source_rows)}行 "
            f"| 状态={self.status}"
        )


# ----------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------

class SkillRegistry:
    """全局技能注册表，线程不安全但单机流程无需锁。"""

    def __init__(self):
        self._specs: dict[str, SkillSpec] = {}

    # ---- 注册 ----
    def register(self, spec: SkillSpec) -> SkillSpec:
        if spec.skill_id in self._specs:
            raise ValueError(f"技能 {spec.skill_id} 已注册，不可重复")
        self._specs[spec.skill_id] = spec
        return spec

    def skill(self, skill_id: str) -> SkillSpec:
        if skill_id not in self._specs:
            raise KeyError(f"未注册的技能：{skill_id}，可用 {list(self._specs)}")
        return self._specs[skill_id]

    # ---- 查询 ----
    def all_specs(self) -> list[SkillSpec]:
        return list(self._specs.values())

    def by_stage(self, stage: str) -> list[SkillSpec]:
        return [s for s in self._specs.values() if s.stage == stage]

    def by_jian(self, jian: str) -> list[SkillSpec]:
        """哪些技能能为某间类供线索（用间交叉时调用）。"""
        return [s for s in self._specs.values() if jian in s.consumes_jian]

    def __contains__(self, skill_id: str) -> bool:
        return skill_id in self._specs


# ----------------------------------------------------------------------
# 统一调用入口
# ----------------------------------------------------------------------

def skill_invoke(
    registry: SkillRegistry,
    skill_id: str,
    *,
    miao: Any = None,            # MiaoSuan 实例（提供假设 + 知己）
    store: Any = None,           # Store 实例（L1/L2/L3）
    ctx: dict | None = None,     # 运行上下文（可用数据/未调取/缺口等）
    params: dict | None = None,  # 技能私有参数（如 target_person）
) -> list[LineageClue]:
    """
    统一调用入口。

    流程：
      1. 解析 skill_id（支持 "stage.skill" 或裸 "skill_id"）
      2. 校验前置条件（知己非空 / store 可用）
      3. 调用 handler(miao, store, ctx, params) -> [LineageClue]
      4. 后处理：补齐 assumption_chain（若技能未填，则按数据依赖反推）
      5. 写入 L1 特征层（供用间交叉消费）

    参数:
        registry : 技能注册表
        skill_id : 调用标识
        miao     : MiaoSuan 实例
        store    : Store 实例
        ctx      : 运行上下文
        params   : 技能私有参数

    返回:
        [LineageClue] —— 该技能产出的所有带血缘线索
    """
    ctx = ctx or {}
    params = params or {}

    # 1. 解析 id
    sid = skill_id.split(".")[-1]
    spec = registry.skill(sid)

    # 2. 前置校验
    _precheck(spec, miao=miao, store=store)

    # 3. 调用 handler
    handler = spec.handler
    if handler is None:
        raise RuntimeError(f"技能 {sid} 已注册但未绑定 handler")
    raw = handler(miao=miao, store=store, ctx=ctx, params=params)

    # 归一化：允许 handler 返回 (clues, meta) 或纯 list
    clues = _normalize(raw)

    # 4. 后处理：补齐血缘 + 写 L1
    for c in clues:
        if not c.assumption_chain:
            c.assumption_chain = _infer_assumptions(c, spec, miao)
        if not c.jian_types:
            c.jian_types = list(spec.consumes_jian)
        if store is not None:
            store.set_feature(f"_clue:{c.clue_id}", "lineage", c.to_dict())

    return clues


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------

def _precheck(spec: SkillSpec, *, miao: Any, store: Any) -> None:
    """前置条件校验：非庙算/知己阶段必须先有知己(ji)，否则无法界定证据缺口。"""
    if spec.stage in ("庙算", "知己"):
        return
    if miao is not None and hasattr(miao, "ji") and not miao.ji:
        raise ValueError(f"技能 {spec.skill_id} 调用前必须先完成『知己』(miao.ji 非空)")
    if store is None and spec.data_deps:
        raise ValueError(f"技能 {spec.skill_id} 声明了数据依赖 {spec.data_deps}，但未提供 store")


def _normalize(raw: Any) -> list[LineageClue]:
    """归一化 handler 返回值。"""
    if isinstance(raw, tuple):
        raw = raw[0]  # 兼容 (clues, extra_meta)
    if raw is None:
        return []
    if isinstance(raw, LineageClue):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, LineageClue) for x in raw):
        return raw
    raise TypeError(f"handler 返回类型不合法：{type(raw)}，应为 [LineageClue]")


def _infer_assumptions(clue: LineageClue, spec: SkillSpec, miao: Any) -> list[str]:
    """
    若技能未显式填 assumption_chain，按数据依赖反推：
    取 miao.hypotheses 中 data_sources 与本技能 data_deps 交集非空者。
    这是「假设覆盖完整性」的兜底 —— 确保每条线索都能挂回某条假设。
    """
    if miao is None or not hasattr(miao, "hypotheses"):
        return []
    chain: list[str] = []
    deps = set(spec.data_deps)
    for h in miao.hypotheses:
        if deps & set(getattr(h, "data_sources", [])):
            chain.append(h.id)
    return chain


# ----------------------------------------------------------------------
# 全局单例（方便正兵操作台直接 import）
# ----------------------------------------------------------------------

DEFAULT_REGISTRY = SkillRegistry()


def get_registry() -> SkillRegistry:
    return DEFAULT_REGISTRY


def reset_registry() -> SkillRegistry:
    """测试/重置用。"""
    global DEFAULT_REGISTRY
    DEFAULT_REGISTRY = SkillRegistry()
    return DEFAULT_REGISTRY


# ----------------------------------------------------------------------
# 人名实体对齐：从 Store(DuckDB) 采集（供 core.entity 一站式入口调用）
# ----------------------------------------------------------------------
def _resolve_person_from_store(store) -> "EntityResolver":
    """
    从 DuckDB 中采集「人名类」实体记录，跑通人名对齐。
    采集范围：银行流水(主体/对方)、通话记录(主体/对端)、招投标档案(分管领导)。
    返回已 ingest 但未 resolve 的 EntityResolver（调用方再 add_aliases + resolve）。
    """
    # 延迟导入：entity_resolution 与 core.registry 互相解耦
    # 复用 core.entity 的路径安全加载器（按绝对路径加载，避免同名包遮蔽 sys.path）
    from .entity import _load_person_resolver
    EntityResolver = _load_person_resolver()
    resolver = EntityResolver()
    conn = getattr(store, "conn", None)
    if conn is None:
        return resolver

    table_cols = [
        ("银行流水", "主体", None),
        ("银行流水", "对方", None),
        ("通话记录", "主体", None),
        ("通话记录", "对端", None),
        ("招投标档案", "分管领导", None),
    ]
    seen: set[tuple[str, str]] = set()   # (name, type) 去重，避免同名人被当作共享证据
    records: list[dict] = []
    for table, col, _ in table_cols:
        try:
            rows = conn.execute(
                f'SELECT DISTINCT "{col}" AS name FROM "{table}" WHERE "{col}" IS NOT NULL'
            ).fetchall()
        except Exception:
            continue   # 表/列不存在则跳过，容错
        for (name,) in rows:
            key = (str(name), "person")
            if key in seen:
                continue
            seen.add(key)
            records.append({"name": name, "type": "person", "source_row_id": f"{table}.{col}"})

    resolver.ingest(records)
    return resolver
