"""core/hold_chain.py —— 物品持有链（R2 时态边 / D2，及 D4 同源红线）。

为什么必须单列
--------------
``links.json`` 原有 14 个链接类型里**没有任何一个表达"持有"**。物品因此只能
挂在人身上当一行属性，而物品与人的根本区别在于：

    **人会转移关系，物品会转移归属。**

甲→车、乙→车 若只画静态边，会被读成"两人共用一辆车"；真实形态可能是
"甲卖给了乙"——**性质完全相反的两种事实**。区分二者的唯一依据是时间区间，
所以持有边的时态不是可选装饰，而是这条边能否被正确理解的前提。

三条红线（改动前务必读）
------------------------
1. **不可判定返回 unknown，绝不静默当"无约束"**。
   把"时间没记录"默认成"一直持有"，会让正兵据此下错结论。开放起点算
   "持有到有证据为止"、开放终点算"持续持有"——两者都是有明确口径的半开
   区间，与"不知道"是三回事，必须分得清。

2. **按登记顺序排，不按时间重排**。
   重排会把"登记顺序与登记时间矛盾"这个信号抹掉，而那正是虚假登记的线索。
   错序只标记，不纠正。

3. **同一物品同时被多方持有必须报冲突，不静默取第一个**。
   对车是套牌线索，对房产是产权异常——这不是数据噪声，是信号本身。

精度口径一律复用 ``core.time_semantics``（唯一真相源），本模块不自带精度表：
只有两端都精确到分钟及以上才判"同时"，日期级只判"同一天先后"，不得声称重叠。
"""
from __future__ import annotations

from datetime import date as _date, datetime as _dt
from typing import Any

from core.time_semantics import (
    derive_time_precision,
    weaker,
    can_judge_simultaneous,
)

# 归一化后的时间形态：datetime→"YYYY-MM-DD HH:MM:SS"；date→"YYYY-MM-DD"。
# **date 绝不补零时刻**——补成 "00:00:00" 会把日期级伪装成时刻级，
# 与 core/time_semantics 派生口径（全零时刻判 date 档）同源。


def _norm(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, _dt):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, _date):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def _pick(row: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in row and row.get(k) not in (None, ""):
            return row.get(k)
    return None


def normalize_hold(row: dict, *, order: int = 0, seq: int = 0) -> dict:
    """把一条持有记录归一化为前端 ``ChainStep`` 形态。

    ``unknownRange`` 为 True 表示**区间不可判定**——不是"没有时间"，是"不知
    道"。两端皆空才是它；半开区间（只有起点或只有终点）是有明确口径的，
    不算不可判定，由 ``rangeText`` 侧表述为"起/在持"。
    """
    if not isinstance(row, dict):
        row = {}
    holder = _pick(row, ("holder", "holder_raw", "holder_name")) or ""
    start_raw = _pick(row, ("start", "start_date", "acquire_date", "from_date"))
    end_raw = _pick(row, ("end", "end_date", "dispose_date", "to_date"))
    start, end = _norm(start_raw), _norm(end_raw)

    p_start = derive_time_precision(start_raw)
    p_end = derive_time_precision(end_raw)

    rid = row.get("id") or row.get("hold_id") or ""
    if not rid:
        rid = f"{holder or '?'}#{seq}"

    return {
        "id": str(rid),
        "holder": str(holder),
        "start": start,
        "end": end,
        "order": int(order if order else row.get("order") or 0) or seq,
        "unknownRange": not start and not end,
        # 精度随记录带出，供下游判重叠；不在此处裁决
        "start_precision": p_start,
        "end_precision": p_end,
    }


def _interval_of(step: dict) -> tuple[str, str]:
    """半开区间：起点空→负无穷（持有到有证据为止）；终点空→正无穷（仍在持）。"""
    s = step.get("start") or ""
    e = step.get("end") or ""
    return (s or "", e or "")


def _comparable(a: dict, b: dict) -> bool:
    """两端精度是否足以判"同时"——取最弱档（木桶效应），复用唯一口径。"""
    ps = weaker(a.get("start_precision"), a.get("end_precision"))
    pb = weaker(b.get("start_precision"), b.get("end_precision"))
    p = weaker(ps, pb)
    if p is None:
        return False
    return can_judge_simultaneous(p, p)


def _overlaps(a: dict, b: dict) -> bool:
    """区间是否重叠（半开区间按 <= 判：边界相接也算同时持有，date 档尤甚）。"""
    a_s, a_e = _interval_of(a)
    b_s, b_e = _interval_of(b)
    # 空端点按无穷处理：字符串比较下用极小/极大哨兵
    a_s_k = a_s or "0000"
    a_e_k = a_e or "9999"
    b_s_k = b_s or "0000"
    b_e_k = b_e or "9999"
    return a_s_k <= b_e_k and b_s_k <= a_e_k


def hold_conflicts(steps: list[dict]) -> tuple[list[dict], list[dict]]:
    """检出"同一物品同时被多方持有"。

    返回 ``(conflicts, unknown_overlaps)``：
    - conflicts：可判定且真重叠——这是**信号**，不是噪声，绝不过滤；
    - unknown_overlaps：精度不足、判不出来——如实列出，**不得并入 conflicts
      也不得丢弃**。把它并入前者是伪精确，丢了会让正兵读成"确认无重叠"。
    """
    conflicts: list[dict] = []
    unknown: list[dict] = []
    if not isinstance(steps, list):
        return conflicts, unknown
    ordered = sorted(steps, key=lambda s: (int(s.get("order") or 0), str(s.get("id"))))
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if not _comparable(a, b):
                unknown.append({
                    "a": a.get("id"),
                    "b": b.get("id"),
                    "reason": "精度不足，无法判定是否同时持有",
                })
                continue
            if _overlaps(a, b):
                conflicts.append({
                    "a": a.get("id"),
                    "b": b.get("id"),
                    "kind": "overlap",
                    "reason": "同一时段被多方持有",
                })
    return conflicts, unknown


def out_of_order_pairs(steps: list[dict]) -> list[dict]:
    """登记顺序与登记时间矛盾：后一手起点早于前一手起点。

    只标记不纠正——这是虚假登记的线索本身。
    """
    out: list[dict] = []
    ordered = sorted(steps, key=lambda s: (int(s.get("order") or 0), str(s.get("id"))))
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        if a.get("unknownRange") or b.get("unknownRange"):
            continue
        a_s = a.get("start") or "0000"
        b_s = b.get("start") or "0000"
        if b_s < a_s:
            out.append({
                "later": b.get("id"),
                "earlier": a.get("id"),
                "reason": "后一手起始时间早于前一手",
            })
    return out


def build_hold_chain(records: list[dict], *, item_ref: str = "") -> dict:
    """组装一条物品的持有链，输出前端 ``buildChainLayout`` 可直接消费的形态。

    全部环节时间皆不可判定时 ``flat=True`` 并给出 reason——那时画成图只剩
    一排互不相连的盒子，**列表比图清楚**，这是刻意选择，不是降级。
    """
    rows = records if isinstance(records, list) else []
    steps = [normalize_hold(r, seq=i) for i, r in enumerate(rows)
             if isinstance(r, dict)]
    # 红线 2：按登记顺序，绝不按时间重排
    steps.sort(key=lambda s: (int(s.get("order") or 0), str(s.get("id"))))

    conflicts, unknown = hold_conflicts(steps)
    ooo = out_of_order_pairs(steps)

    flat = False
    reason = ""
    if not steps:
        flat, reason = True, "无持有记录"
    elif all(s.get("unknownRange") for s in steps):
        flat = True
        reason = "全部持有环节时间不可判定，无法排出链序"

    return {
        "item_ref": item_ref,
        "steps": steps,
        "conflicts": conflicts,
        "unknown_overlaps": unknown,
        "out_of_order": ooo,
        "flat": flat,
        "reason": reason,
    }
