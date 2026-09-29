"""
packs/comm/impl.py
通讯研判镜头包实现（经 pack_loader 的 "impl:func" 路径加载）。

包装对象：call_frequency_spike —— 此前是无 Lens 包装的裸 Function，
只能经画布 RC-204 白名单直连调用，进不了镜头目录。

红线（与 packs/fund 同）：
  1. 证据引用不悬空：函数返回 caller_raw / callee_raw，先补查 obj_person
     拿 person_id 再挂 node 引用；查不到只挂 aggregate，不凭名字造 ref。
  2. 零命中不出线索，降级原因落 ctx。
  3. 不编造未计算的量：函数没算的基线（如逐月走势）basis 不写。

措辞纪律：本函数判据是「头部对端频次 ≥2 倍常态中位数」，**仅示高频**。
无时间序列基线时算不出「突增」，因此标题与 basis 一律不出现"突增"字样——
那是把一次截面比较说成趋势判断，属于夸大。
"""
from __future__ import annotations

from core.functions import FunctionExecutor
from core.lens_assumption import resolve_function_assumption
from core.lens_basis import basis_for
from core.registry import LineageClue

_SOURCE_TYPE = "通讯研判"


def _invoke(store, fn_name: str, fn_params: dict, health) -> dict:
    return FunctionExecutor(store, health=health).invoke(fn_name, fn_params)


def _clean(params: dict, keys: list[str]) -> dict:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _note_degraded(ctx, skill_id: str, out: dict) -> None:
    """零命中/降级留痕到 ctx["lens_notes"]（ctx 是 dict，不是对象）。

    区分「数据源未接入」与「确实没有高频对端」：两者界面上都显示为空，
    不留痕就无从分辨。
    """
    if not isinstance(ctx, dict):
        return
    r = out.get("result") or {}
    ctx.setdefault("lens_notes", []).append({
        "skill_id": skill_id,
        "function": out.get("function"),
        "degraded": bool(out.get("degraded")),
        "degraded_reason": out.get("degraded_reason"),
        "diagnostics": r.get("diagnostics"),
        "zero_hit": True,
    })


def _person_ref(store, raw_name) -> dict | None:
    """raw_name → person node 引用。查不到返回 None，不猜不造。"""
    name = str(raw_name or "").strip()
    if not name or store is None:
        return None
    try:
        rows = store.query(
            "SELECT person_id FROM obj_person WHERE raw_name = ? LIMIT 1", (name,))
    except Exception:
        return None
    if rows:
        return {"kind": "node", "ref": f"obj_person#{rows[0]['person_id']}",
                "key_column": "person_id"}
    return None


def call_frequency_lens(miao=None, store=None, ctx=None, params=None,
                        health=None) -> list:
    """通话高频 → 头部对端一条观察（不是每对通话一条）。

    为什么只在 hit 时出条
    --------------------
    函数判据是"头部对端 ≥2 倍常态中位数"。未达判据时出条等于把一份
    "频次分布"原样摊给正兵，那是数据不是研判。但零命中/降级必须落 ctx，
    否则"数据源没接入"和"确实没有高频对端"在界面上都显示为空，无从区分。
    """
    params = params or {}
    fn = "call_frequency_spike"
    out = _invoke(store, fn, _clean(params, ["absolute_threshold"]), health)
    r = out.get("result") or {}
    if not r.get("hit"):
        _note_degraded(ctx, "comm_call_frequency", out)
        return []

    pairs = r.get("pairs") or []
    if not pairs:
        return []

    refs: list[dict] = []
    seen: set[str] = set()
    for p in pairs:
        for key in ("caller_raw", "callee_raw"):
            name = str(p.get(key) or "").strip()
            if not name or name in seen:
                continue
            ref = _person_ref(store, name)
            if ref:
                refs.append(ref)
                seen.add(name)
    # 频次是聚合量，不算逐条通话引用
    for p in pairs[:5]:
        refs.append({"kind": "aggregate", "metric": "call_times",
                     "value": p.get("times")})

    _b = basis_for("comm_call_frequency", r)
    hyp, _why = resolve_function_assumption(fn)
    return [LineageClue(
        skill_id="comm_call_frequency",
        title=f"{r.get('basis') or '头部对端通话频次显著高于常态'}",
        evidence_refs=refs,
        detail={
            "function": fn,
            "basis": _b["basis"],
            "falsification": _b["falsification"],
            "claims": _b["claims"],
            "source_type": _SOURCE_TYPE,
            "assumed_hypothesis": hyp,
            "subject": r.get("subject"),
            "pairs": pairs,
            "diagnostics": r.get("diagnostics"),
            "threshold_used": (r.get("diagnostics") or {}).get("threshold_used"),
            # 措辞纪律：本镜头只示高频，不称突增（无时间序列基线）
            "not_claiming": "突增",
        },
    )]
