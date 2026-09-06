"""
core/de_recommend.py
REQ-D-021 数据元驱动落点推荐（纯函数，只读）。

新表接入画像时，对每一列用「列名别名 + 值模式」匹配已注册数据元（data_elements.json），
推荐该列应挂的数据元以及配套的 clean/transform 规则 token。

红线（REQ-D-021）：
  - 只产出**推荐**（draft 提案），永不自动写 objects.json / 不自动生效；
  - 低置信度一律标 needs_confirmation（需人工确认）；
  - 混装复合列不给单一数据元，只给上游拆分提示（split_hint），拆分走 source_sql。

匹配信号（值模式为主、列名别名为辅）：
  - enum：样本值落入数据元枚举值域的比例（代码表：性别/币种/证件类型/案件类别…）；
  - format：样本值匹配数据元 format 正则的比例（如身份证 18 位）；
  - 列名别名：列名包含数据元 name 或其 alias 列表（数据元可选 alias 字段，声明式）。
规则提示按值类型（value_type.analyze_column 的否定式类型）派生 op token。
"""
from __future__ import annotations

import re

from core.value_type import analyze_column

CONFIRM_THRESHOLD = 0.70   # 命中率低于此值不推荐
HIGH_THRESHOLD = 0.90      # 命中率达到此值且为强信号 → high（否则 medium 需人工确认）


def _name_hit(col_name: str, spec: dict) -> bool:
    """列名别名命中数据元 name 或 alias（大小写/空白不敏感）。"""
    cn = str(col_name).lower().replace(" ", "").replace("　", "")
    names = [str(spec.get("name", "")).lower()]
    names += [str(a).lower() for a in (spec.get("alias") or [])]
    return any(nm and nm in cn for nm in names if nm)


def _match_rate(values: list[str], spec: dict) -> tuple[float, str]:
    """返回 (命中率, 匹配方式)。enum/format 取命中率高者；无信号 → (0.0, "")。"""
    vals = [str(v) for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return 0.0, ""
    best_rate, best_by = 0.0, ""
    enum = spec.get("enum")
    if enum:
        eset = {str(e) for e in enum}
        hit = sum(1 for v in vals if v in eset)
        rate = hit / len(vals)
        if rate > best_rate:
            best_rate, best_by = rate, "enum"
    fmt = spec.get("format")
    if fmt:
        try:
            rx = re.compile(fmt)
            hit = sum(1 for v in vals if rx.match(v))
            rate = hit / len(vals)
            if rate > best_rate:
                best_rate, best_by = rate, "format"
        except re.error:
            pass
    return best_rate, best_by


def _rule_hints(ana: dict, values: list[str]) -> dict:
    """按值类型派生 clean/transform op token 建议（只建议，不自动挂接）。"""
    hints: dict = {}
    neg = ana.get("negative_types") or []
    if "phone" in neg:
        hints["transform"] = ["digits_only", "strip_cc"]
    elif "date_str" in neg:
        hints["transform"] = ["cn_date_norm", "pad_date"]
    if "account" in neg:
        masked = any(("*" in str(v) or "×" in str(v)) for v in values)
        hints["clean"] = (["reject_if:contains_mask", "digits_only"] if masked
                          else ["digits_only"])
    return hints


def recommend_for_column(col_name: str, values: list, elements: dict,
                         *, confirm_threshold: float = CONFIRM_THRESHOLD,
                         high_threshold: float = HIGH_THRESHOLD) -> dict:
    """对单列推荐数据元 + clean/transform 规则。

    返回 {"col", "recommendations":[...], "split_hint": str|None,
          "needs_confirmation": bool}。recommendations 元素：
      data_element / de_name / match_rate / match_by / confidence /
      sensitive / needs_confirmation / clean? / transform?
    """
    vals = [str(v) for v in (values or []) if v is not None and str(v).strip() != ""]
    ana = analyze_column(vals)

    # 复合列：≥2 归一落点 → 不推单一数据元，提示上游拆分
    if ana.get("mixed") and len(ana.get("landing_suggestions") or []) >= 2:
        return {
            "col": str(col_name),
            "recommendations": [],
            "split_hint": ("混装落点 %s，建议在 source_sql 上游拆分为多列，"
                           "勿映射单一数据元" % ana["landing_suggestions"]),
            "needs_confirmation": True,
        }

    recs: list[dict] = []
    for eid in sorted(elements or {}):
        spec = elements[eid]
        if not isinstance(spec, dict):
            continue
        rate, by = _match_rate(vals, spec)
        if rate < confirm_threshold:
            continue
        confidence = "high" if rate >= high_threshold and by in ("enum", "format") else "medium"
        rec = {
            "data_element": eid,
            "de_name": spec.get("name", eid),
            "match_rate": round(rate, 3),
            "match_by": by,
            "confidence": confidence,
            "sensitive": bool(spec.get("sensitive")),
            "needs_confirmation": confidence != "high",
        }
        rec.update(_rule_hints(ana, vals))
        recs.append(rec)

    # 无数据元命中但值模式明确（手机/日期/账号）→ 仍给规则建议，标需人工确认
    if not recs:
        hints = _rule_hints(ana, vals)
        if hints.get("transform") or hints.get("clean"):
            recs.append({
                "data_element": None, "de_name": None,
                "match_rate": 0.0, "match_by": "value_pattern",
                "confidence": "medium", "sensitive": False,
                "needs_confirmation": True, **hints,
            })

    return {
        "col": str(col_name),
        "recommendations": recs,
        "split_hint": None,
        "needs_confirmation": any(r.get("needs_confirmation") for r in recs),
    }


def recommend_for_table(columns: list[str], col_values: dict, elements: dict) -> list[dict]:
    """对整表逐列推荐（列名 → 该列推荐结果 dict）。只读，不产生任何写动作。"""
    return [recommend_for_column(c, col_values.get(c, []), elements) for c in columns]
