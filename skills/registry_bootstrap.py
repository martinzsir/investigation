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

# 间类 → 侦查维度映射（雷达五轴数据来源）
_JIAN_TO_DIM: dict[str, str] = {
    "生间": "资金",   # 银行流水异常
    "反间": "资金",   # 过桥资金
    "因间": "关系",   # 利益关联
    "死间": "行为",   # 行为轨迹/OSINT
    "内间": "通讯",   # 举报线索/通讯
}


def _dims_for_jian(jian_types: list[str]) -> list[str]:
    """间类 → 去重维度列表（雷达五轴聚合用）。"""
    seen: list[str] = []
    for j in jian_types:
        d = _JIAN_TO_DIM.get(j)
        if d and d not in seen:
            seen.append(d)
    return seen


def _chain_jian_dim_for(text: str) -> tuple[list[str], list[str], list[str]]:
    """按庙算模式库把 finding 文本映射为 (假设链, 间类, 维度)。"""
    from core.hypotheses import MiaoSuan  # 延迟导入：core 不依赖 skills，无循环
    for p in MiaoSuan.FINDING_PATTERNS:
        if any(k in text for k in p["keywords"]):
            tpl = p["hypothesis"]
            return [tpl.id], list(tpl.jian_types), list(tpl.dimension)
    return [], [], []


def _clue_from_xu_shi(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """虚实扫描：每个 finding → 一条 LineageClue，按模式库挂假设链与间类。"""
    clues: list[LineageClue] = []
    findings = result.get("虚实扫描", {}).get("findings", [])
    for i, f in enumerate(findings):
        text = f"{f.get('候选虚处', '')}{f.get('依据', '')}"
        # 假设链/间类/维度：规则手册声明（rules.json）优先；未声明时回落模式库映射/关键词兜底
        chain, jian, dims = _chain_jian_dim_for(text)
        if f.get("assumption"):
            chain = [f["assumption"]]
        if f.get("jian_types"):
            jian = list(f["jian_types"])
        if not jian:
            jian = ["反间"] if "过桥" in text else ["生间"]
        # 维度：模式库优先；无映射时按间类推断
        if not dims:
            dims = _dims_for_jian(jian)
        detail = {"依据": f.get("依据"), "级别": f.get("级别"), "维度": dims}
        if f.get("rule_id"):  # 规则溯源：线索携带规则 id 与判据原文（可回放）
            detail["rule_id"] = f["rule_id"]
            detail["rule_text"] = f.get("rule_text", "")
        clues.append(LineageClue(
            skill_id=spec.skill_id,
            title=f.get("候选虚处", ""),
            detail=detail,
            source_rows=f.get("source_rows", []),
            assumption_chain=chain,
            jian_types=jian,
        ))
    return clues


def _clue_from_qi_zheng(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """奇正分工：奇兵/正兵任务清单 → 一条线索（待固证）。

    方案 A+C：从 Q1_result 提取资金主体拼入标题，basis 补项目名摘要。
    """
    qz = result.get("奇正分工", {})
    q1_rows = qz.get("Q1_result") or []
    # C：提取首个资金主体做标题前缀
    subject = ""
    if q1_rows and isinstance(q1_rows[0], dict):
        subject = str(q1_rows[0].get("资金主体") or "")
    title = "奇正分工方案" + (f" · {subject}" if subject else "")
    # A：basis 副标题补 Q1 碰撞摘要
    basis = ""
    if q1_rows and isinstance(q1_rows[0], dict):
        r0 = q1_rows[0]
        proj = r0.get("项目") or ""
        amt = r0.get("金额") or ""
        if proj and amt:
            basis = f"时间窗碰撞：{proj} · 金额 {amt}"
        elif proj:
            basis = f"时间窗碰撞：{proj}"
    jian_types = list(spec.consumes_jian)
    detail = {
        "奇兵": qz.get("奇兵(AI)") or qz.get("奇兵") or [],
        "正兵": qz.get("正兵(人)") or qz.get("正兵") or [],
        "依据": basis,
        "维度": _dims_for_jian(jian_types),
    }
    return [LineageClue(
        skill_id=spec.skill_id,
        title=title,
        detail=detail,
        source_rows=[dict(r) for r in q1_rows],
        jian_types=list(spec.consumes_jian),
        needs_human_review=True,
    )]


def _clue_from_yong_jian(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """用间交叉：每行命中 → 一条线索，jian_types 取该行命中的间类。

    方案 A：标题补数据源摘要，basis 展开命中的数据源列表。
    """
    clues: list[LineageClue] = []
    rows = result.get("用间交叉", {}).get("rows", [])
    for row in rows:
        if not row.get("命中"):
            continue
        jian_name = row["间"]
        sources = row.get("数据源") or []
        # A：标题补首个数据源简称
        src_brief = ""
        if sources:
            first_src = str(sources[0]).split("→")[0] if sources else ""
            src_brief = f"（{first_src}）" if first_src else ""
        title = f"{jian_name}命中{src_brief}"
        # A：basis 副标题展开全部命中数据源
        basis = "；".join(str(s) for s in sources[:3]) if sources else ""
        clues.append(LineageClue(
            skill_id=spec.skill_id,
            title=title,
            detail={"数据源": sources, "等级": result["用间交叉"].get("交叉等级"),
                    "依据": basis, "维度": _dims_for_jian([jian_name])},
            jian_types=[jian_name],
        ))
    return clues


def _clue_default(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """默认：把输出 dict 的每个顶层键当作一条线索。

    方案 A+C：尝试从 result 中提取被盘点对象名拼入标题，basis 补摘要。
    """
    # C：尝试从双向盘点结构提取对象名
    title = spec.name
    basis = ""
    for key, val in result.items():
        if isinstance(val, dict):
            # zhi_ji_zhi_bi: "彼（张卫国）" → 提取人名
            for sub_key in val:
                if "彼" in sub_key and "（" in sub_key:
                    name = sub_key[sub_key.index("（") + 1:sub_key.rindex("）") if "）" in sub_key else len(sub_key)]
                    title = f"双向盘点：{name}"
                    # A：basis 从子项摘要
                    parts = [f"{k}：{v}" for k, v in list(val[sub_key].items())[:3]
                             if isinstance(v, str)]
                    basis = "；".join(parts)
                    break
    jian_types = list(spec.consumes_jian)
    return [LineageClue(
        skill_id=spec.skill_id,
        title=title,
        detail={**result, "依据": basis, "维度": _dims_for_jian(jian_types)},
        jian_types=jian_types,
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

    def handler(miao=None, store=None, ctx=None, params=None, health=None) -> list[LineageClue]:
        ctx = ctx or {}
        # 各技能 run() 签名不完全一致，用 kwargs 兼容
        sig = {"ctx": ctx}
        names = _run_param_names(run_fn)
        if "store" in names:
            sig["store"] = store
        if "miao" in names:
            sig["miao"] = miao
        # REQ-G-010：仅向声明了 health 形参的技能透传运行诊断（其余技能零影响）
        if "health" in names:
            sig["health"] = health
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
