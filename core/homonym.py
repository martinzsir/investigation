"""
core/homonym.py
同名异人检测（REQ：实体对齐消歧）。

## 背景：为什么需要它

代理键按内容哈希生成（person_<sha1(name)[:12]>），同名必然同键。这带来
两个后果：

1. obj_person 表内**永远不会出现重复 name**——重名在进入表之前就被哈希
   合并了。所以"表内去重"不是问题。
2. **两个不同的人叫同一个名字，会被静默当成一个人。** 表内重复至少看得见，
   哈希合并完全无声。这比重复危险得多。

## 消歧时机

必须在**哈希之前**完成（entity_mapping 归并阶段），不能在 _proxy_keys 里
加序号——那是 REQ-004 增量重建明确要避免的（旧按序号分配会在插入新名字时
级联改键）。

## 三类同名，本模块只处理第三类

- 同人异名（张卫国(财政局) → 张卫国）  ← entity_mapping 已覆盖
- 同名异类型（张卫国在 obj_person 与 obj_account）← _probe_type 已覆盖
- **同名异人（两个张卫国）**            ← 本模块

## 红线

- **不静默决定。** 证据不足一律 pending，交人工。系统替正兵决定"这个张卫国
  是哪个张卫国"，后面所有联动都建立在可能错误的地基上。
- **不改写语义层。** 只出诊断与 review 候选；accept 后才经既有 review_loop
  落 entity_mapping，本模块不直接改任何 obj_*/lnk_*。
- **不写死本体。** 表名/列名按存在性探测，缺失即跳过该路证据（不报错）。

## 证据强度

强 → 弱：身份证号 > 空间互斥 > 账户互斥。
"""
from __future__ import annotations

import json
import math
from typing import Any

# 关系/身份后缀：剥离后与原名不同者是「关系型指代」（张卫国配偶），
# 既不是同一人也不是同名异人，必须排除——否则每次都会跳进 review 刷屏。
_RELATIONAL_SUFFIXES = (
    "配偶", "妻弟", "妻妹", "之子", "之女", "父亲", "母亲",
    "兄弟", "姐姐", "妹妹", "哥哥", "弟弟", "之妻", "之夫",
)

# 空间互斥判定阈值：同一时刻出现在相距超过此值的两地，必为两人。
# 10 km 远大于单人 1 小时内的合理位移（步行 5km/h、驾车市区 60km/h 但受
# 轨迹采样间隔约束）；同时远小于跨区县误并的量级。
_SPATIAL_GAP_M = 10_000.0

# 同日互斥的弱判定阈值（date 档无时刻时用，阈值放宽）
_SPATIAL_GAP_M_DATE_ONLY = 30_000.0

# 单人位移的合理速度上限（km/h）。超出即在给定时间差内物理不可达。
# 120 已高于市区驾车实际水平，取保守值以免把正常通勤误判为互斥。
_MAX_SPEED_KMH = 120.0


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """球面距离（米）。与 core/geo._haversine_m 同算法，此处独立实现以免
    形成 core.geo ↔ core.homonym 的循环依赖。"""
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def strip_relational(name: str) -> tuple[str, bool]:
    """剥离关系后缀。返回 (核心名, 是否为关系型指代)。

    strip_relational("张卫国")      → ("张卫国", False)
    strip_relational("张卫国配偶")  → ("张卫国", True)
    """
    s = str(name or "").strip()
    for suf in sorted(_RELATIONAL_SUFFIXES, key=len, reverse=True):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)], True
    return s, False


def _table_exists(conn, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [name]).fetchone() is not None
    except Exception:
        return False


def _cols(conn, table: str) -> set[str]:
    try:
        return {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}
    except Exception:
        return set()


# ----------------------------------------------------------------------
# 证据采集
# ----------------------------------------------------------------------
def _evidence_id_card(conn, name: str) -> dict | None:
    """身份证号证据：同名但证号不同 = 铁证（最强）。

    obj_person_identity 当前可能为空表——有 schema 无数据，此时返回 None
    （证据不可用），不是"无冲突"。
    """
    if not _table_exists(conn, "obj_person_identity"):
        return None
    c = _cols(conn, "obj_person_identity")
    if not {"raw_name", "id_card"} <= c:
        return None
    try:
        rows = conn.execute(
            "SELECT id_card FROM obj_person_identity "
            "WHERE raw_name = ? AND id_card IS NOT NULL AND id_card <> ''",
            [name]).fetchall()
    except Exception:
        return None
    cards = sorted({str(r[0]).strip() for r in rows})
    if not cards:
        return None                      # 无身份数据：证据不可用
    return {
        "kind": "id_card",
        "strength": "strong",
        "distinct_count": len(cards),
        "detail": f"该姓名下存在 {len(cards)} 个不同身份证号",
        "evidence_available": True,
    }


def _evidence_spatial(conn, name: str,
                      gap_m: float = _SPATIAL_GAP_M) -> dict | None:
    """空间互斥证据：同一时刻出现在相距过远的两地 → 必为两人。

    本项目特有的强信号——轨迹带时刻后可直接判定，无需额外采集。
    坐标若为区划质心，同区任意两址距离恒为 0，此时该证据天然失效
    （不会误报），但也不会给出结论。
    """
    if not (_table_exists(conn, "obj_trackpoint")
            and _table_exists(conn, "obj_location")):
        return None
    tc, lc = _cols(conn, "obj_trackpoint"), _cols(conn, "obj_location")
    # 轨迹的地点列在语义层叫 location（绑定自源表「地点」），历史/测试装置可能
    # 用 std_address——两边列名不一致会让前置检查恒失败、证据永远返回 None
    # （失效不可见：单测用 std_address 全绿，真实库却是 location）。此处自适应。
    t_loc = next((c for c in ("location", "std_address") if c in tc), None)
    l_loc = next((c for c in ("std_address", "location") if c in lc), None)
    if t_loc is None or l_loc is None:
        return None
    need_t = {"person_raw", "date"}
    if not need_t <= tc or not {"lat", "lng"} <= lc:
        return None

    has_ts = "timestamp" in tc
    sel_ts = ", t.timestamp" if has_ts else ", NULL"
    try:
        rows = conn.execute(
            f"""
            SELECT t.date{sel_ts}, l.lat, l.lng
            FROM obj_trackpoint t
            JOIN obj_location l ON t.{t_loc} = l.{l_loc}
            WHERE t.person_raw = ? AND l.lat IS NOT NULL AND l.lng IS NOT NULL
            """, [name]).fetchall()
    except Exception:
        return None
    if len(rows) < 2:
        return None

    # 互斥判据不是"同一时刻在两地"（采样不可能恰好同时），而是
    # **在该时间差内物理上无法到达**：
    #     dist > max_speed × Δt  且  dist > gap_m  → 不可能是同一人
    # date 档无时刻时 Δt 取 0，退化为纯距离阈值（放宽到日粒度阈值）。
    pts: list[tuple[Any, float, float]] = []
    real_ts = 0                      # 有真实时刻值的点数
    for r in rows:
        d, ts, lat, lng = r[0], r[1], float(r[2]), float(r[3])
        if has_ts and ts is not None:
            real_ts += 1
            tkey = ts
        else:
            tkey = d
        pts.append((tkey, lat, lng))
    # 列存在但值全空（当前真实数据即如此）时按 date 档处理——
    # 否则 date 相减无 total_seconds 会退化为 dt=0，被误判成"同一时刻"。
    timed = has_ts and real_ts > 0

    gap_m = gap_m if has_ts else max(gap_m, _SPATIAL_GAP_M_DATE_ONLY)

    def _hours(a, b) -> float:
        """两时间点的小时差；date 档返回 0.0（不可测）。"""
        if not timed:
            return 0.0
        try:
            # datetime.datetime 实例本身没有 total_seconds（只有 timedelta 有），
            # 所以直接相减再取——date 相减也得 timedelta（同日为 0）。
            delta = (a - b) if a >= b else (b - a)
            return abs(delta.total_seconds()) / 3600.0
        except Exception:
            return 0.0

    worst = None      # (超出比, 距离m, 所需速度kmh, 时刻)
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            t1, la1, ln1 = pts[i]
            t2, la2, ln2 = pts[j]
            dist = _haversine_m((la1, ln1), (la2, ln2))
            if dist <= 0.0:
                continue                     # 坐标重合（质心）不构成证据
            dt = _hours(t1, t2)
            reachable_km = _MAX_SPEED_KMH * dt      # dt=0 → 0，纯距离判据生效
            if dist / 1000.0 > reachable_km and dist > gap_m:
                need = (dist / 1000.0 / dt) if dt > 0 else float("inf")
                ratio = (dist / 1000.0) / reachable_km if reachable_km > 0 else float("inf")
                cand = (ratio, dist, need, t1, t2, dt)
                if worst is None or cand[0] > worst[0]:
                    worst = cand

    if worst is None:
        return None          # 无互斥点对：证据不可用（不是"无冲突"）

    ratio, dist, need, t1, t2, dt = worst
    same_moment = bool(timed and dt == 0.0)
    if dt > 0:
        detail = (f"{dt * 60:.0f} 分钟内位移 {dist / 1000:.1f} km，"
                  f"需 {need:.0f} km/h，超出合理上限 {_MAX_SPEED_KMH} km/h")
        gran = "minute"
        strength = "strong"
    elif same_moment:
        detail = (f"同一时刻出现在相距 {dist / 1000:.1f} km 的两地"
                  f"（物理上不可能）")
        gran = "minute"
        strength = "strong"
    else:
        # 无时刻数据：只能按日粒度判。同一日跨区县移动是常态，
        # 所以这一档是**弱证据**，不足以单独定 distinct。
        detail = (f"同一日出现在相距 {dist / 1000:.1f} km 的两地"
                  f"（无时刻数据，按日粒度阈值判定，属弱证据）")
        gran = "date"
        strength = "weak"
    return {
        "kind": "spatial",
        "strength": strength,
        "max_gap_m": round(dist, 1),
        "threshold_m": gap_m,
        "required_speed_kmh": None if need == float("inf") else round(need, 1),
        "max_speed_kmh": _MAX_SPEED_KMH,
        "at": f"{t1} ↔ {t2}",
        "time_granularity": gran,
        "timed_points": real_ts,
        "detail": detail,
        "evidence_available": True,
    }


def _evidence_account(conn, name: str) -> dict | None:
    """账户互斥证据：同名关联到完全不同且互斥的账户集合。

    强度最弱——账户不同也可能是同一人开了多个户，所以只作辅助，
    单独命中不足以定 distinct。
    """
    if not _table_exists(conn, "obj_account"):
        return None
    c = _cols(conn, "obj_account")
    if not {"raw_name", "account_id"} <= c:
        return None
    try:
        rows = conn.execute(
            "SELECT account_id, source_rows FROM obj_account WHERE raw_name = ?",
            [name]).fetchall()
    except Exception:
        return None
    if len(rows) < 2:
        return None
    # 账户来源行完全不重叠 → 互斥
    srcs = []
    for aid, sr in rows:
        try:
            srcs.append(set(json.loads(sr or "[]")))
        except Exception:
            srcs.append(set())
    disjoint = all(not (srcs[i] & srcs[j])
                   for i in range(len(srcs)) for j in range(i + 1, len(srcs)))
    return {
        "kind": "account",
        "strength": "weak",
        "account_count": len(rows),
        "sources_disjoint": bool(disjoint),
        "detail": f"该姓名下有 {len(rows)} 个账户"
                  + ("，来源行完全不重叠" if disjoint else "，来源行有重叠"),
        "evidence_available": True,
    }


# ----------------------------------------------------------------------
# 主检测
# ----------------------------------------------------------------------
def detect_homonyms(conn, *, names: list[str] | None = None,
                    spatial_gap_m: float | None = None) -> dict:
    """同名异人检测。

    返回结构：
      groups: 每个同名面值一组，含证据链与裁决（distinct/same/pending）
      diagnostics: 证据不可用等说明（不静默）

    裁决规则：
      distinct  — 至少一条 strong 证据且明确指向不同实体
      same      — 有证据且明确指向同一实体（如证号唯一）
      pending   — 证据不足/矛盾/仅 weak → 交人工
    """
    gap = float(spatial_gap_m) if spatial_gap_m else _SPATIAL_GAP_M

    if names is None:
        names = _collect_person_names(conn)

    groups, diagnostics = [], []
    for name in sorted(set(names)):
        core, is_relational = strip_relational(name)
        if is_relational:
            # 关系型指代：不是同名异人候选，显式排除并记录一次
            diagnostics.append({
                "name": name, "kind": "relational_reference",
                "note": f"「{name}」是关系型指代（核心名「{core}」），"
                        f"既非同一人也非同名异人，不参与消歧",
            })
            continue

        ev = []
        for fn, kw in ((_evidence_id_card, {}),
                       (_evidence_spatial, {"gap_m": gap}),
                       (_evidence_account, {})):
            try:
                e = fn(conn, name, **kw)
            except Exception as ex:                      # 证据采集失败不致命
                diagnostics.append({
                    "name": name, "kind": f"{fn.__name__}_failed",
                    "note": f"{str(ex)[:120]}",
                })
                continue
            if e:
                ev.append(e)

        if not ev:
            # 无任何可用证据 → 不做裁决，维持现有合并行为，但如实说明
            diagnostics.append({
                "name": name, "kind": "no_evidence",
                "note": "无可用消歧证据（身份表为空/轨迹不足/单账户），"
                        "维持现有按名合并；不静默分列",
            })
            continue

        strong = [e for e in ev if e.get("strength") == "strong"]
        idc = next((e for e in ev if e["kind"] == "id_card"), None)
        sp = next((e for e in ev if e["kind"] == "spatial"), None)

        if idc and idc.get("distinct_count", 0) > 1:
            verdict = "distinct"
            reason = "身份证号互斥"
        elif idc and idc.get("distinct_count") == 1:
            verdict = "same"
            reason = "身份证号唯一"
        elif sp and sp.get("strength") == "strong":
            verdict = "distinct"
            reason = (f"空间互斥（{sp['max_gap_m'] / 1000:.1f} km > "
                      f"{sp['threshold_m'] / 1000:.1f} km）")
        elif strong:
            verdict = "pending"
            reason = "存在强证据但不足以定论，需人工确认"
        else:
            verdict = "pending"
            reason = "仅有弱证据（账户互斥），不足以分列，需人工确认"

        groups.append({
            "name": name,
            "entity_type": "person",
            "evidence": ev,
            "strong_count": len(strong),
            "verdict": verdict,
            "reason": reason,
            "note": ("消歧须在哈希前完成；accept 后经 review_loop 落 "
                     "entity_mapping 分列，本模块不直接改写语义层"),
        })

    return {
        "groups": groups,
        "diagnostics": diagnostics,
        "summary": {
            "names_scanned": len(set(names)),
            "groups": len(groups),
            "distinct": sum(1 for g in groups if g["verdict"] == "distinct"),
            "same": sum(1 for g in groups if g["verdict"] == "same"),
            "pending": sum(1 for g in groups if g["verdict"] == "pending"),
            "excluded_relational": sum(
                1 for d in diagnostics if d["kind"] == "relational_reference"),
            "no_evidence": sum(
                1 for d in diagnostics if d["kind"] == "no_evidence"),
        },
    }


def _collect_person_names(conn) -> list[str]:
    """收集应参与人名消歧的姓名面值（形态判 person，排除关系型指代由上层做）。"""
    out: list[str] = []
    try:
        from core.entity import is_person_name
    except Exception:
        is_person_name = lambda n: bool(n) and 2 <= len(str(n)) <= 4

    seen = set()
    for tbl, col in (("obj_person", "raw_name"),
                     ("obj_trackpoint", "person_raw"),
                     ("obj_account", "raw_name")):
        if not _table_exists(conn, tbl):
            continue
        if col not in _cols(conn, tbl):
            continue
        try:
            for (v,) in conn.execute(
                    f"SELECT DISTINCT {col} FROM {tbl} "
                    f"WHERE {col} IS NOT NULL AND {col} <> ''").fetchall():
                s = str(v).strip()
                if s in seen:
                    continue
                seen.add(s)
                if is_person_name(s):
                    out.append(s)
        except Exception:
            continue
    return out


def to_review_candidates(result: dict) -> list[dict]:
    """把 pending 组转成 review 候选（复用既有 review 队列，不新建）。

    distinct/same 也不自动落表——本模块只出建议，落表走 review_loop.accept。
    """
    cands = []
    for g in result.get("groups", []):
        if g["verdict"] != "pending":
            continue
        cands.append({
            "candidate_id": f"homonym:{g['name']}",
            "entity_type": "person",
            "canonical": g["name"],
            "variants": [g["name"]],
            "confidence": 0.5,
            "kind": "homonym",
            "evidence": g["evidence"],
            "reason": g["reason"],
            "note": "同名异人待确认：确认分列则 accept 并给出各自归属行，"
                    "确认同一人则 reject",
        })
    return cands
