"""
core/unit_scan.py —— 单位与口径一致性扫描（REQ-D-020）。

只读：走语义层读网关消费 obj_*，不直读 Parquet、零业务写路径、只告警不阻断。
元/万元混用、百分比与小数混用这类问题统计特征看不出来，却会让所有金额类结论
错误。检测两路（均为**提示**，不自动定性——金额突增本身可能是真实业务）：

  - AC-1/AC-2 量级矛盾：同一金额类数据元（decimal/integer）被多个对象/表引用时，
    各列中位数跨对象相差 ≥ ratio_threshold（默认 10000 倍 = 元 vs 万元的典型量级
    差）→ 疑似单位混用/金额分布异常突增，提示人工核对；
  - AC-3 单位缺失：金额类属性引用的数据元未声明 unit → info 提示补充单位声明
    （非硬失败）。

单位声明位置：data_elements.json 元素 spec 的 "unit" 字段（如 "元"/"万元"/"%"/"小数"），
loader 原样透传（缺失不报错）。AC-4：全部落 run_diagnostic（kind=unit_mismatch），
绝不抛异常/中断装载。
"""
from __future__ import annotations

import statistics

from core.ontology_loader import load_data_elements, load_pack
from core.run_health import get_health

# 金额类值类型（参与单位/量级检测）
_AMOUNT_TYPES = ("decimal", "integer")
# 金额列名启发词根（数据元未声明 unit 时，用于判断"疑似金额列"以给缺失提示）
_AMOUNT_NAME_HINTS = ("amount", "金额", "余额", "数额", "钱款", "交易金额")


def _num(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _median(vals: list[float]) -> float:
    return float(statistics.median(vals))


def scan(gateway, *, health=None, ratio_threshold: float = 10000.0) -> dict:
    """扫描金额类数据元跨表单位/量级一致性。

    返回 {groups, mismatches, missing_unit}：groups 为每个金额类数据元的逐列
    中位数与声明单位；mismatches 为跨列量级差 ≥ ratio_threshold 的疑似混用；
    missing_unit 为未声明 unit 的金额类数据元属性（AC-3 提示）。
    """
    rh = get_health(health)
    pack = gateway.explain()["pack"]
    elements = load_data_elements(pack)
    spec = load_pack(pack)
    mat = set(gateway.materialized_objects())
    mat_props = gateway.materialized_props()

    groups: dict[str, list[dict]] = {}
    missing_unit: list[dict] = []
    for o in spec.objects:
        if o.runtime or o.name not in mat or not o.prop_data_elements:
            continue
        for prop, de in o.prop_data_elements.items():
            if o.properties.get(prop) not in _AMOUNT_TYPES:
                continue
            if not mat_props.get(f"{o.name}.{prop}"):
                continue
            element = elements.get(de) or {}
            vals = [nv for r in gateway.objects(o.name)
                    if (nv := _num(r.get(prop))) is not None]
            if not vals:
                continue
            med = _median(vals)
            unit = element.get("unit")
            col = {"object": o.name, "property": prop, "unit": unit,
                   "median": round(med, 4), "element": de}
            groups.setdefault(de, []).append(col)
            if not unit:
                name_like = any(h in prop.lower() for h in
                                ("amount",) ) or any(h in prop for h in
                                ("金额", "余额", "数额", "钱款"))
                missing_unit.append({"object": o.name, "property": prop,
                                     "element": de, "name_like_amount": name_like})
                rh.record(
                    "unit_mismatch", "info", source="unit_scan",
                    reason=(f"{o.name}.{prop}（数据元 {de}）金额类属性未声明 unit"
                            f"（REQ-D-020 AC-3：建议在 data_elements.json 补充单位"
                            f"如 元/万元/%，非硬失败）"),
                    object=o.name, prop=prop, element=de, issue="unit_missing")

    mismatches: list[dict] = []
    for de, cols in groups.items():
        if len(cols) < 2:
            continue
        positive = [c for c in cols if c["median"] > 0]
        if len(positive) < 2:
            continue
        hi = max(positive, key=lambda c: c["median"])
        lo = min(positive, key=lambda c: c["median"])
        ratio = hi["median"] / lo["median"]
        if ratio >= ratio_threshold:
            units = {c.get("unit") for c in cols}
            rec = {"element": de, "ratio": round(ratio, 1),
                   "units": sorted(u for u in units if u),
                   "high": f"{hi['object']}.{hi['property']}（中位数 {hi['median']}）",
                   "low": f"{lo['object']}.{lo['property']}（中位数 {lo['median']}）"}
            mismatches.append(rec)
            rh.record(
                "unit_mismatch", "warning", source="unit_scan",
                reason=(f"数据元 {de} 跨表金额量级相差 {ratio:.0f} 倍 ≥ {ratio_threshold:.0f}"
                        f"（{rec['low']} vs {rec['high']}；声明单位 {sorted(u for u in units if u) or '未全声明'}）"
                        f"——疑似元/万元混用或金额突增，需人工核对（只提示不定性，REQ-D-020 AC-1/AC-2）"),
                element=de, issue="magnitude_mismatch", ratio=round(ratio, 2))

    return {"groups": groups, "mismatches": mismatches,
            "missing_unit": missing_unit}
