"""P3 物品研判镜头包实现（经 core.pack_loader 的 "impl:func" 路径加载）。

三条红线（与其它镜头包一致）
----------------------------
1. **证据引用不悬空**：hold_record 主键直接挂行引用，物品主键挂 node 引用，
   持有人 raw_name 走 obj_person / obj_org 补查（与 fund/impl._subject_ref
   同口径），查不到只挂行引用不造 node。
2. **零命中不出线索**：返回 []，降级原因经 ctx["lens_notes"] 通道留痕。
3. **不编造未计算的量**：basis 只陈述函数实际返回的字段；overlap_kind
   分 overlap/unknown_overlap 如实透传，不为 unknown 编"实际重叠"措辞。

假设归属
--------
本包 assumption 暂不强挂钩——H 系列假设尚未覆盖物品冲突场景。后续若补
H-ITEM 系列，core.lens_assumption.resolve_function_assumption 会自动
反查（基于 functions.json 的 description 与 hypothesis_patterns.json）。
"""
from __future__ import annotations

from typing import Any

from core.functions import FunctionExecutor
from core.lens_basis import basis_for
from core.registry import LineageClue

_SOURCE_TYPE = "物品研判"

# raw_name → (对象类型, 主键列)，与 fund/impl._SUBJECT_TABLES 同口径
_SUBJECT_TABLES = (("person", "person_id"), ("org", "org_id"))


def _rows_of(out: dict) -> list:
    """统一取数：sql 类 Function 结果在 out["rows"]，py 类在 out["result"]。"""
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
    """零命中/降级原因经 ctx["lens_notes"] 通道留痕（与 fund 同口径）。"""
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
    """raw_name → (person / org) node 引用；查不到返回 None 不猜不造。"""
    name = str(raw_name or "").strip()
    if not name or store is None:
        return None
    for t, col in _SUBJECT_TABLES:
        try:
            rows = store.query(
                f"SELECT {col} FROM obj_{t} WHERE raw_name = ? LIMIT 1",
                (name,))
        except Exception:
            continue  # 表不存在/未接入 → 换下一个类型，不报错
        if rows:
            return {"kind": "node", "ref": f"obj_{t}#{rows[0][col]}",
                    "key_column": col}
    return None


def _hold_ref(hold_id: Any) -> dict | None:
    """hold_record 行引用（按 hold_id 主键）。

    注意：`kind` 必须用 `node`（与 fund/relation 等包同口径），
    `core.registry._EVIDENCE_KINDS = (node/edge/time_window/aggregate/file)`
    不含 `row`——任何"具体行引用"在内核都归为 node。
    """
    hid = str(hold_id or "").strip()
    if not hid:
        return None
    return {"kind": "node", "ref": f"obj_hold_record#{hid}",
            "key_column": "hold_id"}


def _item_ref(store, item_type: Any, primary_digest: Any) -> dict | None:
    """(item_type, primary_digest) → obj_item node 引用。

    identity_key=[item_type, primary_digest]（objects.json R6），两者齐才能锚定。
    """
    it = str(item_type or "").strip()
    pd = str(primary_digest or "").strip()
    if not it or not pd or store is None:
        return None
    try:
        rows = store.query(
            "SELECT item_id FROM obj_item "
            "WHERE item_type = ? AND primary_digest = ? LIMIT 1",
            (it, pd))
    except Exception:
        return None
    if rows:
        return {"kind": "node",
                "ref": f"obj_item#{rows[0]['item_id']}",
                "key_column": "item_id"}
    return None


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
# 镜头：同号同时段冲突（规则 R-ITEM-1）
# ----------------------------------------------------------------------
def item_duplicate_hold_collision_lens(miao=None, store=None, ctx=None,
                                      params=None, health=None) -> list:
    """同 (item_type, primary_digest) 同时段多人持有 → 每对持有人一条观察。

    按对出条而非按 hold_record 逐行出条：单条持有登记本身不构成异常，
    **同一物品同时被两人持有**才是结构事实。逐行出条会让 N 条冲突登记
    原样摊给正兵，等于没做研判。
    """
    params = params or {}
    fn = "item_duplicate_hold_collision"
    out = _invoke(store, fn, _clean(params, ["min_overlap_days"]), health)
    rows = _rows_of(out)
    if not rows:
        _note_degraded(ctx, "item_duplicate_hold_collision", out,
                       {"degraded_reason": out.get("degraded_reason")})
        return []

    clues: list = []
    for r in rows:
        item_type = r.get("item_type")
        primary_digest = r.get("primary_digest")
        holder_a = r.get("holder_a")
        holder_b = r.get("holder_b")
        overlap_kind = str(r.get("overlap_kind") or "overlap")
        overlap_days = r.get("overlap_days") or 0
        # 证据引用：物品 + 两条持有登记 + 两个持有人（查得到才挂）
        refs: list[dict] = []
        item_ref = _item_ref(store, item_type, primary_digest)
        if item_ref:
            refs.append(item_ref)
        for hid in (r.get("hold_a_id"), r.get("hold_b_id")):
            hr = _hold_ref(hid)
            if hr:
                refs.append(hr)
        for holder in (holder_a, holder_b):
            sr = _subject_ref(store, holder)
            if sr:
                refs.append(sr)
        # 标题：unknown_overlap 时降级文案，避免"实际重叠 N 天"误导
        item_label = (f"{item_type}:{primary_digest}"
                      if primary_digest else "未知物品")
        if overlap_kind == "unknown_overlap":
            title = (f"{item_label} 被 {holder_a} 与 {holder_b} 同时登记持有"
                     "（区间不可判定）")
            basis_extra = "区间起止缺失，仅判同号同时段登记冲突"
        else:
            title = (f"{item_label} 被 {holder_a} 与 {holder_b} 同时段持有"
                     f"（重叠约 {overlap_days} 天）")
            basis_extra = None
        detail_extra = {
            "item_type": item_type,
            "primary_digest": primary_digest,
            "holder_a": holder_a,
            "holder_b": holder_b,
            "overlap_kind": overlap_kind,
            "overlap_days": overlap_days,
            "a_start": r.get("a_start"),
            "a_end": r.get("a_end"),
            "b_start": r.get("b_start"),
            "b_end": r.get("b_end"),
            "hold_a_id": r.get("hold_a_id"),
            "hold_b_id": r.get("hold_b_id"),
        }
        if basis_extra:
            detail_extra["degraded"] = True
            detail_extra["degraded_reason"] = basis_extra
        clues.append(_mk_clue(
            "item_duplicate_hold_collision", fn, title,
            {**out, **r}, refs, detail_extra))
    return clues
