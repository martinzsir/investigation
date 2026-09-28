"""时间精度语义（date / hour / minute / second）——「可比粒度」的唯一口径。

为什么单列一个模块
------------------
trackpoint 升级后 ``date``（DE_DATE，日期级）与 ``timestamp``（DE_DATETIME，
带时刻）**并存**。这带来一个真实的误判风险：

    两个事件都是日期级（无时刻），若按类型当 datetime 比，会全部落在
    00:00:00 上 → 系统判出「两人同一时刻出现在同一地点」。

这与空间侧「李志强 259 号 vs 张卫国 100 号被算成 distance_m=0.0」是**同一类
错误**：把粗精度判据伪装成高精度判据。区别只在于——空间侧已被四级判据修掉，
时间侧此前没有对应机制。

本模块提供唯一口径：**判「能否同时」读精度，不读类型**。

精度从粗到精：date < hour < minute < second。两个事件比较时取**较低**的一档
（木桶效应），绝不允许越级声称。

派生 vs 覆盖
------------
默认**由值派生**：时刻全零（00:00:00）视为 date 档，秒非零为 second 档……
零维护，且能自动适应源数据。

但派生有一个已知盲区：**真正在午夜发生的事件**（00:00:00 是真实时刻）会被
误判成 date 档。此时由接入层显式覆盖——行内带 ``time_precision`` 字段即用其
值（合法才采纳，非法回落派生，不硬失败）。

覆盖是"补充"而非"替代"：绝大多数场景派生足够，覆盖只在接入层明确知道源
精度时使用。
"""

from __future__ import annotations

from datetime import date as _date, datetime as _dt
from typing import Any

# 精度从粗到精（顺序即可比性序：越靠右越精细）
TIME_PRECISIONS: tuple[str, ...] = ("date", "hour", "minute", "second")
_RANK = {p: i for i, p in enumerate(TIME_PRECISIONS)}

# 行内显式覆盖的键名（接入层写；不声明为本体属性，避免再动代理键哈希）
PRECISION_KEY = "time_precision"


def precision_rank(p: str | None) -> int:
    """精度序：-1 表示未知/不可比。"""
    return _RANK.get(str(p or "").strip().lower(), -1)


def _coerce_dt(v: Any) -> _dt | None:
    """把值解析为 datetime；纯 date 视为 00:00:00。无法解析 → None。"""
    if v is None:
        return None
    if isinstance(v, _dt):
        return v
    if isinstance(v, _date):
        return _dt(v.year, v.month, v.day)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H",
                "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return _dt.strptime(s, fmt)
        except ValueError:
            continue
    return None


def derive_time_precision(value: Any) -> str | None:
    """由值派生精度：**时刻全零视为 date 档**（不因类型是 datetime 就当精确）。

    返回 None 表示无值/不可解析——调用方应视为"不参与时刻级判定"。
    """
    dt = _coerce_dt(value)
    if dt is None:
        return None
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        # 全零时刻：要么真是日期级数据，要么真是午夜事件。
        # 保守取 date 档——宁可判粗（说"不支持同时"），不可判精（伪精确）。
        return "date"
    if dt.second or dt.microsecond:
        return "second"
    if dt.minute:
        return "minute"
    return "hour"


def resolve_time_precision(row: Any, *,
                           keys: tuple[str, ...] = ("timestamp", "date"),
                           declared: bool = True) -> str | None:
    """求一行的可比时间精度：显式覆盖优先，否则按 keys 顺序取首个可解析值派生。

    ``declared=False`` 时忽略行内覆盖（只派生）——用于"只认实测值"的场合。
    """
    if not isinstance(row, dict):
        return None
    if declared:
        explicit = row.get(PRECISION_KEY)
        if isinstance(explicit, str) and precision_rank(explicit) >= 0:
            return explicit.strip().lower()
    for k in keys:
        if k in row:
            p = derive_time_precision(row.get(k))
            if p:
                return p
    return None


def weaker(a: str | None, b: str | None) -> str | None:
    """两事件可比时取**较低**档（木桶效应）。任一未知 → None（不可比）。"""
    ra, rb = precision_rank(a), precision_rank(b)
    if ra < 0 or rb < 0:
        return None
    return TIME_PRECISIONS[min(ra, rb)]


def can_judge_simultaneous(a: str | None, b: str | None) -> bool:
    """能否判「同时」：**仅当两者都精确到分钟及以上**。

    date/hour 档只能判"同一天内/同一小时前后"，不得声称同时——这正是
    time_note 那句"不支持同时/同行结论"的机制化（而非文案约定）。
    """
    p = weaker(a, b)
    return p in ("minute", "second")


def time_conflict(date_v: Any, ts_v: Any) -> bool:
    """date 与 timestamp 并存时的**一致性冲突**：两者都有值且日期部分不等。

    静默取舍最危险——正兵永远不知道系统用了哪个。此处只报冲突，不裁决：
    裁决权归接入/清洗层（修数据），不由判定逻辑偷偷选一个。
    """
    d = _coerce_dt(date_v)
    t = _coerce_dt(ts_v)
    if d is None or t is None:
        return False
    return (d.year, d.month, d.day) != (t.year, t.month, t.day)


def scan_time_conflicts(conn: Any, table: str, *, pk_col: str = "",
                        date_col: str = "date", ts_col: str = "timestamp",
                        limit: int = 50) -> list[dict]:
    """扫描语义表，返回 date 与 timestamp **都有值且日期部分不等**的行（截断）。

    表/列缺失返回空列表（不硬失败——timestamp 是 optional_columns，当前数据
    很可能整列 NULL，那是正常状态不是错误）。
    """
    out: list[dict] = []
    if conn is None or not table:
        return out
    pk = pk_col or f"{table}_id"
    try:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", [table]).fetchall()}
    except Exception:
        return out
    if date_col not in cols or ts_col not in cols or pk not in cols:
        return out
    try:
        rows = conn.execute(
            f'SELECT "{pk}", "{date_col}", "{ts_col}" FROM "{table}" '
            f'WHERE "{date_col}" IS NOT NULL AND "{ts_col}" IS NOT NULL'
        ).fetchall()
    except Exception:
        return out
    for pk_v, d_v, t_v in rows:
        if time_conflict(d_v, t_v):
            out.append({"object": table, "pk": pk_v, "date": d_v,
                        "timestamp": t_v})
            if len(out) >= limit:
                break
    return out
