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

from core.run_health import get_health

try:
    from duckdb import Error as _DuckDBError
    _RUNTIME_EXC: tuple = (ValueError, KeyError, IndexError, AttributeError,
                           TypeError, RuntimeError, OSError, _DuckDBError)
except ImportError:  # pragma: no cover
    _RUNTIME_EXC = (ValueError, KeyError, IndexError, AttributeError,
                    TypeError, RuntimeError, OSError)


# ----------------------------------------------------------------------
# 处置状态常量（状态机合法迁移见 ClueStatusMachine）
# ----------------------------------------------------------------------

class ClueStatus:
    PENDING = "待查"        # 初始状态：奇兵产出，待正兵处置
    VERIFYING = "查证中"     # 正兵已接手核查
    EXCLUDED = "已排除"      # 经查证不成立（需填 note）
    CONFIRMED = "已固证"     # 经查证成立，形成稳定证据
    FILED = "已立案"        # 仅「已固证 + 法定程序完备」可由正兵显式置位

    # 允许的目标状态集合（R6：默认值；正式包从 states.json 读取）
    ALLOWED = {PENDING, VERIFYING, EXCLUDED, CONFIRMED, FILED}

    # 可由 AI / 自动化设定的状态（不含 已立案，那是受控红线）
    MACHINE_SETTABLE = {PENDING, VERIFYING, EXCLUDED, CONFIRMED}

    @classmethod
    def from_pack(cls, pack: str = "default") -> list[dict]:
        """R6：从 states.json 读取状态声明列表。"""
        from core.ontology_loader import load_states
        return load_states(pack)["states"]

    @classmethod
    def terminal_states(cls, pack: str = "default") -> set[str]:
        """R6：受控终态集合（terminal=True）。"""
        return {s["name"] for s in cls.from_pack(pack) if s.get("terminal")}

    @classmethod
    def human_only_states(cls, pack: str = "default") -> set[str]:
        """R6：requires_role=human 的状态集合（AI/机器无权置位）。"""
        return {s["name"] for s in cls.from_pack(pack)
                if s.get("requires_role") == "human"}


class ClueStatusMachine:
    """线索处置状态迁移校验。"""

    # 合法迁移表（R6：默认值；正式包从 states.json 读取）
    _TRANSITIONS = {
        ClueStatus.PENDING:   {ClueStatus.VERIFYING, ClueStatus.EXCLUDED, ClueStatus.CONFIRMED},
        ClueStatus.VERIFYING: {ClueStatus.PENDING, ClueStatus.EXCLUDED, ClueStatus.CONFIRMED},
        ClueStatus.EXCLUDED:  {ClueStatus.PENDING},        # 排除后可因新证据重开
        ClueStatus.CONFIRMED: {ClueStatus.EXCLUDED, ClueStatus.FILED},
        ClueStatus.FILED:     set(),                        # 终态
    }

    @classmethod
    def transitions_for(cls, pack: str = "default") -> dict[str, set[str]]:
        """R6：从 states.json 读取迁移表。"""
        from core.ontology_loader import load_states
        return load_states(pack)["transitions"]

    @classmethod
    def can_transition(cls, current: str, target: str,
                       pack: str = "default") -> bool:
        trans = cls.transitions_for(pack)
        return target in trans.get(current, set())

    @classmethod
    def validate(cls, current: str, target: str,
                 pack: str = "default") -> None:
        states = {s["name"] for s in ClueStatus.from_pack(pack)}
        if target not in states:
            raise ValueError(f"非法处置状态：{target}，允许 {sorted(states)}")
        if not cls.can_transition(current, target, pack):
            raise ValueError(f"非法状态迁移：{current} → {target}（见 ClueStatusMachine）")


@dataclass
class StatusAuditEntry:
    """单条状态变更审计记录。

    event_id：关联 audit_chain 持久哈希链中的对应事件（P0-3 哈希链线索级）。
              仅写内存 audit_log 时为空串；经 audit_chain 落链时填入返回值。
    """
    from_status: str
    to_status: str
    operator: str          # 操作人/主体；AI 自动化时应为 "system" 或具体技能
    note: str = ""
    timestamp: str = field(default_factory=lambda: _now())
    event_id: str = ""

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
    """子技能元数据。注册时声明，运行时供调度器读取。

    P3 新增字段（全部带默认值，旧规格零改动）：
      consumes_objects : 消费的语义对象/链接类型（能力声明主口径）
      produces_dims    : 产出的评分维度标签
      mode             : deterministic（可复现，直接推演）| draft（不可复现，人验才成证据）
      enabled          : 单镜头开关（灰度/停用/吊销）
      params_schema    : 任务声明 {参数名: {type, enum?, required?}}；空=仅被动上报
      external_services: draft 镜头依赖的外部服务标识；确定性镜头恒空
      timeout_ms       : 外部调用超时；0=不适用
      result_ttl_s     : 产出新鲜期；0=不适用
      scope_reads      : 镜头查询通道可读的语义类型；缺省 []=零读取权（fail-closed）
      pack_id          : 来源包标识；内置技能为 "_builtin"
    """
    skill_id: str                     # 调用标识，如 "xu_shi"
    name: str                         # 中文名
    stage: str                        # 所属阶段：庙算/知己/虚实/奇正/用间/全胜
    consumes_jian: list[str] = field(default_factory=list)  # 消费的间类
    data_deps: list[str] = field(default_factory=list)       # 依赖的数据源
    handler: Optional[Callable] = None                        # 实际执行函数
    # ---- P3 镜头契约 ----
    consumes_objects: list[str] = field(default_factory=list)
    # 镜头用途说明（业务话术，声明在 pack.json；启停面板/运行弹窗据此向用户
    # 解释"这个镜头干什么"——此前只有中文名，用户只能盲开关）。
    # 缺失时由 UI 层按 produces_dims/consumes_objects 自动兜底描述，不硬编码。
    description: str = ""
    produces_dims: list[str] = field(default_factory=list)
    mode: str = "deterministic"
    enabled: bool = True
    params_schema: dict[str, Any] = field(default_factory=dict)
    external_services: list[str] = field(default_factory=list)
    timeout_ms: int = 0
    result_ttl_s: int = 0
    scope_reads: list[str] = field(default_factory=list)
    pack_id: str = "_builtin"

    MODES = ("deterministic", "draft")
    PARAM_TYPES = ("string", "integer", "decimal", "date", "boolean")

    def validate(self) -> None:
        """注册期内部一致性校验（硬失败）。

        跨文件的引用存在性（consumes_objects/scope_reads 指向的底座类型）由
        pack_loader 在注册前对照 ontology 校验，不在此处。
        """
        if self.mode not in self.MODES:
            raise ValueError(
                f"技能 {self.skill_id} 非法 mode={self.mode!r}，允许 {self.MODES}")
        if self.mode == "draft":
            if not self.external_services:
                raise ValueError(
                    f"draft 镜头 {self.skill_id} 必须声明 external_services")
            if self.timeout_ms <= 0:
                raise ValueError(
                    f"draft 镜头 {self.skill_id} 必须声明正整数 timeout_ms")
            if self.result_ttl_s <= 0:
                raise ValueError(
                    f"draft 镜头 {self.skill_id} 必须声明正整数 result_ttl_s")
        elif (self.external_services or self.timeout_ms != 0
              or self.result_ttl_s != 0):
            raise ValueError(
                f"deterministic 镜头 {self.skill_id} 的 external_services/"
                f"timeout_ms/result_ttl_s 必须为空/0")
        if not isinstance(self.params_schema, dict):
            raise ValueError(
                f"技能 {self.skill_id} params_schema 必须为对象 dict")
        for pname, pspec in self.params_schema.items():
            if not isinstance(pspec, dict):
                raise ValueError(
                    f"技能 {self.skill_id} 参数 {pname} 声明必须为对象 dict")
            if pspec.get("type") not in self.PARAM_TYPES:
                raise ValueError(
                    f"技能 {self.skill_id} 参数 {pname} type="
                    f"{pspec.get('type')!r}，允许 {self.PARAM_TYPES}")
        if self.consumes_objects:
            extra = set(self.scope_reads) - set(self.consumes_objects)
            if extra:
                raise ValueError(
                    f"技能 {self.skill_id} scope.reads {sorted(extra)} "
                    f"超出 consumes_objects 声明")

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
    # ---- P3 结构化证据引用 ----
    # 每条：{"kind": "node"|"edge"|"time_window"|"aggregate"|"file",
    #       "ref": "obj_org#id"/"lnk_transfers#id", "file_uri"/...}
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LineageClue":
        """从 dict 重建线索；旧记录缺新字段（如 evidence_refs）时回落默认值。"""
        import dataclasses as _dc
        names = {f.name for f in _dc.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

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
        # P0-3：先落持久哈希链拿 event_id，再写入内存 audit_log（关联受保护事件）
        event_id = ""
        if audit_chain is not None:
            event_id = audit_chain.append(
                operator=operator,
                before={"status": self.status, "clue_id": self.clue_id},
                after={"status": target, "note": note, "clue_id": self.clue_id},
                source_row_ids=[json.dumps(r, ensure_ascii=False, default=str)
                                if not isinstance(r, str) else r
                                for r in self.source_rows],
                ontology_version=audit_chain.current_ontology_version())
        self.audit_log.append(StatusAuditEntry(
            self.status, target, operator, note, event_id=event_id).to_dict())
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
        # P0-3：先落持久哈希链拿 event_id，再写入内存 audit_log（关联受保护事件）
        event_id = ""
        if audit_chain is not None:
            event_id = audit_chain.append(
                operator=operator,
                before={"status": self.status, "clue_id": self.clue_id},
                after={"status": ClueStatus.FILED, "legal_basis": legal_basis,
                       "clue_id": self.clue_id},
                source_row_ids=[json.dumps(r, ensure_ascii=False, default=str)
                                if not isinstance(r, str) else r
                                for r in self.source_rows],
                ontology_version=audit_chain.current_ontology_version())
        self.audit_log.append(StatusAuditEntry(
            self.status, ClueStatus.FILED, operator, note,
            event_id=event_id).to_dict())
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
        spec.validate()
        self._specs[spec.skill_id] = spec
        return spec

    def unregister(self, skill_id: str) -> None:
        """注销技能（镜头包拔出/吊销）；不存在静默忽略。"""
        self._specs.pop(skill_id, None)

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
    health=None,                 # REQ-G-010 运行诊断（None → NullRunHealth）
) -> list[LineageClue]:
    """
    统一调用入口。

    流程：
      1. 解析 skill_id（支持 "stage.skill" 或裸 "skill_id"）
      2. enabled=false → 调度短路（记 skill_disabled）
      3. 前置校验 + params 与 params_schema 双向核对（硬失败，不隔离）
      4. 调用 handler：运行期异常白名单 → 失败隔离（skill_failed 留痕 +
         ctx["degraded"] 清单），返回空线索，不影响其余镜头
      5. 归一化 + 后处理：补齐血缘、evidence_refs 契约校验、写 L1 特征层

    参数:
        registry : 技能注册表
        skill_id : 调用标识
        miao     : MiaoSuan 实例
        store    : Store 实例
        ctx      : 运行上下文
        params   : 技能私有参数
        health   : 运行诊断（None → NullRunHealth）

    返回:
        [LineageClue] —— 该技能产出的所有带血缘线索；镜头失败/停用时为 []
    """
    if ctx is None:
        ctx = {}
    params = dict(params or {})
    h = get_health(health)

    # 1. 解析 id
    sid = skill_id.split(".")[-1]
    spec = registry.skill(sid)

    # 1.5 参数自动填充：必填缺失时按 pack.json 的 auto_from 声明推导靶心。
    # 定向镜头（relation_*/timeline_*）此前因必填缺失在批量阶段被整体跳过，
    # 与"自动研判"目标相悖；显式传入的参数优先，不做覆盖。
    _param_source = params.pop("_param_source", "")
    if any(p.get("required") and p_name not in params
           for p_name, p in (spec.params_schema or {}).items()):
        try:
            from core.focus import auto_fill_params
            combos, sources = auto_fill_params(spec, store, ctx, health=health)
            if combos:
                filled = dict(combos[0])
                src = filled.pop("_param_source", "")
                for k, v in filled.items():
                    params.setdefault(k, v)
                _param_source = _param_source or src
        except Exception:
            pass  # 推导失败不隔离：继续走 _validate_params 的既有硬失败口径

    # 2. 单镜头开关：调度短路（灰度/停用/吊销）
    if not spec.enabled:
        h.record("skill_disabled", severity="info", source=sid,
                 reason=f"镜头 {sid} 已停用，调度短路")
        return []

    # 3. 前置校验 + 参数核对（契约/配置错误，硬失败不走隔离）
    _precheck(spec, miao=miao, store=store)
    _validate_params(spec, params)

    # 4. 调用 handler（仅运行期数据异常被隔离；注册期/契约硬失败照常抛出）
    handler = spec.handler
    if handler is None:
        raise RuntimeError(f"技能 {sid} 已注册但未绑定 handler")
    try:
        raw = handler(miao=miao, store=store, ctx=ctx, params=params, health=health)
    except _RUNTIME_EXC as e:
        h.record("skill_failed", severity="warning", source=sid,
                 reason=f"{type(e).__name__}: {e}", skill_id=sid)
        ctx.setdefault("degraded", []).append({
            "skill_id": sid, "error_type": type(e).__name__,
            "error": str(e)})
        return []

    # 归一化：允许 handler 返回 (clues, meta) 或纯 list
    clues = _normalize(raw)

    # 自动填参溯源：每条线索记 param_source（靶心从哪来），可审计、可复算
    if _param_source:
        for c in clues:
            c.detail.setdefault("param_source", _param_source)

    # 5. 后处理：补齐血缘 + evidence_refs 契约校验 + 写 L1
    for c in clues:
        if not c.assumption_chain:
            c.assumption_chain = _infer_assumptions(c, spec, miao)
        if not c.jian_types:
            # P6：间类不再由技能自报（spec.consumes_jian），改为融合层按
            # evidence_refs 源对象类型 → packs/wujian 映射反查注入；无包留空。
            c.jian_types = _infer_jian_types(c, ctx)
        _validate_evidence_refs(c, store)
        if store is not None:
            store.set_feature(f"_clue:{c.clue_id}", "lineage", c.to_dict())

    return clues


def scoped_rows(spec: SkillSpec, access: Any, store: Any, object_type: str,
                *, where: str = "", params: list | None = None,
                policy_pack: str = "default") -> list[dict]:
    """
    镜头查询通道：scope_reads 作用域 + policies 属性遮蔽的只读读取（P3）。

      - object_type 不在 spec.scope_reads → PermissionError（缺省零读取权，
        fail-closed；须在 pack.json scope.reads 显式声明）
      - access 为 AccessContext，查询结果按属性级策略遮蔽；system 角色旁路
      - 只作用于镜头查询通道，不改使用方五出口口径
    """
    if object_type not in spec.scope_reads:
        raise PermissionError(
            f"镜头 {spec.skill_id} 作用域未授权读取 {object_type}"
            f"（fail-closed，请在 pack.json scope.reads 声明）")
    conn = getattr(store, "conn", store)
    table = _resolve_semantic_table(conn, object_type)
    sql = f'SELECT * FROM "{table}"'
    if where:
        sql = f"{sql} WHERE {where}"
    cur = conn.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if access is not None:
        from core.policy import PolicyEngine
        rows = PolicyEngine(policy_pack).apply_row_masks(access, object_type, rows)
    return rows


def _resolve_semantic_table(conn: Any, name: str) -> str:
    """类型名 → 物化语义表：对象 obj_X 优先，其次链接 lnk_X。"""
    for prefix in ("obj_", "lnk_"):
        table = f"{prefix}{name}"
        hit = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?",
            [table]).fetchone()[0]
        if hit:
            return table
    raise ValueError(
        f"语义类型 {name} 未物化（obj_{name}/lnk_{name} 均不存在）")


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


def _validate_params(spec: SkillSpec, params: dict) -> None:
    """下发参数与 params_schema 双向核对：未声明参数拒、必填缺失拒。"""
    for k in params:
        if k not in spec.params_schema:
            raise ValueError(
                f"镜头 {spec.skill_id} 未声明参数 {k!r}，禁止下发"
                f"（见 params_schema）")
    for pname, pspec in spec.params_schema.items():
        if pspec.get("required") and pname not in params:
            raise ValueError(
                f"镜头 {spec.skill_id} 缺少必填参数 {pname!r}")


_EVIDENCE_KINDS = ("node", "edge", "time_window", "aggregate", "file")


def _source_types_from_refs(clue: LineageClue) -> list[str]:
    """从 evidence_refs 提取去前缀的源对象/链接类型名（去重保序）。

    ref 形如 obj_transaction#<pk> / lnk_transfers#<key>；去掉 obj_/lnk_
    前缀即 ontology 类型名。aggregate 的 ref 同样按表名解析；file/纯 metric
    无源表，跳过。
    """
    out: list[str] = []
    seen: set[str] = set()
    for r in clue.evidence_refs or []:
        if not isinstance(r, dict):
            continue
        target = r.get("ref")
        if not isinstance(target, str) or "#" not in target:
            continue
        table = target.split("#", 1)[0]
        if table.startswith(("obj_", "lnk_")):
            tname = table[4:]
            if tname and tname not in seen:
                seen.add(tname)
                out.append(tname)
    return out


def _infer_jian_types(clue: LineageClue, ctx: dict) -> list[str]:
    """P6 融合层注入：evidence_refs 源类型 → packs/wujian 映射反查间类。

    五间包未安装（load_wujian 返回 None）→ 空列表（交叉页以缺口展示）。
    """
    try:
        from core.wujian import load_wujian
    except Exception:
        return []
    pack = "default"
    if isinstance(ctx, dict) and isinstance(ctx.get("ontology_pack"), str):
        pack = ctx["ontology_pack"]
    wj = load_wujian(pack)
    if wj is None:
        return []
    return wj.jians_for_types(_source_types_from_refs(clue))


def _validate_evidence_refs(clue: LineageClue, store: Any) -> None:
    """
    evidence_refs 挂 P1 key 契约（P3）：
      ① 结构合法：kind 白名单；node/edge/time_window 须带 "table#key" ref；
         file 须带 file_uri；aggregate 带 ref（定位到行）或 metric（聚合量）
      ② 引用的 obj_*/lnk_* 语义表存在
      ③ 引用行按表分组批量 IN 查询，悬空引用硬失败
    """
    refs = clue.evidence_refs
    if not refs:
        return
    grouped: dict[str, tuple[str, set[str]]] = {}

    def _group(target: Any, key_column: str | None = None) -> None:
        if not isinstance(target, str) or "#" not in target:
            raise ValueError(
                f"线索 {clue.clue_id} 证据缺少 'table#key' 形式 ref：{target!r}")
        table, _, key = target.partition("#")
        if not table or not key:
            raise ValueError(
                f"线索 {clue.clue_id} 证据 ref {target!r} 表名/键值不得为空")
        # obj_* 物化表带统一 pk 列；lnk_* 边表无 pk（CREATE AS build_sql），
        # 必须由引用方显式 key_column 指定行键列
        if not key_column:
            if table.startswith("obj_"):
                key_column = "pk"
            else:
                raise ValueError(
                    f"线索 {clue.clue_id} 证据 {target} 引用边表须显式声明 "
                    f"key_column（lnk_* 无统一 pk 列）")
        prev = grouped.get(table)
        if prev is not None and prev[0] != key_column:
            raise ValueError(
                f"线索 {clue.clue_id} 证据对表 {table} 的 key_column 不一致："
                f"{prev[0]} vs {key_column}")
        grouped.setdefault(table, (key_column, set()))[1].add(key)

    for r in refs:
        if not isinstance(r, dict):
            raise ValueError(
                f"线索 {clue.clue_id} evidence_refs 条目必须为 dict")
        kind = r.get("kind")
        if kind not in _EVIDENCE_KINDS:
            raise ValueError(
                f"线索 {clue.clue_id} evidence_refs kind={kind!r}，"
                f"允许 {_EVIDENCE_KINDS}")
        if kind == "file":
            if not r.get("file_uri"):
                raise ValueError(
                    f"线索 {clue.clue_id} file 类证据缺少 file_uri")
        elif kind == "aggregate":
            target = r.get("ref")
            if target:
                _group(target, r.get("key_column"))
            elif not r.get("metric"):
                raise ValueError(
                    f"线索 {clue.clue_id} aggregate 证据须带 ref 或 metric")
        else:
            _group(r.get("ref"), r.get("key_column"))

    if not grouped:
        return
    if store is None:
        raise ValueError(
            f"线索 {clue.clue_id} 携带 evidence_refs 但未提供 store，无法校验引用")
    conn = getattr(store, "conn", store)
    for table, (key_column, keys) in grouped.items():
        hit = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?",
            [table]).fetchone()[0]
        if not hit:
            raise ValueError(
                f"线索 {clue.clue_id} evidence_refs 引用了不存在的语义表 {table}")
        col_hit = conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name=? AND column_name=?",
            [table, key_column]).fetchone()[0]
        if not col_hit:
            raise ValueError(
                f"线索 {clue.clue_id} evidence_refs 表 {table} 无行键列 "
                f"{key_column}")
        placeholders = ", ".join(["?"] * len(keys))
        found = conn.execute(
            f'SELECT "{key_column}" FROM "{table}" '
            f'WHERE "{key_column}" IN ({placeholders})',
            list(keys)).fetchall()
        missing = keys - {row[0] for row in found}
        if missing:
            raise ValueError(
                f"线索 {clue.clue_id} evidence_refs 存在悬空引用 {table}#"
                f"{sorted(missing)[:3]}（共 {len(missing)} 条）")


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


def _clue_data_sources(clue: LineageClue) -> set[str]:
    """从线索**实际证据**反推数据源名（技能没声明 data_deps 时用）。

    奇正/用间的 data_deps 为空（它们走 Function 编排，不声明静态依赖），
    旧逻辑直接返回 []，兜底对这两个技能永久失效。改为从证据反推：
      - source_rows 的「语义表」字段（obj_call → call）
      - evidence_refs 的 ref（obj_call#xxx → call）
    再经五间包 source_names 译成展示名，与假设 data_sources 同口径。
    译不到就回落类型名——宁可匹配不上，也不硬编码中文。
    """
    types: set[str] = set()
    for r in (getattr(clue, "source_rows", None) or []):
        if not isinstance(r, dict):
            continue
        t = str(r.get("语义表") or r.get("对象类型") or "").strip()
        if t:
            types.add(t[4:] if t.startswith("obj_") else t)
    for e in (getattr(clue, "evidence_refs", None) or []):
        if not isinstance(e, dict):
            continue
        ref = str(e.get("ref") or "")
        if "#" in ref:
            head = ref.split("#", 1)[0]
            types.add(head[4:] if head.startswith("obj_") else head)
    if not types:
        return set()
    try:
        from core.wujian import load_wujian
        wj = load_wujian(getattr(spec_pack_of(clue), "pack", "default"))
    except Exception:
        wj = None
    names = (wj.source_names if wj is not None else {}) or {}
    return {names.get(t, t) for t in types}


def spec_pack_of(clue: LineageClue):
    """占位：包标识当前未随线索携带，返回 None 由调用方回落 default。"""
    return None


def _infer_assumptions(clue: LineageClue, spec: SkillSpec, miao: Any) -> list[str]:
    """若技能未显式填 assumption_chain，按数据依赖反推挂回**一条**假设。

    为什么必须收敛到一条
    --------------------
    旧实现把所有"数据源有交集"的假设全塞进 chain，实测 xu_shi 的
    data_deps=银行流水 会同时命中 H1(银行流水/招投标档案) 与 H4(银行流水/
    工商信息) → 返回 ['H1','H4']。一条线索挂两条假设等于有两个证伪
    目标，处置时无从下手——"挂回某条假设"这个兜底目标反而被破坏了。

    消歧：先按数据源交集数，再按**间类交集数**（线索自身的 jian_types
    是本条证据的真实归属，比静态 data_deps 更贴切）。两者都并列 →
    返回空，不猜（AI 不给定性，猜错的假设比没有假设更危险）。

    这是「假设覆盖完整性」的兜底 —— 确保每条线索都能挂回某条假设；
    挂不回去时留空，由读面标 needs_hypothesis 让正兵手动指定。
    """
    if miao is None or not hasattr(miao, "hypotheses"):
        return []
    deps = set(spec.data_deps)
    if not deps:
        deps = _clue_data_sources(clue)  # 技能没声明 → 从证据反推
    if not deps:
        return []

    cj = set(getattr(clue, "jian_types", None) or [])
    scored: list[tuple[int, int, str]] = []
    for h in miao.hypotheses:
        overlap = deps & set(getattr(h, "data_sources", None) or [])
        hj = set(getattr(h, "jian_types", None) or [])
        jov = len(hj & cj)
        # 间类不符即排除：假设**声明了**间类却与线索间类无交集 → 不挂。
        # 反例：死间线索（OSINT+工商内档）凭"工商信息"数据源交集挂上了
        # H2（因间），间类明显不符——等于凭一个共有数据源硬认亲，
        # 比留空更危险（留空至少会标 needs_hypothesis 让正兵手动指定）。
        # 假设未声明间类（hj 为空）时不歧视，仍按数据源匹配。
        if hj and not jov:
            continue
        # 须至少一维命中：数据源交集 OR 间类交集（旧实现只认前者，
        # 间类对得上但数据源口径不一致的假设会被整个漏掉）
        if not overlap and not jov:
            continue
        scored.append((jov, len(overlap), str(h.id)))
    if not scored:
        return []
    # 排序键：**间类交集优先于数据源交集**。
    # 反例：按数据源数排，H1 声明了 2 个源恒得 2 分，生/反/因间线索
    # 一律挂 H1（实测三种间类全返回 ['H1']）。间类是侦查学归属，比
    # "谁声明的源多"更能区分假设——先按间类，同间类再比数据源。
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    top = scored[0]
    if len(scored) > 1:
        second = scored[1]
        if (second[0], second[1]) == (top[0], top[1]):
            return []  # 并列且间类也分不出 → 不猜
    return [top[2]]


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
def _resolve_person_from_store(store, health=None) -> "EntityResolver":
    """
    从 DuckDB 中采集「人名类」实体记录，跑通人名对齐。
    采集范围：银行流水(主体/对方)、通话记录(主体/对端)、招投标档案(分管领导)、人员表(姓名)。
    若源表存在电话/身份证类列（如鲁棒性案例的人员表），一并采集作为强证据——
    红线 R-1：同名不同人（互斥强证据）须由对齐器拆簇待裁决，而非按名静默合并。
    返回已 ingest 但未 resolve 的 EntityResolver（调用方再 add_aliases + resolve）。
    """
    # 延迟导入：entity_resolution 与 core.registry 互相解耦
    # 复用 core.entity 的路径安全加载器（按绝对路径加载，避免同名包遮蔽 sys.path）
    from .entity import _load_person_resolver, classify_entity_type
    EntityResolver = _load_person_resolver(health=health)
    resolver = EntityResolver()
    conn = getattr(store, "conn", None)
    if conn is None:
        return resolver

    # 强证据列候选名（探测式：源表存在才采集，缺列降级只取名字）
    _PHONE_CANDS = ("电话", "手机", "手机号", "联系电话", "联系方式")
    _ID_CANDS = ("身份证号", "身份证", "证件号")
    name_sources = [
        ("银行流水", "主体"), ("银行流水", "对方"),
        ("通话记录", "主体"), ("通话记录", "对端"),
        ("招投标档案", "分管领导"), ("人员表", "姓名"),
    ]
    seen: set[tuple] = set()   # (name, phone, id_card) 去重；同名不同强证据 → 多条 → 对齐器拆簇
    records: list[dict] = []
    for table, col in name_sources:
        try:
            cols = [d[0] for d in conn.execute(
                f'SELECT * FROM "{table}" LIMIT 0').description]
        except Exception:
            continue   # 表不存在则跳过，容错
        if col not in cols:
            continue
        phone_col = next((c for c in _PHONE_CANDS if c in cols), None)
        id_col = next((c for c in _ID_CANDS if c in cols), None)
        sel = [f'DISTINCT "{col}" AS name']
        if phone_col:
            sel.append(f'"{phone_col}" AS phone')
        if id_col:
            sel.append(f'"{id_col}" AS id_card')
        try:
            rows = conn.execute(
                f'SELECT {", ".join(sel)} FROM "{table}" WHERE "{col}" IS NOT NULL'
            ).fetchall()
        except Exception:
            continue
        for row in rows:
            name = str(row[0]).strip()
            if not name:
                continue
            # 实体类型按名称形态判，不按"出现于哪张源表"定：
            # 银行流水(主体/对方)混有机构名与交易摘要，此前一律进人名对齐器，
            # 导致「A建材」被判 person、「现金存入」被当实体。org/non_entity
            # 一律排除；unknown 交由人审队列裁决，不强制归入。
            if classify_entity_type(name) != "person":
                continue
            phone = str(row[1]).strip() if phone_col and row[1] is not None else ""
            id_card = (str(row[2]).strip()
                       if id_col and len(row) > 2 and row[2] is not None else "")
            key = (name, phone, id_card)
            if key in seen:
                continue
            seen.add(key)
            rec = {"name": name, "phone": phone, "source_row_id": f"{table}.{col}"}
            if id_card:
                rec["id_card"] = id_card
            records.append(rec)

    resolver.ingest(records)
    return resolver
