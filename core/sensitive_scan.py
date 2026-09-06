"""
core/sensitive_scan.py —— 敏感列启发式扫描（REQ-D-018）。

只读：走语义层读网关消费 obj_*。两路启发式检出"疑似敏感但未声明遮蔽"的列：

  - 列名启发式：属性名含 id_card/idcard/phone/mobile/card/证件/身份证/手机/电话/卡号
    等词根（大小写不敏感）；
  - 值模式：18 位身份证 / 11 位手机（1[3-9]开头）/ 16-19 位银行卡号形态
    （按优先级首中即停，身份证优先于泛卡号，控制误报）。

去重口径（AC-2）：已在 policies.json property_policies 声明遮蔽的属性不再报。
输出（AC-4）：建议补充遮蔽声明；只告警不阻断（AC-5）——只落 run_diagnostic
（kind=sensitive_column_suspect，severity=warning），绝不抛异常/中断流程。
误报率可测（AC-3）：返回值含 scanned 属性数与逐列证据，可用陷阱列量化。

值模式只作用于 string 属性的非空去重值样本；命中率 ≥ hit_ratio 才报
（默认 0.3，防止偶发命中放大）。扫描需 system 上下文网关（掩码值会干扰模式）。
"""
from __future__ import annotations

import re

from core.ontology import _mask_sample
from core.ontology_loader import load_pack
from core.policy import PolicyEngine
from core.run_health import get_health

# 列名词根 → 证据码（按序匹配；bankcard 词根刻意不含泛化 "card"——避免误中 id_card）
COLUMN_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name_idcard", ("id_card", "idcard", "身份证", "证件")),
    ("name_phone", ("phone", "mobile", "手机", "电话")),
    ("name_bankcard", ("bank_card", "bankcard", "card_no", "cardno", "银行卡", "卡号")),
)

# 值模式（有序：首中即停；身份证/手机先于泛卡号，控制误报）
VALUE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("value_idcard", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("value_phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("value_bankcard", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
)

SUGGESTION = ("建议在 policies.json property_policies 补充遮蔽声明"
              "（REQ-D-018；只告警不阻断，需人工确认后声明）")


def _name_hints(prop: str) -> list[str]:
    low = prop.lower()
    return [code for code, roots in COLUMN_HINTS if any(r in low for r in roots)]


def _value_evidence(vals: list, hit_ratio: float) -> dict | None:
    """值模式证据：按优先级首中即停；命中率不足不报（AC-3 误报控制）。"""
    if not vals:
        return None
    for code, rx in VALUE_PATTERNS:
        hits = [v for v in vals if rx.search(str(v))]
        if hits and len(hits) / len(vals) >= hit_ratio:
            return {"pattern": code, "hits": len(hits), "distinct": len(vals),
                    "ratio": round(len(hits) / len(vals), 4),
                    "sample_masked": _mask_sample(hits[0])}
    return None


def scan(gateway, *, health=None, sample_limit: int = 200,
         hit_ratio: float = 0.3, policy: PolicyEngine | None = None) -> dict:
    """扫描未声明遮蔽的疑似敏感列。返回 {suspects, scanned, details}。

    policy：策略引擎实例（None = 按 pack 默认路径装载 PolicyEngine；
    临时案件包测试可显式传入以指定 policies.json 路径）。
    suspects：疑似列数；scanned：实际取值扫描的 string 属性数（误报率分母）；
    details：逐列证据（object/property/evidence/suggestion）。
    """
    rh = get_health(health)
    pack = gateway.explain()["pack"]
    spec = load_pack(pack)
    pe = policy if policy is not None else PolicyEngine(pack)
    declared = pe.property_policies   # (obj, prop) → 已声明遮蔽
    mat = set(gateway.materialized_objects())
    mat_props = gateway.materialized_props()

    details: list[dict] = []
    scanned = 0
    for o in spec.objects:
        if o.runtime or o.name not in mat:
            continue
        for prop, ptype in o.properties.items():
            if not mat_props.get(f"{o.name}.{prop}"):
                continue
            if (o.name, prop) in declared:
                continue    # AC-2：已声明遮蔽的属性不报（去重口径）
            hints = _name_hints(prop)
            evidence: list[dict] = [{"name_hint": h} for h in hints]
            if ptype == "string":
                vals = [v for v in gateway.distinct_values(
                    o.name, prop, limit=sample_limit) if v is not None]
                if vals:
                    scanned += 1
                    ve = _value_evidence(vals, hit_ratio)
                    if ve:
                        evidence.append(ve)
            elif hints:
                # 非字符串列只吃列名词根证据（值模式需文本形态）
                scanned += 1
            if not evidence:
                continue
            codes = [e.get("name_hint") or e.get("pattern") for e in evidence]
            details.append({
                "object": o.name, "property": prop,
                "evidence": evidence, "suggestion": SUGGESTION,
            })
            rh.record(
                "sensitive_column_suspect", "warning",
                source="sensitive_scan",
                reason=(f"{o.name}.{prop} 疑似敏感列未声明遮蔽"
                        f"（证据：{', '.join(codes)}）；{SUGGESTION}"),
                object=o.name, prop=prop, evidence=evidence,
                suggestion=SUGGESTION)
    return {"suspects": len(details), "scanned": scanned, "details": details}
