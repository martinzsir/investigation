"""
skills/registry_bootstrap.py
注册式适配层：把五子技能的现有 run() 包装成符合 skill_invoke 契约的 handler。

设计：
  - 每个子技能保留原有 run(ctx, store, ...) 逻辑（不动）
  - 本模块提供一个 adapter(spec_id)，把 run() 返回的 dict 转换成 [LineageClue]
  - 模块导入时自动把五个技能注册到 DEFAULT_REGISTRY

正兵操作台只需：
    from core.registry import skill_invoke, get_registry
    from skills import registry_bootstrap  # 触发注册（副作用）
    clues = skill_invoke(get_registry(), "xu_shi", miao=miao, store=store, ctx=ctx)
"""

from __future__ import annotations

from typing import Any

from core.registry import (
    DEFAULT_REGISTRY, SkillSpec, LineageClue, SkillRegistry, get_registry,
)


# ----------------------------------------------------------------------
# 适配函数：把各技能 run() 的 dict 输出 → [LineageClue]
# ----------------------------------------------------------------------

def _chain_jian_for(text: str) -> tuple[list[str], list[str]]:
    """按庙算模式库（单一事实来源）把 finding 文本映射为 (假设链, 间类)。"""
    from core.hypotheses import MiaoSuan  # 延迟导入：core 不依赖 skills，无循环
    for p in MiaoSuan.FINDING_PATTERNS:
        if any(k in text for k in p["keywords"]):
            tpl = p["hypothesis"]
            return [tpl.id], list(tpl.jian_types)
    return [], []


def _clue_from_xu_shi(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """虚实扫描：每个 finding → 一条 LineageClue，按模式库挂假设链与间类。"""
    clues: list[LineageClue] = []
    findings = result.get("虚实扫描", {}).get("findings", [])
    for i, f in enumerate(findings):
        text = f"{f.get('候选虚处', '')}{f.get('依据', '')}"
        # 假设链/间类优先取模式库映射；未命中模式时兜底按关键词推断
        chain, jian = _chain_jian_for(text)
        if not jian:
            jian = ["反间"] if "过桥" in text else ["生间"]
        clues.append(LineageClue(
            skill_id=spec.skill_id,
            title=f.get("候选虚处", ""),
            detail={"依据": f.get("依据"), "级别": f.get("级别")},
            source_rows=f.get("source_rows", []),
            assumption_chain=chain,
            jian_types=jian,
        ))
    return clues


def _clue_from_qi_zheng(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """奇正分工：奇兵/正兵任务清单 → 一条线索（待固证）。"""
    qz = result.get("奇正分工", {})
    return [LineageClue(
        skill_id=spec.skill_id,
        title="奇正分工方案",
        detail={"奇兵": qz.get("奇兵", []), "正兵": qz.get("正兵", [])},
        jian_types=list(spec.consumes_jian),
        needs_human_review=True,
    )]


def _clue_from_yong_jian(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """用间交叉：每行命中 → 一条线索，jian_types 取该行命中的间类。"""
    clues: list[LineageClue] = []
    rows = result.get("用间交叉", {}).get("rows", [])
    for row in rows:
        if row.get("命中"):
            clues.append(LineageClue(
                skill_id=spec.skill_id,
                title=f"{row['间']}命中",
                detail={"数据源": row.get("数据源", []), "等级": result["用间交叉"].get("交叉等级")},
                jian_types=[row["间"]],
            ))
    return clues


def _clue_default(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """默认：把输出 dict 的每个顶层键当作一条线索。"""
    return [LineageClue(
        skill_id=spec.skill_id,
        title=spec.name,
        detail=result,
        jian_types=list(spec.consumes_jian),
    )]


# skill_id -> 专用转换器
_ADAPTERS = {
    "xu_shi": _clue_from_xu_shi,
    "qi_zheng": _clue_from_qi_zheng,
    "yong_jian": _clue_from_yong_jian,
}


def make_handler(skill_id: str, run_fn):
    """
    生成符合 skill_invoke 契约的 handler。

    契约：handler(miao, store, ctx, params) -> list[LineageClue]
    实现：调用原 run()，再用对应 adapter 把 dict → [LineageClue]，
          自动补齐 assumption_chain（若未填则按庙算假设反推）。
    """
    adapter = _ADAPTERS.get(skill_id, _clue_default)

    def handler(miao=None, store=None, ctx=None, params=None) -> list[LineageClue]:
        ctx = ctx or {}
        # 各技能 run() 签名不完全一致，用 kwargs 兼容
        sig = {"ctx": ctx}
        if "store" in _run_param_names(run_fn):
            sig["store"] = store
        if "miao" in _run_param_names(run_fn):
            sig["miao"] = miao
        result = run_fn(**sig)
        return adapter(DEFAULT_REGISTRY.skill(skill_id), result or {})

    return handler


def _run_param_names(fn) -> set[str]:
    import inspect
    return set(inspect.signature(fn).parameters.keys())


# ----------------------------------------------------------------------
# 自动注册（导入本模块即生效）
# ----------------------------------------------------------------------

def register_all(registry: SkillRegistry | None = None) -> SkillRegistry:
    """把五个子技能注册到 registry。幂等，可重复调用。"""
    reg = registry or DEFAULT_REGISTRY

    # 延迟导入：避免循环引用（core.registry <-> skills）
    from skills.miaosuan import run as run_miao
    from skills.zhi_ji_zhi_bi import run as run_zj
    from skills.xu_shi import run as run_xs
    from skills.qi_zheng import run as run_qz
    from skills.yong_jian import run as run_yj

    specs = [
        SkillSpec(skill_id="miaosuan", name="庙算沙盘", stage="庙算",
                   consumes_jian=[], data_deps=[],
                   handler=make_handler("miaosuan", run_miao)),
        SkillSpec(skill_id="zhi_ji_zhi_bi", name="双向画像机", stage="知己",
                   consumes_jian=[], data_deps=[],
                   handler=make_handler("zhi_ji_zhi_bi", run_zj)),
        SkillSpec(skill_id="xu_shi", name="虚实扫描", stage="虚实",
                   consumes_jian=["生间", "反间"], data_deps=["银行流水", "中标档案"],
                   handler=make_handler("xu_shi", run_xs)),
        SkillSpec(skill_id="qi_zheng", name="奇正分工器", stage="奇正",
                   consumes_jian=["生间", "反间", "因间"], data_deps=[],
                   handler=make_handler("qi_zheng", run_qz)),
        SkillSpec(skill_id="yong_jian", name="五间交叉器", stage="用间",
                   consumes_jian=["因间", "生间", "反间", "死间", "内间"], data_deps=[],
                   handler=make_handler("yong_jian", run_yj)),
    ]

    for s in specs:
        if s.skill_id not in reg:
            reg.register(s)
    return reg


# 导入即注册（正兵操作台 import 此模块后可直接 skill_invoke）
register_all(DEFAULT_REGISTRY)
