"""观察档案（Observation Dossier）—— 镜头产出的**正确容器**。

为什么不是 LineageClue
----------------------
镜头产出此前被一律包成 LineageClue，与规则线索同进处置清单、走同一套
状态流转。实测十条镜头线索全部「查证中」而假设链全部为空——**没有要证伪
的命题，查证就没有终点**。正兵拿到"张卫国的跨类型事件序列：148 起事件"
无法处置它，因为它不是一个主张，只是一组事实。

分界线不在确定性（规则同样 deterministic），而在**有没有常态基线**：
  - 规则：通话频次达常态中位数 2 倍 → 回答"算不算异常"（判定）
  - 镜头：事件数 ≥2 / 窗口内类型数 ≥2 → 回答"结构存不存在"（观测）

镜头产出都写着 `evidence_level: "观察"`，但该字段此前只有写入方、无消费方。
本模块把它接上：镜头产出 = 观察档案，**不是线索**。

观察档案 = 已摆出的事实（无命题、无处置状态）
线索     = 待证明的命题（有假设链、有状态流转）

正兵认为某条观察构成疑点 → **显式提升为线索**，且必须指定验证哪个假设。
那一刻它才成为命题。定性权属正兵，镜头不替他决定。

稳定 id（关键设计）
------------------
observation_id 由 **skill_id + 观察对象 + 参数** 派生，**不含版本号**。
这样 RESCAN 后同一观察 id 不变，正兵的认领/标注能跨版本挂住——
符合"已产生的东西不因配置变更而消失"。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stable_id(skill_id: str, subject_key: str, params_key: str) -> str:
    """观察稳定 id：只由「谁产出 + 观察谁 + 怎么产出」决定，**不含版本**。

    同一案件、同一镜头、同一靶心、同一参数 → 每次重扫得到同一个 id，
    正兵的认领/归档/标注得以跨版本挂住。
    """
    raw = f"{skill_id}|{subject_key}|{params_key}"
    return "obs_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _params_key(params: dict | None) -> str:
    """参数指纹：只取影响结果的键（排除靶心之外的技术参数）。"""
    if not params:
        return ""
    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class Observation:
    """一条观察档案。

    与 LineageClue 的关键差异：
      - **无 status**：不进处置状态流转
      - **无 assumption_chain**：观察本身不是命题，不预设验证什么
      - **有 basis/falsification**：陈述观测到的结构 + 什么情况下不成立
      - **有 facts**：人能读的事件明细（判据的支撑行）
    """
    observation_id: str = ""
    skill_id: str = ""
    lens_name: str = ""          # 镜头中文名（本体声明，换本体自动跟随）
    title: str = ""
    # 观察对象（供检索/筛选：按主体、按项目）
    subject: str = ""
    project: str = ""
    # 判据三件套
    basis: str = ""              # 陈述观测（不作定性）
    falsification: str = ""      # 什么情况下这不成立
    claims: list[str] = field(default_factory=list)   # 可核对的断言
    # 事实明细：人能读的事件摘要（判据的支撑行）
    facts: list[dict] = field(default_factory=list)
    # 语义层证据引用（可定位回真实行）
    evidence_refs: list[dict] = field(default_factory=list)
    # 完整函数输出（供画布成图/下钻；读侧按需取用）
    detail: dict[str, Any] = field(default_factory=dict)
    # 溯源与受限说明
    param_source: str = ""       # 靶心从哪来（可审计、可复算）
    degraded: bool = False
    degraded_reason: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Observation":
        known = set(Observation.__dataclass_fields__)
        return Observation(**{k: v for k, v in d.items() if k in known})


# ----------------------------------------------------------------------
# 从 LineageClue 转观察档案
# ----------------------------------------------------------------------
# handler 仍返回 LineageClue：复用 skill_invoke 的 evidence_refs 硬校验
# （引用必须指向真实存在的语义行）与 L1 特征层写入。转换发生在**编排层**，
# 不动 handler 契约——改动面最小、且不丢失既有校验。

def observation_from_clue(clue: Any) -> Observation:
    """镜头产出的 LineageClue → Observation。

    抽取「观察」该有的部分；**不继承** status / assumption_chain /
    jian_types / 定性_policy——那些是命题的属性，观察没有。
    """
    d = clue.to_dict() if hasattr(clue, "to_dict") else dict(clue)
    detail = dict(d.get("detail") or {})
    skill_id = str(d.get("skill_id") or "")

    # 观察对象：不同镜头的靶心字段不同，统一归到 subject/project
    subject = ""
    project = ""
    sub = detail.get("subject")
    if isinstance(sub, dict):
        subject = str(sub.get("name") or sub.get("pk") or "")
    if not subject:
        subject = str(detail.get("主体") or "")
    proj = detail.get("project")
    if isinstance(proj, dict):
        project = str(proj.get("name") or proj.get("pk") or "")
    sa, sb = detail.get("subject_a"), detail.get("subject_b")
    if not subject and isinstance(sa, dict) and isinstance(sb, dict):
        subject = f"{sa.get('name', '')}×{sb.get('name', '')}"

    # 稳定 id 的靶心部分
    subject_key = subject or project or ""
    params_key = _params_key(detail.get("param_source") or "")
    # param_source 含靶心来源（如 focus:case_aliases#1:张卫国），
    # 用它做参数指纹能区分不同靶心；靶心变化 → 观察不同 → id 不同。
    params_key = str(detail.get("param_source") or "") or params_key

    return Observation(
        observation_id=_stable_id(skill_id, subject_key, params_key),
        skill_id=skill_id,
        lens_name=str(d.get("skill_id") or ""),  # 中文名由读面按本体翻译
        title=str(d.get("title") or ""),
        subject=subject,
        project=project,
        # 兼容中文键「依据/证伪/断言」：handler 都按中文键写，英文键取不到
        basis=str(detail.get("basis") or detail.get("依据") or ""),
        falsification=str(detail.get("falsification") or detail.get("证伪") or ""),
        claims=list(detail.get("claims") or detail.get("断言") or []),
        facts=_facts_from_detail(detail),
        evidence_refs=list(d.get("evidence_refs") or []),
        detail=detail,
        param_source=str(detail.get("param_source") or ""),
        degraded=bool(detail.get("degraded")),
        degraded_reason=str(detail.get("degraded_reason") or ""),
    )


def _facts_from_detail(detail: dict, limit: int = 50) -> list[dict]:
    """从镜头 detail 抽人能读的事件明细（判据的支撑行）。

    与 evidence_builder 的事实栏同源：有 date/role/brief 的事件才是
    人能读的事实，干巴巴的 `obj_call#pk` 引用留作 evidence_refs。
    """
    events: list[dict] = []
    for e in detail.get("timeline") or []:
        if isinstance(e, dict):
            events.append(e)
    for e in detail.get("events") or []:
        if isinstance(e, dict):
            events.append(e)
    for b in detail.get("bursts") or []:
        if isinstance(b, dict):
            for e in b.get("events") or []:
                if isinstance(e, dict):
                    events.append(e)

    seen: set[str] = set()
    out: list[dict] = []
    for e in events:
        key = str(e.get("event_pk") or "") or json.dumps(
            e, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": e.get("date"),
            "offset_days": e.get("offset_days"),
            "type": e.get("type"),
            "src_object": e.get("src_object"),
            "role": e.get("role"),
            "brief": e.get("brief"),
            "event_pk": e.get("event_pk"),
        })
        if len(out) >= limit:
            break
    return out
