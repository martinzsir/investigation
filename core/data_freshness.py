"""
core/data_freshness.py —— 数据时间新鲜度扫描（REQ-D-019）。

只读：走语义层读网关消费 obj_*，不直读 Parquet、零业务写路径。回答"我看到的
流水是多久以前的"——按对象的 date 类型时间属性取 MAX(数据时间)，与当前日期比对，
超期（可配 stale_days）告警。

与本体版本新鲜度（core/ontology_version.freshness 的 FRESH/STALE/UNBUILT，
回答"语义层声明是否过期"）**分开显示、互不混淆**：本扫描 kind=data_freshness_stale、
source=data_freshness，回答"数据本身多久没更新"。

空时间属性不误报（AC-3）：对象的时间列全为 NULL / 无 date 属性 → 跳过，不判"很旧"。
只告警不阻断；样本不落明细（时间非敏感，但仍只读）。

未来日期（AC-6）：数据时间晚于参照日期超过容差（默认 1 天，吸收当日提前录入与时区差）
→ kind=data_freshness_future 告警。修之前 age 为负天数，`age > stale_days` 恒假，
2099-01-01 这类脏值/时区错误被判"数据新鲜"静默通过（demoD 四张表各 1 行）。
"""
from __future__ import annotations

import datetime as _dt

from core.ontology_loader import load_pack
from core.run_health import get_health


def _to_date(v):
    """date / datetime / ISO 字符串 → datetime.date；不可解析 → None。"""
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def scan(gateway, *, health=None, stale_days: int = 180, as_of=None,
         base_dir: "Path | None" = None,
         future_tolerance_days: int = 1) -> dict:
    """扫描各含时间属性对象的最新数据时间，超期（或超前）告警。

    stale_days：超期阈值（天，默认 180≈半年，AC-2 可配）；as_of：参照日期
    （测试注入，默认 date.today()）。返回 {objects, stale, future, stale_days, as_of}。
    base_dir：案件快照 ontology 根（缺省 None = 模板包，CLI/MCP 行为不变）。
    future_tolerance_days：允许数据时间早于参照日期的容差（默认 1 天）。
    """
    rh = get_health(health)
    pack = gateway.explain()["pack"]
    spec = load_pack(pack, base_dir=base_dir)
    mat = set(gateway.materialized_objects())
    mat_props = gateway.materialized_props()
    ref = as_of or _dt.date.today()

    objects_fresh: list[dict] = []
    stale = 0
    future = 0
    for o in spec.objects:
        if o.runtime or o.name not in mat:
            continue
        date_props = [p for p, t in o.properties.items()
                      if t == "date" and mat_props.get(f"{o.name}.{p}")]
        for prop in date_props:
            vals = [r.get(prop) for r in gateway.objects(o.name)
                    if r.get(prop) is not None]
            if not vals:
                continue   # AC-3：时间属性全空 → 不误报为"很旧"
            latest_d = _to_date(max(vals, key=lambda x: str(x)))
            if latest_d is None:
                continue
            age = (ref - latest_d).days
            entry = {"object": o.name, "property": prop,
                     "latest": str(latest_d), "age_days": age}
            objects_fresh.append(entry)
            if age < -max(0, future_tolerance_days):
                future += 1          # AC-6：未来日期不再被判"数据新鲜"
                rh.record(
                    "data_freshness_future", "warning", source="data_freshness",
                    reason=(f"{o.name}.{prop} 最新数据时间 {latest_d} 晚于参照日期 "
                            f"{ref}（超前 {-age} 天 > 容差 {future_tolerance_days} 天）"
                            f"——疑似日期格式/时区错误或脏值，请复核源数据"),
                    **entry)
            elif age > stale_days:
                stale += 1
                rh.record(
                    "data_freshness_stale", "warning", source="data_freshness",
                    reason=(f"{o.name}.{prop} 最新数据时间 {latest_d}，距今 {age} 天"
                            f" > 阈值 {stale_days} 天（数据时间旧；与本体版本 FRESH/STALE 无关）"),
                    **entry)
    return {"objects": objects_fresh, "stale": stale, "future": future,
            "stale_days": stale_days, "as_of": str(ref)}
