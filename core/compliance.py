"""
core/compliance.py —— 数据元合规扫描（REQ-D-016）。

只读：走语义层读网关（OntologyReadGateway）消费 obj_*，不直读 Parquet、
不动 obj_* schema、零业务写路径。按数据元 format/checksum/range/enum（代码表）
对"已引用数据元"的已物化属性逐值扫描：

  - 扫描目标 = objects.json 属性声明 {"data_element": "DE_X"}（loader 已校验
    ID 已注册）；未引用数据元的属性不扫（AC-8）；
  - 检查项可单独启停：调用方 checks 参数 或 data_elements.json 顶层
    compliance_checks 声明（AC-6，见 core.ontology_loader.load_compliance_checks）；
  - 不静默机制：违规行以"对象.属性 + 代理键(pk) + 违规码"落 run_diagnostic
    （kind=compliance_violation，可下钻；样本一律脱敏——违规值可能是敏感值）；
  - 聚合结果返回给画像（每属性 checked/violations/rate/codes）。

注意：网关需 system 上下文（内部质量门，非分析师读数）——非 system 上下文的
属性遮蔽会把掩码值当原值扫描，产生误报。
"""
from __future__ import annotations

import re

from core.data_elements import CHECKSUM_ALGOS
from core.ontology import _mask_sample
from core.ontology_loader import (
    COMPLIANCE_CHECK_NAMES,
    load_compliance_checks,
    load_data_elements,
    load_pack,
)
from core.run_health import get_health

# 违规码（落 run_diagnostic detail.code，可下钻统计）
CODE_FORMAT = "format_mismatch"
CODE_CHECKSUM = "checksum_failed"
CODE_RANGE = "range_violation"
CODE_ENUM = "enum_unknown"


def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _check_value(v, element: dict, checks: tuple) -> list[str]:
    """单值合规检查，返回违规码列表（空 = 合规）。"""
    codes: list[str] = []
    if "format" in checks and element.get("format"):
        if not re.fullmatch(element["format"], str(v)):
            codes.append(CODE_FORMAT)
    if "checksum" in checks and element.get("checksum"):
        algo = CHECKSUM_ALGOS.get(element["checksum"])
        if algo is not None and not algo(str(v)):
            codes.append(CODE_CHECKSUM)
    if "range" in checks and element.get("range"):
        rng = element["range"]
        lo, hi = rng.get("min"), rng.get("max")
        # date/timestamp 物化列的值是 date/datetime 对象（非 str）：转 ISO 字符串
        # 走字典序比较（ISO 格式字典序=时间序），否则 float() 失败被静默跳过漏检
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        try:
            if (isinstance(v, str) and (lo is not None and not _is_number(lo)
                                        or hi is not None and not _is_number(hi))):
                # 边界为非数值字符串（如 ISO 日期）→ 字典序比较
                if lo is not None and v < str(lo):
                    codes.append(CODE_RANGE)
                elif hi is not None and v > str(hi):
                    codes.append(CODE_RANGE)
            else:
                val = float(v)
                if lo is not None and val < float(lo):
                    codes.append(CODE_RANGE)
                elif hi is not None and val > float(hi):
                    codes.append(CODE_RANGE)
        except (TypeError, ValueError):
            pass    # 不可比的值类型不归 range 管（format/type 检查覆盖）
    if "enum" in checks:
        allowed = element.get("enum")
        if allowed and v not in allowed:
            codes.append(CODE_ENUM)
    return codes


_CHECK_FIELDS = {
    "format": "format",
    "checksum": "checksum",
    "range": "range",
    "enum": "enum",
}


def _applicable_checks(element: dict, enabled: tuple) -> list[str]:
    """该数据元在当前启用项中实际可执行的检查（声明了对应字段才算）。

    引用数据元但未声明任何检查字段（如 DE_NAME 仅姓名）→ 空列表：
    算扫描目标（targets 计数）但不逐值检查、不入 by_property。
    """
    return [c for c in enabled if element.get(_CHECK_FIELDS[c])]


def resolve_checks(pack: str, checks: tuple | None) -> tuple:
    """生效检查项：显式参数优先；否则按 data_elements.json 顶层
    compliance_checks 声明（未声明的检查项默认开，AC-6）。"""
    if checks is not None:
        return tuple(checks)
    off = load_compliance_checks(pack)
    return tuple(c for c in COMPLIANCE_CHECK_NAMES if off.get(c, True))


def scan(gateway, *, health=None, checks=None, max_records: int = 200) -> dict:
    """对已物化对象执行合规扫描，返回聚合摘要（画像/健康度消费）。

    checks：显式启用检查项元组（None = 按 data_elements.json 顶层
    compliance_checks 声明，未声明项默认全开）。
    max_records：run_diagnostic 违规明细落账上限（超出部分仍计数，可经
    by_property 下钻；防止脏数据放大成诊断风暴）。
    """
    rh = get_health(health)
    pack = gateway.explain()["pack"]
    elements = load_data_elements(pack)
    enabled = resolve_checks(pack, checks)
    targets: list = []
    for o in load_pack(pack).objects:
        if o.runtime or not o.prop_data_elements:
            continue
        for prop, de in o.prop_data_elements.items():
            element = elements.get(de)
            if element is not None:
                targets.append((o, prop, element))

    mat = set(gateway.materialized_objects())
    mat_props = gateway.materialized_props()
    by_property: dict[str, dict] = {}
    total = recorded = 0
    for o, prop, element in targets:
        if o.name not in mat or not mat_props.get(f"{o.name}.{prop}"):
            continue    # 未物化对象/缺列：画像层另行标注，此处不扫
        applicable = _applicable_checks(element, enabled)
        if not applicable:
            continue    # 引用但无检查字段（如 DE_NAME）：算目标不逐值扫描
        rows = gateway.objects(o.name)
        checked = 0
        codes: dict[str, int] = {}
        for r in rows:
            v = r.get(prop)
            if v is None:
                continue
            checked += 1
            for code in _check_value(v, element, tuple(applicable)):
                codes[code] = codes.get(code, 0) + 1
                total += 1
                if recorded < max_records:
                    recorded += 1
                    rh.record(
                        "compliance_violation", "warning", source="compliance",
                        reason=(f"{o.name}.{prop}<-{r.get(o.pk)}: {code}"
                                f"（数据元 {o.prop_data_elements[prop]}，"
                                f"样本 {_mask_sample(v)}）"),
                        object=o.name, prop=prop, key=str(r.get(o.pk)),
                        code=code, element=o.prop_data_elements[prop])
        if checked:
            by_property[f"{o.name}.{prop}"] = {
                "element": o.prop_data_elements[prop],
                "checked": checked,
                "violations": sum(codes.values()),
                "rate": round(sum(codes.values()) / checked, 4),
                "codes": codes,
            }
    return {"targets": len(targets), "scanned": len(by_property),
            "checks": list(enabled), "violations": total, "recorded": recorded,
            "by_property": by_property}
