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

import re
from typing import Any

from core.registry import (
    DEFAULT_REGISTRY, SkillSpec, LineageClue, SkillRegistry, get_registry,
)

# 方案 B 行集口径：用间交叉是表级覆盖度判定（语义代理表非空即命中，
# COUNT(*)），其推断的溯源粒度天然是表级而非行级。命中的每个数据源挂一行
# 「表级汇总行」：确定性 Function 产物，可复跑、可审计，不伪造行级证据。
AGGREGATE_GRANULARITY = "表级汇总"
#: 旧版 Function 依据字符串 "举报材料→obj_tipoff(13行)" 解析回落
_HIT_STR_RE = re.compile(r"^(.+?)→(\S+?)\((\d+)行\)$")


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


def _hypothesis_for_jian(jian_name: str, hit_object_types: list[str],
                         pack: str = "default",
                         base_dir=None) -> tuple[str, str]:
    """间类 → 假设：本体声明反查 + 命中对象类型消歧。

    为什么必须反查本体
    ------------------
    用间线索此前假设链全空 → 进处置清单却无命题可证伪，只能空转。
    补假设的**唯一可靠来源**是本体假设模式库的 jian_types 声明——
    假设回答"什么算可疑"，属领域知识，换本体自动跟随，不硬编码 H1..Hn。

    为什么还要消歧
    --------------
    间类到假设不是一对一的。实测本本体：
        生间 → H1（收受财物）**和** H3（密切私下关系）
    只按间类反查，生间命中该挂哪个？两者说的是完全不同的事——
    一个是钱，一个是关系。硬选一个等于替正兵定性。

    消歧用**命中的对象类型**与假设 evidence_object_types 的交集：
        生间命中 call/trackpoint/transaction
        H1 evidence=[transaction]          交集 1
        H3 evidence=[call, trackpoint]     交集 2  → H3 胜
    语义上也对：生间主力是通话 114 条 + 轨迹，正是"密切关系"的证据。

    返回 (hypothesis_id, 依据)。推不出返回 ("", 原因)——
    **不猜、不硬凑**：宁可留空让正兵指定，也不给一个来路不明的假设。
    """
    try:
        from core.ontology_loader import load_hypothesis_patterns
        raw = load_hypothesis_patterns(pack, base_dir=base_dir)
    except Exception:
        return "", "假设模式库装载失败"
    patterns = raw if isinstance(raw, list) else []
    if not patterns:
        return "", "本体未声明假设模式库"

    cands: dict[str, dict] = {}
    for it in patterns:
        h = it.get("hypothesis") if isinstance(it, dict) else None
        if not isinstance(h, dict):
            continue
        hid = str(h.get("id") or "").strip()
        if not hid:
            continue
        if jian_name in (h.get("jian_types") or []):
            cands.setdefault(hid, h)

    if not cands:
        return "", f"本体未为间类「{jian_name}」声明假设"
    if len(cands) == 1:
        hid = next(iter(cands))
        return hid, f"间类「{jian_name}」唯一映射到 {hid}"

    # 多候选：用命中对象类型与 evidence_object_types 的交集消歧
    hits = {str(t) for t in hit_object_types if t}
    scored: list[tuple[int, str]] = []
    for hid, h in cands.items():
        ev = {str(x) for x in (h.get("evidence_object_types") or [])}
        if not ev:  # 证据对象类型未声明 → 回落 object_types
            ev = {str(x) for x in (h.get("object_types") or [])}
        scored.append((len(hits & ev), hid))
    scored.sort(key=lambda x: (-x[0], x[1]))

    if scored[0][0] == 0:
        return "", (f"间类「{jian_name}」有 {len(cands)} 个候选假设，"
                    f"但均不覆盖命中的对象类型（{'、'.join(sorted(hits))}）")
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return "", (f"间类「{jian_name}」候选假设覆盖度相同"
                    f"（{scored[0][1]} 与 {scored[1][1]} 均 {scored[0][0]} 项），"
                    f"无法消歧")
    return scored[0][1], (
        f"间类「{jian_name}」多候选，按命中对象类型消歧 → {scored[0][1]}"
        f"（交集 {scored[0][0]} 项）")


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
    Q1（time_window_collision）输出英文投影列：owner_raw/title/amount/
    offset_days/pub_date，此处做业务化呈现（不再依赖旧版中文列名）。
    """
    qz = result.get("奇正分工", {})
    q1_rows = qz.get("Q1_result") or []
    # C：提取首个资金主体做标题前缀
    subject = ""
    if q1_rows and isinstance(q1_rows[0], dict):
        subject = str(q1_rows[0].get("owner_raw")
                      or q1_rows[0].get("资金主体") or "")
    title = "奇正分工方案" + (f" · {subject}" if subject else "")
    # A：basis 副标题补 Q1 碰撞摘要（项目 · 公示日 · 偏移 · 金额）
    basis = ""
    if q1_rows and isinstance(q1_rows[0], dict):
        r0 = q1_rows[0]
        proj = r0.get("title") or r0.get("项目") or ""
        parts = [f"时间窗碰撞：{proj}"] if proj else []
        pub = r0.get("pub_date")
        off = r0.get("offset_days")
        if pub is not None and off is not None:
            try:
                n = int(off)
                when = "公示当天" if n == 0 else (
                    f"公示日后 {n} 天" if n > 0 else f"公示日前 {abs(n)} 天")
                parts.append(f"{pub} {when}")
            except (TypeError, ValueError):
                pass
        amt = r0.get("amount") or r0.get("金额")
        if amt:
            try:
                num = float(amt)
                if num and num % 10000 == 0:
                    amt = f"{num / 10000:g} 万元"
            except (TypeError, ValueError):
                pass
            parts.append(f"金额 {amt}")
        basis = " · ".join(parts)
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


def _aggregate_rows_from_cross_row(row: dict) -> list[dict[str, Any]]:
    """用间交叉 Function 行 → 表级汇总行集（方案 B 行集口径）。

    优先消费结构化 命中明细 [{source, table, obj_name, n}]；
    旧版 Function 无此字段时回落解析 依据 字符串（"源→表(n行)"）。
    """
    out: list[dict[str, Any]] = []
    details = row.get("命中明细")
    if isinstance(details, list):
        for d in details:
            if not isinstance(d, dict) or not d.get("table"):
                continue
            out.append({
                "数据源": str(d.get("source") or ""),
                "语义表": str(d.get("table") or ""),
                "对象类型": str(d.get("obj_name") or ""),
                "行数": int(d.get("n") or 0),
                "粒度": AGGREGATE_GRANULARITY,
            })
    if out:
        return out
    for hit in row.get("依据") or []:
        m = _HIT_STR_RE.match(str(hit))
        if not m:
            continue
        out.append({
            "数据源": m.group(1),
            "语义表": m.group(2),
            "对象类型": "",
            "行数": int(m.group(3)),
            "粒度": AGGREGATE_GRANULARITY,
        })
    return out


def _clue_from_yong_jian(spec: SkillSpec, result: dict) -> list[LineageClue]:
    """用间交叉：每行命中 → 一条线索，jian_types 取该行命中的间类。

    方案 A：标题补数据源摘要，basis 展开命中的数据源列表。
    方案 B：行集口径=表级汇总（Function COUNT 命中的语义表，一源一行），
    推断卡挂确定性表级溯源；detail.行集口径 明示粒度。
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
        # B：表级汇总行集（旧版 Function 输出回落字符串解析）
        agg_rows = _aggregate_rows_from_cross_row(row)
        # 本间判定优先（全局等级对单间无区分力：数据接全了恒为最高级）；
        # 缺本间字段（旧 Function 输出）时回落全局等级，再不行按命中算。
        n_src = row.get("本间独立源数")
        if not isinstance(n_src, int):
            n_src = len(row.get("命中明细") or [])
        j_level = row.get("本间交叉等级") or result["用间交叉"].get("交叉等级")
        detail: dict[str, Any] = {
            "数据源": sources, "等级": j_level,
            "本间独立源数": n_src,
            "本间独立数据源": list(row.get("本间独立数据源") or []),
            "够格为线索": bool(n_src >= 2),
            "依据": basis, "维度": _dims_for_jian([jian_name])}
        if agg_rows:
            detail["行集口径"] = AGGREGATE_GRANULARITY
        # 表级 COUNT 转 aggregate 证据引用：五间交叉是覆盖度判定，没行级
        # 定位，故只挂 metric（不带 ref——_validate_evidence_refs 第 664-670
        # 行 aggregate 无 ref 时只要求 metric 字段，通过校验）。前端 refText
        # 扩展读 source/table 让显示从"聚合量 count：5"变成可读的
        # "举报材料（obj_tipoff）行数=5"。
        ev_refs: list[dict[str, Any]] = []
        for r in agg_rows:
            tbl = str(r.get("语义表") or "")
            ev_refs.append({
                "kind": "aggregate",
                "metric": "row_count",
                "value": int(r.get("行数") or 0),
                "source": str(r.get("数据源") or ""),
                "table": tbl,
                "obj_type": str(r.get("对象类型") or ""),
                "granularity": str(r.get("粒度") or ""),
            })
        # 假设链：本体间类声明反查（多候选时按命中对象类型消歧）。
        # 推不出就留空并记原因——不猜、不硬凑，读面可据此提示正兵指定。
        hit_types = [str(d.get("obj_name") or "")
                     for d in (row.get("命中明细") or [])
                     if isinstance(d, dict)]
        hid, why = _hypothesis_for_jian(jian_name, hit_types)
        detail["假设依据"] = why
        if not hid:
            detail["假设缺失原因"] = why
        clues.append(LineageClue(
            skill_id=spec.skill_id,
            title=title,
            detail=detail,
            source_rows=agg_rows,
            evidence_refs=ev_refs,
            jian_types=[jian_name],
            assumption_chain=[hid] if hid else [],
        ))
    return clues


def split_yong_jian(clues: list) -> tuple[list, list]:
    """用间产出分流：够格为线索的留线索，单源（观察）转观察档案。

    兑现五间方法论自己声明的规则
    ----------------------------
        "单源=观察 → 双源=线索 → 三源=可立案依据候选"

    此前 adapter 只要"命中"就产线索，**完全不看交叉等级**，于是单源的
    内间也变成"查证中"的线索——规则承诺从未兑现。

    判据用**本间独立源数**（不是全局）：全局是全案汇总，数据接全了恒为
    3 级，对单间没有区分力。

    返回 (clues, observations)：observations 为 Observation 列表。
    """
    from core.observation import observation_from_clue

    keep: list = []
    observations: list = []
    for c in clues:
        det = c.detail if hasattr(c, "detail") else (c.get("detail") or {})
        if det.get("够格为线索", True):
            keep.append(c)
            continue
        # 观察没有主体靶心（五间是数据源盘点，不是针对某个人的发现）→
        # 转换前先把间类写进 detail.主体：observation id 由「技能+靶心」
        # 派生，靶心为空会让多条单源观察挤成同一个 id。
        jn = (c.jian_types if hasattr(c, "jian_types") else
              c.get("jian_types")) or []
        if jn and not det.get("主体"):
            det["主体"] = jn[0]
        observations.append(observation_from_clue(c))
    return keep, observations


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
# 自动注册（内置包引导；P3 起外部镜头包走 core.pack_loader 自枚举）
# ----------------------------------------------------------------------

def register_all(registry: SkillRegistry | None = None) -> SkillRegistry:
    """把五个内置子技能注册到 registry。幂等，可重复调用。

    P3：内置规格新契约字段（mode/params_schema/scope_reads 等）全走默认值，
    注册时经 SkillSpec.validate() 校验通过，与 packs/* 外部包同一注册路径。
    """
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
        # data_deps 须与假设的 data_sources 同口径（五间 source_names 展示名）：
        # 旧值"中标档案"与假设的"招投标档案"对不上，因间线索兜底恒落空。
        SkillSpec(skill_id="xu_shi", name="虚实扫描", stage="虚实",
                   consumes_jian=["生间", "反间"], data_deps=["银行流水", "招投标档案"],
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
