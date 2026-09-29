"""
packs/fund/impl.py
资金研判镜头包实现（经 pack_loader 的 "impl:func" 路径加载）。

为什么要有这个包
----------------
本包 4 个 Function（integer_transfer_aggregates / quarter_end_integer_deposits /
overpass_two_hop / time_window_collision）此前是**无 Lens 包装的裸 Function**：
只能经画布 RC-204 白名单直连调用，进不了镜头目录——于是无法批量调度、
无法产出 basis 判据、也无法像其它镜头那样挂假设与证据引用。

包装后它们与 geo/timeline/relation 各镜头同构：统一调度、统一线索化、
统一挂假设、统一出 basis。计算仍全部委托给 ontology Function，本文件
不写一行业务 SQL、不直接读 Parquet。

三条红线（与其它镜头包一致）
----------------------------
1. **证据引用不悬空**：函数返回 raw_name 时，先补查 obj_person / obj_org
   拿真实主键再挂 node 引用；查不到就只挂 aggregate 引用，绝不凭名字
   造一个 ref（悬空引用在 skill_invoke 后处理会硬失败，那是体系性的保护，
   这里不去绕过它）。
2. **零命中不出线索**：返回 []，并把降级原因落 ctx（数据源未接入与
   "确实没有"必须能区分，不能都显示为空）。
3. **不编造未计算的量**：basis 只陈述函数实际返回的字段。

假设归属：一律经 core.lens_assumption.resolve_function_assumption 从本体
反查（functions.json 的规则挂钩 + hypothesis_patterns.json 的 rule→hypothesis），
本文件不硬编码任何 H 编号——硬编码就是重新引入"抄错"的可能。
"""
from __future__ import annotations

from typing import Any

from core.functions import FunctionExecutor
from core.lens_assumption import resolve_function_assumption
from core.lens_basis import basis_for
from core.registry import LineageClue

_SOURCE_TYPE = "资金研判"

# raw_name → (对象类型, 主键列)
_SUBJECT_TABLES = (("person", "person_id"), ("org", "org_id"))


def _rows_of(out: dict) -> list:
    """统一取数：**sql 类 Function 结果在 out["rows"]，py 类在 out["result"]。**

    不统一的后果是静默零线索——早前本文件对 py 类的 overpass_two_hop 按
    out["rows"] 取值，函数实际算出了 1 条过桥路径，镜头却报"零线索"，
    正兵会读成"确实没有过桥"。这不报错、有输出，是最难发现的失效形态。
    """
    rows = out.get("rows")
    if rows:
        return rows
    r = out.get("result")
    if isinstance(r, dict):
        return r.get("rows") or []
    if isinstance(r, list):
        return r
    return []


def _invoke(store, fn_name: str, fn_params: dict, health) -> dict:
    return FunctionExecutor(store, health=health).invoke(fn_name, fn_params)


def _clean(params: dict, keys: list[str]) -> dict:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _note_degraded(ctx, skill_id: str, out: dict, r: dict | None = None) -> None:
    """零命中/降级原因经 ctx["lens_notes"] 通道留痕（不造线索，只留痕）。

    为什么零命中也要留痕：任务"100% 完成、零观察、无原因"是最难排查的
    无声失败——正兵无法区分「数据源未接入」与「确实没有此类结构」。
    这里两者都落，degraded 标志如实区分。

    ctx 是 **dict**（skill_invoke 契约），不是对象：早前写成 getattr(ctx,...)
    永远取不到值，降级信息静默丢失——正是"代码在跑、看不出问题"的失效形态。
    """
    if not isinstance(ctx, dict):
        return
    r = r or {}
    ctx.setdefault("lens_notes", []).append({
        "skill_id": skill_id,
        "function": out.get("function"),
        "degraded": bool(out.get("degraded")),
        "degraded_reason": (r.get("degraded_reason")
                            or out.get("degraded_reason")),
        "zero_hit": True,
    })


def _subject_ref(store, raw_name: Any) -> dict | None:
    """raw_name → node 引用（person / org）。查不到返回 None，不猜不造。

    为什么必须补查：Function 输出的是 raw_name（业务名），而证据引用要挂在
    真实主键上。用 raw_name 直接拼 ref 会造出悬空引用，后处理校验会硬失败。
    """
    name = str(raw_name or "").strip()
    if not name or store is None:
        return None
    for t, col in _SUBJECT_TABLES:
        try:
            rows = store.query(
                f"SELECT {col} FROM obj_{t} WHERE raw_name = ? LIMIT 1", (name,))
        except Exception:
            continue  # 表不存在/未接入 → 换下一个类型，不报错
        if rows:
            return {"kind": "node", "ref": f"obj_{t}#{rows[0][col]}",
                    "key_column": col}
    return None


def _project_ref(store, title: Any) -> dict | None:
    """title → bid_project node 引用。查不到返回 None。"""
    t = str(title or "").strip()
    if not t or store is None:
        return None
    try:
        rows = store.query(
            "SELECT project_id FROM obj_bid_project WHERE title = ? LIMIT 1", (t,))
    except Exception:
        return None
    if rows:
        return {"kind": "node",
                "ref": f"obj_bid_project#{rows[0]['project_id']}",
                "key_column": "project_id"}
    return None


def _assumption(fn_name: str) -> str | None:
    """从本体反查假设（不硬编码 H 编号）。"""
    hyp, _why = resolve_function_assumption(fn_name)
    return hyp


def _mk_clue(skill_id: str, fn_name: str, title: str, out: dict,
             refs: list[dict], detail_extra: dict | None = None) -> LineageClue:
    """构造线索：basis/claims/falsification 统一由 lens_basis 生成。"""
    _b = basis_for(skill_id, out)
    detail = {
        "function": fn_name,
        "basis": _b["basis"],
        "falsification": _b["falsification"],
        "claims": _b["claims"],
        "source_type": _SOURCE_TYPE,
        # 观察本身不是命题，但声明它服务于哪个假设：正兵提升为线索时
        # 以此作默认候选，避免"提升了却不知道在验证什么"。
        "assumed_hypothesis": _assumption(fn_name),
    }
    if detail_extra:
        detail.update(detail_extra)
    return LineageClue(
        skill_id=skill_id,
        title=title,
        evidence_refs=refs,
        detail=detail,
    )


# ----------------------------------------------------------------------
# 镜头 1：整数转账聚合（规则 R2 → H4）
# ----------------------------------------------------------------------
def integer_transfer_lens(miao=None, store=None, ctx=None, params=None,
                          health=None) -> list:
    """整数转账 from→to 聚合 → 每个主体对一条观察。

    按主体对出条而非按行出条：单笔整数转账本身不构成异常，**成对的、
    整万元的、可识别为过桥结构的资金流向**才是。逐行出条会把 29 笔流水
    原样摊给正兵，等于没做研判。
    """
    params = params or {}
    fn = "integer_transfer_aggregates"
    out = _invoke(store, fn, _clean(params, ["round_unit"]), health)
    rows = _rows_of(out)
    if not rows:
        _note_degraded(ctx, "fund_integer_transfer", out, {"degraded_reason": out.get("degraded_reason")})
        return []

    unit = params.get("round_unit", 10000)
    clues: list = []
    for r in rows:
        refs: list[dict] = []
        for key in ("from_raw", "to_raw"):
            ref = _subject_ref(store, r.get(key))
            if ref:
                refs.append(ref)
        refs.append({"kind": "aggregate", "metric": "total_amount",
                     "value": r.get("total")})
        # 主体解析结果带进 detail：一端歧义另一端不歧义是常态，合并即丢失
        clues.append(_mk_clue(
            "fund_integer_transfer", fn,
            f"{r.get('from_raw')} → {r.get('to_raw')} 整数转账 "
            f"{r.get('total')}（{unit} 元整数倍聚合）",
            {**out, **r}, refs,
            {"from_raw": r.get("from_raw"), "to_raw": r.get("to_raw"),
             "round_unit": unit,
             "unresolved_subjects": [
                 k for k in ("from_raw", "to_raw")
                 if not _subject_ref(store, r.get(k))]},
        ))
    return clues


# ----------------------------------------------------------------------
# 镜头 2：季末整数现金存入（规则 R1 → H1）
# ----------------------------------------------------------------------
def quarter_deposit_lens(miao=None, store=None, ctx=None, params=None,
                         health=None) -> list:
    """季末窗口内整万元现金存入按季度聚合 → 每个季度一条观察。

    引用纪律：本函数 SQL 只返回 q / cnt / amt，**不带行键**，因此无法挂
    到任何 obj_transaction 单行。这里只挂 aggregate 引用并在 detail 明说
    "季度聚合，未挂单行引用"——不去事后按季度反查一堆 txn_id 伪装成逐笔证据，
    那会把聚合事实包装成逐笔事实，是夸大。
    """
    params = params or {}
    fn = "quarter_end_integer_deposits"
    out = _invoke(store, fn, _clean(params, ["round_unit",
                                             "quarter_end_window_days",
                                             "cash_summary_tokens"]), health)
    rows = _rows_of(out)
    if not rows:
        _note_degraded(ctx, "fund_quarter_deposit", out, {"degraded_reason": out.get("degraded_reason")})
        return []

    clues: list = []
    for r in rows:
        refs = [
            {"kind": "aggregate", "metric": "deposit_count", "value": r.get("cnt")},
            {"kind": "aggregate", "metric": "deposit_amount", "value": r.get("amt")},
        ]
        window = params.get("quarter_end_window_days", 15)
        clues.append(_mk_clue(
            "fund_quarter_deposit", fn,
            f"{r.get('q')} 季度末整数现金存入 {r.get('cnt')} 笔 / "
            f"{r.get('amt')}（窗口 {window} 天）",
            {**out, **r}, refs,
            {"quarter": str(r.get("q")),
             "window_days": window,
             "aggregate_only": True,
             "aggregate_note": "季度聚合口径，函数未返回单行键，故未挂逐笔引用"},
        ))
    return clues


# ----------------------------------------------------------------------
# 镜头 3：两跳过桥路径（规则 R2 → H4）
# ----------------------------------------------------------------------
def overpass_two_hop_lens(miao=None, store=None, ctx=None, params=None,
                          health=None) -> list:
    """上游 → 桥 → 下游两跳路径，每条路径一条观察。

    ITM-05 时态口径：留空 max_gap_days 时**不声称过桥成立**（相隔数年的两笔
    转账不构成一条过桥路径）；启用后按真实日期差过滤，不可判定的路径
    标 gap_unknown 保留而非静默丢弃——路径变少会被读成"确实没有过桥"，
    而实际是"数据不全"。
    """
    params = params or {}
    fn = "overpass_two_hop"
    out = _invoke(store, fn, _clean(params, ["max_gap_days"]), health)
    rows = _rows_of(out)
    if not rows:
        _note_degraded(ctx, "fund_overpass_two_hop", out, {"degraded_reason": out.get("degraded_reason")})
        return []

    filtered = bool(params.get("max_gap_days") is not None)
    clues: list = []
    for p in rows:
        refs: list[dict] = []
        for key in ("source", "bridge", "dest"):
            ref = _subject_ref(store, p.get(key))
            if ref:
                refs.append(ref)
        for uri in (p.get("source_rows") or [])[:10]:
            refs.append({"kind": "file", "file_uri": uri})
        gap_note = ""
        if filtered:
            if p.get("gap_unknown"):
                gap_note = "；间隔不可判定（日期缺失），未做时间过滤"
            elif p.get("gap_days") is not None:
                gap_note = f"；两跳间隔 {p['gap_days']} 天"
        clues.append(_mk_clue(
            "fund_overpass_two_hop", fn,
            f"{p.get('source')} → {p.get('bridge')} → {p.get('dest')} "
            f"两跳（入 {p.get('amount_in')} / 出 {p.get('amount_out')}，"
            f"{p.get('engine')} 轨{gap_note}）",
            {**out, **p}, refs,
            {"gap_days": p.get("gap_days"),
             "gap_unknown": bool(p.get("gap_unknown")),
             "gap_filtered": filtered,
             "engine": p.get("engine")},
        ))
    return clues


# ----------------------------------------------------------------------
# 镜头 4：中标-资金时间窗碰撞（规则 R6 → H4）
# ----------------------------------------------------------------------
def time_window_collision_lens(miao=None, store=None, ctx=None, params=None,
                               health=None) -> list:
    """中标公示 ±20 天邻接边上的整数资金碰撞，每条碰撞一条观察。

    判据在规则层不在链接层：本镜头只给"碰撞"这个事实，不声称"行贿"。
    """
    params = params or {}
    fn = "time_window_collision"
    out = _invoke(store, fn, _clean(params, ["round_unit",
                                             "exclude_org_suffix"]), health)
    rows = _rows_of(out)
    if not rows:
        _note_degraded(ctx, "fund_time_window_collision", out, {"degraded_reason": out.get("degraded_reason")})
        return []

    clues: list = []
    for r in rows:
        refs: list[dict] = []
        pref = _project_ref(store, r.get("title"))
        if pref:
            refs.append(pref)
        sref = _subject_ref(store, r.get("owner_raw"))
        if sref:
            refs.append(sref)
        refs.append({"kind": "aggregate", "metric": "offset_days",
                     "value": r.get("offset_days")})
        clues.append(_mk_clue(
            "fund_time_window_collision", fn,
            f"『{r.get('title')}』公示 {r.get('pub_date')} 附近 "
            f"{r.get('owner_raw')} 整数资金 {r.get('amount')}"
            f"（偏移 {r.get('offset_days')} 天）",
            {**out, **r}, refs,
            {"owner_raw": r.get("owner_raw"),
             "pub_date": str(r.get("pub_date")),
             "offset_days": r.get("offset_days"),
             "unresolved_project": pref is None},
        ))
    return clues
