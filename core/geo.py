"""
core/geo.py
REQ-G-021 地点标准化与空间匹配（只读、离线、无大模型/无网络依赖）。

背景：行为维度的"同框/地点重合"原先用字符串精确相等（t1.location = t2.location），
交警录入的「滨江路中段 K3+200」「某交叉口东侧 50 米」与招投标档案的「滨江路」必然不相等，
碰撞从设计上就不可能命中。本模块提供：
  1) 地点标准化：去 K 桩号（K12+300）、方位/距离修饰（东侧 50 米）、路段段次（中段/交叉口/
     路口/附近…），提取路名主干（含「A路与B路」交叉口）。
  2) 同框判定 locations_colocated：优先路名主干判同；注入了 geocoder 时按 haversine
     距离阈值判定。**无坐标/无法解析不报错**，返回 degraded 降级标注（REQ-G-021 AC4）。

红线/边界：
  - 纯函数、只读、离线；geocoder 默认为 None（内核无网络），可经 set_geocoder 注入。
  - 机器不做过度推断：异名地点在无坐标时不擅自判同（保守不误判，AC2）。
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import date
from typing import Any, Callable, Optional

# 路名主干后缀（按长到短匹配，避免"大道"被"道"截断）
_ROAD_SUFFIX = r"(?:高架桥|立交桥|快速路|环城路|大道|公路|高速|大街|道路|路|街|道|巷|弄|环)"
_ROAD_TOKEN = re.compile(
    r"[0-9A-Za-z\u4e00-\u9fa5]{1,14}?" + _ROAD_SUFFIX)

# 需剥离的修饰（在提取路名前去除）
_K_MARKER = re.compile(r"[KkＫｋ]\s*\d+\s*[\+＋]\s*\d+|[KkＫｋ]\s*\d+")
_DISTANCE = re.compile(
    r"(?:东|西|南|北|东南|东北|西南|西北)?\s*(?:侧|边|旁|向)?\s*\d+(?:\.\d+)?\s*(?:米|m|M|公里|km|KM|千米)")
_DOOR = re.compile(r"\d+\s*号(?:楼|栋|单元)?")
_SEGMENT = re.compile(
    r"(交叉口|十字路口|交口|路口|环岛|转盘|中段|东段|西段|南段|北段|段|"
    r"附近|周边|旁边|旁|对面|门口|门前|边上|往东|往西|往南|往北|"
    r"东侧|西侧|南侧|北侧|东面|西面|南面|北面)")
_EXTRA = re.compile(r"[\s，,。.、;；()（）]+")
# 交叉口连接词（提取路名前替换为分隔，提取后统一用"与"重连，避免"与与"）
_CONNECTOR = re.compile(r"[与和及]")

# 可注入的地理编码器：text -> (lat, lng) 或 None。离线默认 None。
Geocoder = Callable[[str], Optional[tuple[float, float]]]
_GEOCODER: Geocoder | None = None


def set_geocoder(fn: Geocoder | None) -> None:
    """注入地理编码器（离线内核默认无；测试可注入桩）。传 None 清除。"""
    global _GEOCODER
    _GEOCODER = fn


def normalize_location(text: str | None) -> str:
    """把自由文本地点归一为路名主干；无法解析（无路名）返回空串。

    例："滨江路中段 K3+200" → "滨江路"；
        "中山路与解放路交叉口东侧50米" → "中山路与解放路"。
    """
    if not text or not isinstance(text, str):
        return ""
    s = text.strip()
    s = _K_MARKER.sub(" ", s)
    s = _DISTANCE.sub(" ", s)
    s = _DOOR.sub(" ", s)
    s = _SEGMENT.sub(" ", s)
    s = _CONNECTOR.sub(" ", s)
    s = _EXTRA.sub(" ", s)
    tokens = _ROAD_TOKEN.findall(s)
    if not tokens:
        return ""
    # 保序去重（去掉可能残留的连接词起首）
    seen: list[str] = []
    for t in tokens:
        t = t.lstrip("与和及")
        if t and t not in seen:
            seen.append(t)
    return "与".join(seen)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def locations_colocated(loc_a: str | None, loc_b: str | None,
                        radius_m: int = 200) -> dict:
    """判定两个自由文本地点是否同框。

    返回 dict：{colocated, method, reason, normalized_a, normalized_b,
               distance_m, degraded, degraded_reason}
    method: "geocode"（坐标距离）| "name_match"（路名主干相同）|
            "degraded"（无法判定）。无坐标/无法解析一律不抛错。
    """
    na, nb = normalize_location(loc_a), normalize_location(loc_b)
    base = {"normalized_a": na, "normalized_b": nb, "distance_m": None,
            "degraded": False, "degraded_reason": None}
    if not na or not nb:
        return {**base, "colocated": False, "method": "degraded",
                "reason": "地点无法解析为路名主干，且无坐标可用",
                "degraded": True,
                "degraded_reason": "location_unparseable"}
    # 路名主干相同 → 判同（确定性，无需坐标）
    if na == nb:
        return {**base, "colocated": True, "method": "name_match",
                "reason": f"路名主干相同：{na}"}
    # 主干不同：有坐标则按距离，无坐标则保守不判同并降级标注
    if _GEOCODER is not None:
        try:
            ca, cb = _GEOCODER(loc_a or ""), _GEOCODER(loc_b or "")
        except Exception:
            ca = cb = None
        if ca and cb:
            dist = _haversine_m(ca, cb)
            return {**base, "distance_m": round(dist, 1),
                    "colocated": dist <= radius_m, "method": "geocode",
                    "reason": f"坐标距离 {dist:.1f}m ≤ 阈值 {radius_m}m"
                    if dist <= radius_m else f"坐标距离 {dist:.1f}m > 阈值 {radius_m}m"}
    return {**base, "colocated": False, "method": "degraded",
            "reason": f"路名主干不同（{na} vs {nb}）且无地理编码，"
                      f"无法判定空间邻近，保守不判同",
            "degraded": True, "degraded_reason": "geocoder_unavailable"}


# ----------------------------------------------------------------------
# Ontology py Function 入口：fn(store, merged_params)
# loc_a/loc_b 为自由文本（py 路径不走 SQL 模板 enum 校验；可经编排/MCP/测试传入）。
# radius_m 在 functions.json 声明为 integer 参数（规则可挂钩调阈值）。
# ----------------------------------------------------------------------
def location_colocated(store: Any, params: dict) -> dict:
    loc_a = params.get("loc_a")
    loc_b = params.get("loc_b")
    radius = params.get("radius_m", 200)
    try:
        radius = int(radius)
    except (TypeError, ValueError):
        radius = 200
    if not loc_a or not loc_b:
        return {"hit": False, "colocated": False, "method": "degraded",
                "degraded": True,
                "degraded_reason": "missing_loc_args",
                "reason": "未提供 loc_a/loc_b 待比对地点（规则挂钩时应由编排层传入或改表内匹配）",
                "normalized_a": normalize_location(loc_a),
                "normalized_b": normalize_location(loc_b)}
    return locations_colocated(loc_a, loc_b, radius)


# ----------------------------------------------------------------------
# P1 行政区划离线匹配（AdminMatcher）：从 admin_ref.parquet 加载五级区划，
# 对自由文本做上下文约束匹配（先省级，再地级，再县级，再乡镇级）。
# 输出 {province, prefecture, county, township, admin_code, confidence, match_method}。
# 双段归并键 = admin_path + 路名主干（防同名"滨江路"跨区县误并）。
# ----------------------------------------------------------------------
_ADMIN_REF_PATH = "data/ref/admin_ref.parquet"
_ADMIN_CACHE: list[dict] | None = None

# 匹配级别置信度
_CONFIDENCE = {
    "province": 0.3,
    "prefecture": 0.5,
    "county": 0.7,
    "township": 0.9,
}


def _load_admin_ref() -> list[dict]:
    """加载 admin_ref.parquet 为内存列表（惰性加载，缓存复用）。"""
    global _ADMIN_CACHE
    if _ADMIN_CACHE is not None:
        return _ADMIN_CACHE
    import pandas as pd
    from pathlib import Path
    path = Path(_ADMIN_REF_PATH)
    if not path.exists():
        _ADMIN_CACHE = []
        return _ADMIN_CACHE
    df = pd.read_parquet(path)
    # 按层级排序：province < prefecture < county < township（匹配顺序）
    level_order = {"province": 0, "prefecture": 1, "county": 2, "township": 3}
    df["_level_order"] = df["level"].map(level_order)
    df = df.sort_values("_level_order").reset_index(drop=True)
    _ADMIN_CACHE = df.to_dict("records")
    return _ADMIN_CACHE


def _finite_or_none(v: Any) -> float | None:
    """数值质心归一：None/NaN/Inf 一律 None（防 NaN 写库后 IS NOT NULL 计真）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _match_admin_level(text: str, level: str, context: dict | None = None) -> dict | None:
    """在指定层级匹配，返回第一个命中的记录（带上下文过滤）。"""
    rows = _load_admin_ref()
    for row in rows:
        if row["level"] != level:
            continue
        name = row["name"]
        core = row.get("core_name", "")
        # 全名匹配（含后缀，如"房县""城东区""交通街道"）始终允许。
        # 裸核心名限制：
        #  - 长度须 >= 2：单字核心名（全国 221 个县级单字核心，如
        #    房/化/通/城）会与普通汉语词碰撞——"安置房"含"房"→房县、
        #    "绿化"含"化"→化隆——制造跨省假命中；
        #  - 乡镇级不允许裸核心名：街道核心名大量来自通用词
        #    （交通街道/建设街道/人民街道…），"智慧交通"含"交通"即误挂
        #    东胜区交通街道且置信度高达 0.9；须出现全名（带街/镇/乡后缀）
        #    才算命中。
        bare_core_ok = bool(core and len(core) >= 2 and level != "township"
                           and core in text)
        if name in text or bare_core_ok:
            # 上下文约束：若已确定上级，需匹配
            if context:
                if level == "prefecture" and context.get("province"):
                    if row["province"] != context["province"]:
                        continue
                elif level == "county" and context.get("prefecture"):
                    if row["prefecture"] != context["prefecture"]:
                        continue
                elif level == "township" and context.get("county"):
                    if row["county"] != context["county"]:
                        continue
            return {
                "name": name,
                "code": row["code"],
                "province": row.get("province", ""),
                "prefecture": row.get("prefecture", ""),
                "county": row.get("county", ""),
                "township": row.get("township", ""),
                "full_path": row.get("full_path", ""),
                # admin_ref 部分街道行缺质心（NaN）；NaN 会穿透
                # `is not None` 守卫写进 DuckDB 且 `IS NOT NULL` 仍计真，
                # 污染坐标覆盖率，统一在源头归一为 None。
                "lng": _finite_or_none(row.get("lng")),
                "lat": _finite_or_none(row.get("lat")),
                "match_method": f"exact_{level}",
                "confidence": _CONFIDENCE.get(level, 0.5),
            }
    return None


def parse_admin_path(text: str | None) -> dict:
    """解析自由文本中的行政区划路径，返回最深层级匹配结果。

    匹配策略：上下文约束链式匹配（省→地→县→乡），逐级缩小范围。
    无匹配返回空 dict。离线确定性，不抛错。
    """
    if not text or not isinstance(text, str):
        return {}
    text = text.strip()
    if not text:
        return {}

    result: dict = {}
    context: dict = {}

    # 省级
    m = _match_admin_level(text, "province", None)
    if m:
        result["province"] = m["name"]
        result["province_code"] = m["code"]
        context["province"] = m["name"]

    # 地级（上下文约束）
    m = _match_admin_level(text, "prefecture", context)
    if m:
        result["prefecture"] = m["name"]
        result["prefecture_code"] = m["code"]
        context["prefecture"] = m["name"]

    # 县级
    m = _match_admin_level(text, "county", context)
    if m:
        result["county"] = m["name"]
        result["county_code"] = m["code"]
        result["admin_code"] = m["code"]  # 最深层级 code
        result["confidence"] = m["confidence"]
        result["match_method"] = m["match_method"]
        result["lng"] = m.get("lng")
        result["lat"] = m.get("lat")
        context["county"] = m["name"]

    # 乡镇级
    m = _match_admin_level(text, "township", context)
    if m:
        result["township"] = m["name"]
        result["township_code"] = m["code"]
        result["admin_code"] = m["code"]  # 更新为最深层级
        result["confidence"] = m["confidence"]
        result["match_method"] = m["match_method"]
        result["lng"] = m.get("lng") if m.get("lng") is not None else result.get("lng")
        result["lat"] = m.get("lat") if m.get("lat") is not None else result.get("lat")

    return result


def dual_segment_key(text: str | None) -> str:
    """双段归并键 = admin_path + 路名主干。

    例：「滨江路中段」→「滨江路」（无 admin）→ key="滨江路"
        「北京市朝阳区滨江路」→ admin="北京市/朝阳区" + 路名"滨江路" → key="北京市/朝阳区/滨江路"
    跨区县同名道路自动隔离。
    """
    admin = parse_admin_path(text)
    road = normalize_location(text)

    parts = []
    for level in ["province", "prefecture", "county", "township"]:
        if admin.get(level):
            parts.append(admin[level])
    if road:
        parts.append(road)
    return "/".join(parts) if parts else (text or "")


# ======================================================================
# P2 空间研判 Function（PLAN-GEO-001 §4）：
#   geo_subject_sites       主体落脚点集（trackpoint × location 维度归并）
#   geo_co_located_radius   距离阈值同框（坐标轨 + 同县区划文本轨降级）
#   geo_buffer_scan         缓冲区环带扫描（内环低发带 / 外环排查带）
#   geo_profile_cgt         Rossmo CGT 概率面（确定性分段函数）
#
# 坐标统一 GCJ-02；target 等自由文本由编排层透传（不进 functions.json
# parameters —— string 必须 enum 白名单），数值阈值走声明参数。
# 输出只给「优先排查区域」候选事实，不下定性结论（红线 3）。
# ======================================================================

_DEGRADE_EXC_NAMES = ("CatalogException", "BinderException")


def _is_semantic_gap(exc: BaseException) -> bool:
    """结构降级判据：Catalog/Binder 且引用 obj_*/lnk_* 语义表（数据源未接入/
    schema 不符）。与 core.functions._is_structural_degrade 同口径；geo.py 不
    import core.functions（防循环），用异常类名判定，避免引入 duckdb 依赖。"""
    if type(exc).__name__ not in _DEGRADE_EXC_NAMES:
        return False
    return bool(re.search(r"\b(?:obj_|lnk_)[a-z_]+", str(exc)))


def _tbl(ctx, object_name: str) -> str:
    return ctx.table(object_name) if ctx is not None else f"obj_{object_name}"


def _lnk(ctx, link_name: str) -> str:
    return ctx.link(link_name) if ctx is not None else f"lnk_{link_name}"


def grid_key(lat, lng, grid_meters: int = 200):
    """把坐标吸附到 ~grid_meters 方形网格，返回 (row, col)；坐标缺失返回 None。

    米→度换算：1°lat ≈ 111_320m；1°lng ≈ 111_320·cos(lat)m（随纬度收敛）。
    """
    if lat is None or lng is None:
        return None
    lat, lng = float(lat), float(lng)
    if grid_meters <= 0:
        return None
    dlat = grid_meters / 111_320.0
    cos = math.cos(math.radians(lat))
    if abs(cos) < 1e-9:
        return None
    dlng = grid_meters / (111_320.0 * cos)
    return (math.floor(lat / dlat), math.floor(lng / dlng))


# ----------------------------------------------------------------------
# CGT 核函数（Rossmo 确定性分段公式，纯函数可测）
# ----------------------------------------------------------------------
def cgt_term(d_m: float, buffer_m: float, f: float = 1.0, g: float = 1.0) -> float:
    """Rossmo 分段距离衰减项。d_m 须已做防零下限钳制（调用方负责）。

    正确分段（Rossmo 1999 / Chainey & Tompson 标准引用）：
      d > B   : 1 / d^f                  （缓冲带外，随距离衰减）
      d ≤ B   : B^(g-f) / (2B-d)^g       （缓冲区内，d=0 时为 1/(2^g·B^f)，
                                            往 B 方向升到 1/B^f，形成"罪犯避开
                                            自家门口"的低洼带）
      d ≥ 2B  : 0                        （影响域外）

    错误版本曾把两支对调，导致顶格区永远贴在 d≈2B 的发散点（伪峰）。
    """
    if d_m >= 2 * buffer_m:
        return 0.0
    if d_m > buffer_m:
        return 1.0 / (d_m ** f)
    return (buffer_m ** (g - f)) / ((2 * buffer_m - d_m) ** g)


def cgt_scale_check(points: list[tuple[float, float]], buffer_m: float = 1000,
                    max_isolated_ratio: float = 0.5,
                    max_dispersion_ratio: float = 2.0) -> dict:
    """CGT 适用性自检：事件点空间尺度是否匹配 Rossmo 影响域。

    为什么需要这一步
    ----------------
    Rossmo CGT 建模的是「掠夺者型（marauder）」出行——作案人以落脚点为
    中心活动，事件点彼此落在影响域内，多点叠加才形成概率面的峰。

    当事件点两两距离都超过影响域 2B 时，每个点各自成为一个互不相干的
    小山包，概率面不是"聚集中心"而是"孤岛拼盘"。此时归一化后的
    probability=1.0 只表示"全图最高的那个孤岛顶端"，读图人极易误当成
    "空间聚集在此"。

    实测教训：某工程腐败案 5 个事件点横跨杭州 5 个区（东西 22km），
    而 buffer_m=1000 → 影响域仅 2km，每个点影响域内邻居数恒为 0，
    却照样输出了 17×30 网格、60 个优先排查区、"顶格区概率 1.0"
    的完整概率面。看起来成功，实则尺度错配。

    判据（两条，任一触发即判定不适用）
    --------------------------------
    1) 孤岛占比：影响域内无任何邻居的点占比 > max_isolated_ratio
       ——说明概率面呈孤岛式，不反映多点叠加结构。
    2) 离散比：最近邻距离中位数 / 影响域 > max_dispersion_ratio
       ——说明数据跨度与 buffer_m 参数严重错配（通勤者型或参数过小）。

    两者皆不触发时，若仍存在孤岛点，给出软提示（产出但标注），因为
    部分点孤立并不必然否定整体聚集结构。

    返回 dict：applicable / reason / 各项尺度指标 / suggested_buffer_m。
    """
    pts = sorted((float(a), float(b)) for a, b in points)
    n = len(pts)
    influence = 2.0 * float(buffer_m)
    if n < 2:
        return {"applicable": False, "reason": "事件点不足 2 个，无法评估空间尺度",
                "point_count": n, "influence_m": influence}

    # 最近邻距离（每个点到其余点的最小距离；n=2 时两点互为最近邻）
    nn_list = []
    for i, (la, ln) in enumerate(pts):
        ds = [_haversine_m((la, ln), (b, c))
              for j, (b, c) in enumerate(pts) if j != i]
        if ds:
            nn_list.append(min(ds))
    nn_median = statistics.median(nn_list)
    nn_max = max(nn_list)

    # 孤岛点：影响域内没有任何邻居
    isolated = sum(1 for d in nn_list if d >= influence)
    isolated_ratio = isolated / n

    # 外接框对角线跨度
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    span = _haversine_m((min(lats), min(lngs)), (max(lats), max(lngs)))

    dispersion_ratio = (nn_median / influence) if influence > 0 else float("inf")

    # 自适应建议：让**最远**的那个点也落入影响域（2B ≥ nn_max → B ≥ nn_max/2）
    # 按 nn_median 取会让离群点仍是孤岛，isolated_ratio 仍可能超标；
    # 取 nn_max 才能一次性全覆盖（clamp 到 buffer_m 允许区间）。
    suggested = None
    if dispersion_ratio > 1.0 or isolated > 0:
        suggested = int(min(50000, max(100, math.ceil(nn_max / 2.0))))

    check = {
        "applicable": True,
        "reason": None,
        "point_count": n,
        "influence_m": round(influence, 1),
        "nn_median_m": round(nn_median, 1),
        "nn_max_m": round(nn_max, 1),
        "span_m": round(span, 1),
        "isolated_count": isolated,
        "isolated_ratio": round(isolated_ratio, 4),
        "dispersion_ratio": round(dispersion_ratio, 4),
        "suggested_buffer_m": suggested,
    }

    if isolated_ratio > max_isolated_ratio:
        check["applicable"] = False
        check["reason"] = (
            f"事件点空间离散：{isolated}/{n} 个点在影响域（2B={influence:.0f}m）"
            f"内无任何邻居，概率面将呈孤岛式而非多点叠加，不反映聚集中心"
            f"（最近邻距离中位数 {nn_median:.0f}m，跨度 {span:.0f}m）")
        return check
    if dispersion_ratio > max_dispersion_ratio:
        check["applicable"] = False
        check["reason"] = (
            f"数据跨度与参数错配：最近邻距离中位数 {nn_median:.0f}m 达影响域"
            f"（{influence:.0f}m）的 {dispersion_ratio:.1f} 倍，事件点空间尺度远超"
            f" CGT 缓冲区设定（多见于通勤者型或活动范围跨区县）")
        return check
    if isolated > 0:
        check["reason"] = (
            f"提示：{isolated}/{n} 个事件点为空间孤岛（影响域内无邻居），"
            f"该局部未与其他点叠加")
    return check


def cgt_surface(points: list[tuple[float, float]], grid_meters: float = 200,
                buffer_m: float = 1000, f: float = 1.0, g: float = 1.0,
                max_cells: int = 1600) -> dict:
    """Rossmo CGT 概率面：事件点集 → 外接 bbox（外扩 buffer/2）网格化加权求和。

    网格数超 max_cells 时格宽倍增（确定性）；概率按最大值归一化；
    cells 按 (-probability, lat, lng) 排序——同输入严格同输出。
    返回 {grid_meters, rows, cols, cells, max_probability_raw}。
    """
    pts = sorted((float(a), float(b)) for a, b in points)
    if not pts:
        return {"grid_meters": grid_meters, "rows": 0, "cols": 0,
                "cells": [], "max_probability_raw": 0.0}
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    lat0, lat1 = min(lats), max(lats)
    lng0, lng1 = min(lngs), max(lngs)
    mid_lat = (lat0 + lat1) / 2.0
    cos = math.cos(math.radians(mid_lat))
    cos = abs(cos) if abs(cos) > 1e-9 else 1e-9
    m_lat = (buffer_m / 2.0) / 111_320.0
    m_lng = (buffer_m / 2.0) / (111_320.0 * cos)
    lat0, lat1 = lat0 - m_lat, lat1 + m_lat
    lng0, lng1 = lng0 - m_lng, lng1 + m_lng
    span_lat_m = (lat1 - lat0) * 111_320.0
    span_lng_m = (lng1 - lng0) * 111_320.0 * cos
    gm = float(grid_meters)
    while math.ceil(span_lat_m / gm) * math.ceil(span_lng_m / gm) > max_cells:
        gm *= 2.0
    rows = max(1, math.ceil(span_lat_m / gm))
    cols = max(1, math.ceil(span_lng_m / gm))
    dlat = gm / 111_320.0
    dlng = gm / (111_320.0 * cos)
    d_min = gm / 4.0  # 防除零下限（格心恰与事件点重合时）
    cells: list[dict] = []
    for r in range(rows):
        for c in range(cols):
            clat = lat0 + (r + 0.5) * dlat
            clng = lng0 + (c + 0.5) * dlng
            p = 0.0
            for (elat, elng) in pts:
                d = _haversine_m((clat, clng), (elat, elng))
                p += cgt_term(max(d, d_min), buffer_m, f, g)
            cells.append({"lat": clat, "lng": clng, "probability": p})
    maxp = max(x["probability"] for x in cells)
    if maxp > 0:
        for x in cells:
            x["probability"] = x["probability"] / maxp
    cells.sort(key=lambda x: (-x["probability"], x["lat"], x["lng"]))
    return {"grid_meters": gm, "rows": rows, "cols": cols,
            "cells": cells, "max_probability_raw": maxp}


def _cells_to_geojson(surf: dict) -> dict:
    """概率面 → GeoJSON FeatureCollection（仅保留 probability>0 的格子；
    坐标序 [lng, lat]，与事件点 GCJ-02 同系）。"""
    gm = surf["grid_meters"]
    feats = []
    for cell in surf["cells"]:
        p = cell["probability"]
        if p <= 0:
            continue
        lat, lng = cell["lat"], cell["lng"]
        cos = math.cos(math.radians(lat))
        cos = abs(cos) if abs(cos) > 1e-9 else 1e-9
        hlat = (gm / 2.0) / 111_320.0
        hlng = (gm / 2.0) / (111_320.0 * cos)
        ring = [[lng - hlng, lat - hlat], [lng + hlng, lat - hlat],
                [lng + hlng, lat + hlat], [lng - hlng, lat + hlat],
                [lng - hlng, lat - hlat]]
        feats.append({"type": "Feature",
                      "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"probability": round(p, 6)}})
    return {"type": "FeatureCollection", "features": feats}


# ----------------------------------------------------------------------
# 语义层取数公共件（trackpoint × trackpoint_at × location 三表 JOIN）
# ----------------------------------------------------------------------
from core.time_semantics import weaker as _weaker_precision

_TS_COL_CACHE: dict[str, bool] = {}


def _trackpoint_has_timestamp(store) -> bool:
    """obj_trackpoint 是否有 timestamp 列（可选列，旧库可能没有）。

    每次调用探测一次（零行查询，成本可忽略）：不缓存是因为 py 函数拿到
    的是 ReadOnlyStore 代理——代理禁止写属性（只读护栏），而用
    (db_path, id(store)) 做全局缓存键值会被 GC 后的 id 重用击穿（实测三个
    测试模块连跑时串库，SQL 报 BinderException）。正确性优先于这一次查询。
    """
    # 缓存挂在 **store 实例**上，不用 (db_path, id(store)) 全局字典。
    # 为什么：id(store) 在对象被 GC 后会被新对象重用——实测三个测试模块
    # 连跑时，带 timestamp 列的库先探测并缓存 True，随后新建的无该列的库
    # 复用了同一个 id，命中缓存 → SQL 报 BinderException。挂实例上则随
    # store 生命周期，既保留"每库只探测一次"，又不可能跨库串。
    # 探测方式必须是**查列**而不是查 information_schema：py 函数拿到的
    # store 是 ReadOnlyStore 代理，其 query 走只读白名单校验，
    # information_schema 不在白名单 → 必然抛异常 → 恒判"无该列"（实测
    # 整条时刻轨因此在真实调用路径下静默失效）。SELECT 列 + WHERE 1=0
    # 读零行、表在白名单内，列缺失时抛的 BinderException 即为判据。
    try:
        store.query("SELECT timestamp FROM obj_trackpoint WHERE 1=0")
        return True
    except Exception:
        return False


def _site_rows_sql(ctx, where: str = "", store=None) -> str:
    t_tp = _tbl(ctx, "trackpoint")
    t_ta = _lnk(ctx, "trackpoint_at")
    t_loc = _tbl(ctx, "location")
    # timestamp 为**可选**升级列：无时刻时整列 NULL，时间判定回落 date 档。
    # 列不存在（旧语义库）时投影 NULL，SQL 不硬失败。
    # store 优先于 ctx：Function 经 FunctionExecutor 调用时**只传 params、
    # 不传 ctx**（core/functions.py 的 _call_py 签名如此），此时 ctx 恒为
    # None，若只从 ctx 取 store 就会保守按"无 timestamp 列"投影 NULL——
    # 实测轨迹分段/异常检测经镜头调用因此整列落空、全部降级。函数自身
    # 手里有 store，显式传进来即可，不必依赖 ctx。
    _st = store
    if _st is None:
        _st = getattr(ctx, "store", None)
    if _st is None and isinstance(ctx, dict):
        _st = ctx.get("store")
    ts_sel = ("t.timestamp AS ts, " if _trackpoint_has_timestamp(_st)
              else "NULL AS ts, ")
    return (
        f'SELECT t.track_id, t.person_raw, CAST(t.date AS DATE) AS d, '
        f'{ts_sel}'
        f't.location AS raw_location, l.location_id, l.std_address, '
        f'l.lat, l.lng, l.coord_sys, l.province, l.prefecture, l.county, '
        f'l.township, l.admin_code, l.geocode_source, l.geocode_confidence '
        f'FROM {t_tp} t '
        f'LEFT JOIN {t_ta} ta ON ta.track_id = t.track_id '
        f'LEFT JOIN {t_loc} l ON l.location_id = ta.location_id '
        f'{where}')


def _precision_of(row: dict) -> str | None:
    """求一行的可比时间精度：显式覆盖优先，否则由 timestamp 派生、回落 date。

    关键：**时刻全零派生为 date 档**（真午夜事件由接入层显式覆盖纠正），
    避免"类型是 datetime 就当精确"→ 两事件同落 00:00:00 被判成同时。
    """
    from core.time_semantics import resolve_time_precision
    return resolve_time_precision(
        row, keys=("ts", "d"), declared=True)


def _time_pair(a: dict, b: dict, precision: str | None) -> dict:
    """按**较低精度档**给出时间关系描述；精度不足时不声称同时。

    time_span 只在 minute/second 档给出（秒级间隔），date/hour 档为 None——
    给 None 是刻意的：没有可信的时刻间隔，就不给数字（数字最容易被当成事实）。
    """
    from core.time_semantics import can_judge_simultaneous
    judgeable = can_judge_simultaneous(precision, precision)
    span = None
    if judgeable:
        ta, tb = _coerce_ts(a), _coerce_ts(b)
        if ta is not None and tb is not None:
            span = abs(int((tb - ta).total_seconds()))
    return {"time_precision": precision,
            "simultaneous_judgeable": judgeable,
            "time_span": span}


def _time_note(precision: str | None, window_days: int,
               window_minutes: int | None = None) -> str:
    """按实际可比精度写时间口径说明——**精度决定措辞**，不写死"不支持同时"。

    date/hour 档：只能判"±N 天内先后出现"（同地异时），不声称同时。
    minute/second 档：可判同一时间窗；若配置了 window_minutes 则按真实秒
    间隔过滤，措辞随之改为"已按 N 分钟窗判定"。但仍不声称"同行/伴随移动"
    ——路径概念（移动段）尚未建立，那是下一步的事。
    """
    if precision in ("minute", "second"):
        head = f"事件精确到分钟及以上（{precision} 档），时间间隔可计算"
        if window_minutes is not None:
            head += f"；已按 {window_minutes} 分钟窗判定同时间窗"
        return head + "；仍不支持「同行/伴随移动」结论（无移动段与路径概念）"
    return (f"obj_trackpoint 时间精度为 {precision or '未知'} 档，仅能判"
            f"±{window_days} 天内先后出现（同地异时），"
            "不支持同时/同行结论")


def _coerce_ts(row: dict):
    """取行的时刻值（timestamp 优先，回落 date）；无法解析 → None。"""
    from core.time_semantics import _coerce_dt
    return _coerce_dt(row.get("ts")) or _coerce_dt(row.get("d"))


def _admin_path_of(row: dict) -> str:
    parts = [row.get(k) for k in ("province", "prefecture", "county", "township")
             if row.get(k)]
    return "/".join(parts)


def _degraded_result(reason: str, **extra) -> dict:
    return {"hit": False, "degraded": True, "degraded_reason": reason, **extra}


# ----------------------------------------------------------------------
# Function 1：geo_subject_sites —— 主体落脚点集
# ----------------------------------------------------------------------
def geo_subject_sites(store, params: dict, ctx=None) -> dict:
    """target 主体的轨迹事件按 location 维度归并 → 落脚点集。

    无坐标落脚点保留并标 coord_degraded（admin_path 文本轨兜底）；
    语义表缺失 → 降级零命中，不抛错。
    """
    from core.graph import resolve_subject
    target = (params.get("target") or params.get("target_subject") or "").strip()
    if not target:
        return _degraded_result(
            "缺少 target 参数（落脚点画像须指定主体）",
            subject=None, sites=[], site_count=0, total_visits=0)
    subject = resolve_subject(store, target,
                              params.get("target_type", "auto"), ctx)
    if subject is None:
        return _degraded_result(
            f"主体 {target!r} 在语义层实体中不存在（未建档或未归一）",
            subject=None, sites=[], site_count=0, total_visits=0)
    try:
        rows = store.query(
            _site_rows_sql(ctx, "WHERE t.person_raw = ? ", store)
            + "ORDER BY d, t.track_id",
            (subject["name"],))
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            subject=subject, sites=[], site_count=0, total_visits=0)

    sites: dict[str, dict] = {}
    for r in rows:
        key = r["location_id"] or f"__raw__:{r['raw_location'] or ''}"
        s = sites.get(key)
        if s is None:
            has_coord = r["lat"] is not None and r["lng"] is not None
            s = {"location_id": r["location_id"],
                 "std_address": r["std_address"] or (r["raw_location"] or ""),
                 "admin_path": _admin_path_of(r),
                 "lat": r["lat"], "lng": r["lng"],
                 "coord_sys": r["coord_sys"],
                 "geocode_source": r["geocode_source"],
                 "geocode_confidence": r["geocode_confidence"],
                 "coord_degraded": not has_coord,
                 "visits": 0, "first_date": None, "last_date": None,
                 "event_pks": []}
            sites[key] = s
        s["visits"] += 1
        if r["track_id"]:
            s["event_pks"].append(str(r["track_id"]))
        d = r["d"].isoformat() if r["d"] else None
        if d:
            if s["first_date"] is None or d < s["first_date"]:
                s["first_date"] = d
            if s["last_date"] is None or d > s["last_date"]:
                s["last_date"] = d
    out = sorted(sites.values(),
                 key=lambda s: (-s["visits"], s["first_date"] or "",
                                s["std_address"]))
    with_coord = sum(1 for s in out if not s["coord_degraded"])
    return {
        "hit": bool(out),
        "subject": subject,
        "sites": out,
        "site_count": len(out),
        "total_visits": sum(s["visits"] for s in out),
        "coord_coverage": {"total": len(out), "with_coords": with_coord},
        "degraded": with_coord < len(out),
        "degraded_reason": (
            f"{len(out) - with_coord}/{len(out)} 个落脚点无坐标"
            "（admin_path 文本轨兜底，空间精度降级）"
            if with_coord < len(out) else None),
    }


# ----------------------------------------------------------------------
# Function 5：geo_spatiotemporal_accompany —— 时空伴随（主体对聚合）
# ----------------------------------------------------------------------
# 空间判据精度分级（由准到粗）。**顺序即优先级**，命中高一级即不再回落。
_SPATIAL_LEVELS = ("same_address", "same_road", "geocode", "centroid",
                   "admin_path")
# 精度说明（供 UI/判据文案直出，避免"距离 0 米"被误读为精确同点）
_SPATIAL_LEVEL_NOTE = {
    "same_address": "归一化后为同一地点实体",
    "same_road": "同一区县同一路段（门牌可能不同）",
    "geocode": "门牌级坐标实距",
    "centroid": "坐标为区县质心，距离不代表实际间距",
    "admin_path": "仅同一区县，无坐标可判",
}
# 质心类编码来源：这类坐标是行政区划质心，不是门牌位置
_CENTROID_SOURCES = ("admin_offline", "admin", "centroid", "raw_fallback")


def _is_centroid_coord(row: dict) -> bool:
    """该点坐标是否来自行政区划质心（而非门牌级编码）。

    为什么必须识别
    --------------
    质心坐标下，同区任意两址的 haversine 距离恒为 0。若照常按"坐标距离
    0 米"输出，等于把一个**区级**判据伪装成**米级**判据——正兵会当成
    精确同点，实际可能相距数公里。实测本案：张卫国在文三路 100 号、
    李志强在文三路 259 号，两者坐标同为西湖区质心，旧实现输出
    method=geocode / distance_m=0.0，属伪精确。
    """
    src = str(row.get("geocode_source") or "").strip().lower()
    if not src:
        return False
    return any(k in src for k in _CENTROID_SOURCES)


def _spatial_judgement(a: dict, b: dict, radius_m: int) -> dict | None:
    """两事件点的空间关系判定，返回判据条目；判不上返回 None（不误判）。"""
    la, lb = a.get("location_id"), b.get("location_id")
    if la and lb and la == lb:
        return {"level": "same_address", "distance_m": 0.0, "degraded": False}

    ka = dual_segment_key(a.get("raw_location") or a.get("std_address"))
    kb = dual_segment_key(b.get("raw_location") or b.get("std_address"))
    if ka and kb and ka == kb:
        return {"level": "same_road", "distance_m": None, "degraded": False,
                "note": "同路段，门牌未比对"}

    has_coord = all(r.get("lat") is not None and r.get("lng") is not None
                    for r in (a, b))
    if has_coord:
        dist = _haversine_m((a["lat"], a["lng"]), (b["lat"], b["lng"]))
        if dist <= radius_m:
            centroid = _is_centroid_coord(a) or _is_centroid_coord(b)
            if centroid:
                return {"level": "centroid", "distance_m": round(dist, 1),
                        "degraded": True,
                        "note": _SPATIAL_LEVEL_NOTE["centroid"]}
            return {"level": "geocode", "distance_m": round(dist, 1),
                    "degraded": False}
        return None  # 坐标实距超阈值 → 不同框，不回落粗判据

    ac, bc = a.get("admin_code"), b.get("admin_code")
    if ac and bc and ac == bc:
        return {"level": "admin_path", "distance_m": None, "degraded": True,
                "note": _SPATIAL_LEVEL_NOTE["admin_path"]}
    return None


def _date_span_days(d0: str, d1: str) -> int | None:
    """ISO 日期跨度天数；解析失败返回 None（不猜、不估算）。"""
    try:
        return (date.fromisoformat(d1) - date.fromisoformat(d0)).days
    except Exception:
        return None


# 同框配对的精度权重——与 core/convergence.py 的 PRECISION_WEIGHT 同口径。
# 时刻级：能判时间窗真重叠；日期级：只知前后一天先后出现（同地异时）。
_MEET_W_MINUTE = 1.0
_MEET_W_DATE = 0.2


def geo_spatiotemporal_accompany(store, params: dict, ctx=None) -> dict:
    """时空伴随：异主体事件对经四级空间判据与日窗过滤，聚合到**主体对**。

    与 geo_co_located_radius 的分工
    ------------------------------
    后者输出**事件对**（一次同框即一条），回答"哪两个事件挨上了"；
    本函数聚合成**主体对**，回答"这两个人是否反复出现在相近时空"。
    侦查价值在后者：一次同框可能是巧合，反复同框才构成伴随。

    为什么必须聚合
    --------------
    单看事件对，无法区分"3 次反复同框"与"3 对各同框一次"——而两者的
    研判含义完全不同（前者是关系线索，后者可能只是同期同区活动）。

    时间诚实性（红线）
    ------------------
    obj_trackpoint.date 是**日期**无时刻，故只能判"±window_days 天内
    先后出现"，判不出"同时同地"。产出不带任何"同时/伴随移动/同行"的
    措辞——那是时刻级数据才能支撑的结论。
    """
    radius_m = int(params.get("radius_m", 200))
    if not 10 <= radius_m <= 10000:
        raise ValueError(f"radius_m 允许 10-10000，得到 {radius_m}")
    window_days = int(params.get("window_days", 1))
    if not 0 <= window_days <= 30:
        raise ValueError(f"window_days 允许 0-30，得到 {window_days}")
    # 时刻窗：**可选**（None = 不启用）。仅当参与配对的两事件都精确到
    # minute/second 档时生效；date/hour 档仍走 window_days 天级逻辑，
    # 行为与加此参数前完全一致。所以"数据没有时刻"时语义零变化，
    # "数据有时刻"时自动获得真正的伴随判定，不需改判据代码。
    _wm = params.get("window_minutes")
    window_minutes = None if _wm in (None, "") else int(_wm)
    if window_minutes is not None and not 1 <= window_minutes <= 1440:
        raise ValueError(f"window_minutes 允许 1-1440，得到 {window_minutes}")
    min_meets = int(params.get("min_meets", 2))
    if not 1 <= min_meets <= 100:
        raise ValueError(f"min_meets 允许 1-100，得到 {min_meets}")
    max_pairs = int(params.get("max_pairs", 500))
    # repeated_only：只有"达到反复下限的主体对"才算命中。
    # 为什么要有这个开关——**单次同框与反复同框是两种性质的东西**：
    #   单次同框 = 结构事实（观察级，不足以支撑关系判断）
    #   反复同框 = 可验证的命题（线索级，值得挂假设去证伪）
    # 规则路径（R-GEO-3）置 True，让"反复同框"升格为带假设的命题；
    # 镜头路径置 False，单次同框仍出观察——留着比丢掉好，它是补数据的入口。
    repeated_only = bool(params.get("repeated_only", False))

    try:
        rows = store.query(_site_rows_sql(ctx, store=store) + " ORDER BY d, t.track_id")
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            pairs=[], companions=[], scanned_events=0)

    rows.sort(key=lambda r: (str(r["d"]), str(r["track_id"])))
    pairs: list[dict] = []
    truncated = False
    # 本次判定中真正走了"时刻窗"（而非天级窗）的配对数——用于产出自陈，
    # 让正兵看到这次结论到底是分钟级还是天级判出来的。
    minute_windowed = 0
    # 逐行精度一次算好（避免 O(n²) 里重复派生）
    _prec = [_precision_of(r) for r in rows]
    n = len(rows)
    for i in range(n):
        a = rows[i]
        if not a["d"]:
            continue
        for j in range(i + 1, n):
            b = rows[j]
            if not b["d"]:
                continue
            delta = (b["d"] - a["d"]).days
            if delta > window_days:
                break
            if not a["person_raw"] or a["person_raw"] == b["person_raw"]:
                continue
            sj = _spatial_judgement(a, b, radius_m)
            if sj is None:
                continue
            # 时间精度：两事件取**较低**档（木桶效应）。都精确到分钟及以上
            # 才计算时刻级间隔；否则只有 delta_days 可信，**不得**声称同时
            # ——这是防 00:00:00 伪精确的关键。
            pa = _precision_of(a)
            pb = _precision_of(b)
            prec = _weaker_precision(pa, pb)
            tpair = _time_pair(a, b, prec)
            # 时刻窗过滤：仅当**可判时刻**（两事件都 minute/second 档）且
            # 配置了 window_minutes 时，按真实秒间隔判定，超窗即不算同框。
            # date/hour 档绝不进入此分支——否则就是"同落 00:00:00 当同时"，
            # 与空间侧 distance_m=0.0 是同一类伪精确。
            if window_minutes is not None and tpair["simultaneous_judgeable"]:
                if (tpair["time_span"] is None
                        or tpair["time_span"] > window_minutes * 60):
                    continue
                minute_windowed += 1
            pairs.append({
                "person_1": a["person_raw"], "person_2": b["person_raw"],
                "date_1": a["d"].isoformat(), "date_2": b["d"].isoformat(),
                "delta_days": delta,
                "time_precision": tpair["time_precision"],
                "time_span": tpair["time_span"],
                "simultaneous_judgeable": tpair["simultaneous_judgeable"],
                "event_pk_1": str(a["track_id"]),
                "event_pk_2": str(b["track_id"]),
                "location_id_1": a["location_id"],
                "location_id_2": b["location_id"],
                "std_address_1": a["std_address"] or a["raw_location"],
                "std_address_2": b["std_address"] or a["raw_location"],
                # 判据：四级精度 + 说明，UI 直出不另行加工
                "spatial_level": sj["level"],
                "spatial_note": sj.get("note") or _SPATIAL_LEVEL_NOTE[sj["level"]],
                "distance_m": sj["distance_m"],
                "degraded": sj["degraded"],
            })
            if len(pairs) >= max_pairs:
                truncated = True
                break
        if truncated:
            break

    # ---- 聚合到主体对 ----
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in pairs:
        key = tuple(sorted([p["person_1"], p["person_2"]]))
        groups.setdefault(key, []).append(p)

    companions: list[dict] = []
    for key, ps in groups.items():
        ps.sort(key=lambda x: (x["date_1"], x["date_2"]))
        levels = [p["spatial_level"] for p in ps]
        # 最准的一次决定这组判据的可信上限
        best = min(levels, key=lambda x: _SPATIAL_LEVELS.index(x))
        dates = sorted([p["date_1"] for p in ps] + [p["date_2"] for p in ps])
        addrs = sorted({p["std_address_1"] for p in ps}
                       | {p["std_address_2"] for p in ps})
        # ---- 精度加权的有效同框数 ----
        # 为什么不能直接按 meet_count 排序：日期级配对只证明「前后一天
        # 先后出现在同一路段」（同地异时），每一次都是独立巧合；时刻级
        # 配对证明「时间窗真重叠」。故 3 次时刻级 > 15 次日期级。
        # 不加权就会把次数堆出来的弱证据排到最前——替正兵排反序，
        # 比不排更糟。此处只做**自陈与排序**，不做定性。
        _minute_n = sum(1 for p in ps if p.get("simultaneous_judgeable"))
        _date_n = len(ps) - _minute_n
        _eff = round(_minute_n * _MEET_W_MINUTE + _date_n * _MEET_W_DATE, 3)
        companions.append({
            "person_a": key[0], "person_b": key[1],
            "meet_count": len(ps),
            "minute_level_meets": _minute_n,
            "date_level_meets": _date_n,
            "effective_meet_score": _eff,
            # 有效反复：按加权分量判断是否够格升格为命题（规则路径用）
            "effective_repeated": _eff >= min_meets,
            "repeated": len(ps) >= min_meets,
            "first_date": dates[0], "last_date": dates[-1],
            "span_days": _date_span_days(dates[0], dates[-1]),
            "locations": addrs,
            "location_count": len(addrs),
            "spatial_level": best,
            "spatial_note": _SPATIAL_LEVEL_NOTE[best],
            "level_breakdown": {lv: levels.count(lv) for lv in set(levels)},
            "degraded": any(p["degraded"] for p in ps),
            "event_pks": [p["event_pk_1"] for p in ps]
                         + [p["event_pk_2"] for p in ps],
        })
    # ---- 主体引用归一：两个主体各自解析，互不以对方的歧义为代价 ----
    # 一方重名（pk 为空）不废掉整对：另一方的 pk 照常给出，歧义单独标。
    # 若因一方歧义就把整对丢弃，等于让一个待裁决的名字连累确定的那一半。
    _pnames = sorted({c["person_a"] for c in companions}
                     | {c["person_b"] for c in companions})
    _persons = _person_refs(store, _pnames)
    for c in companions:
        c["person_a_pk"] = (_persons.get(c["person_a"]) or {}).get("pk")
        c["person_b_pk"] = (_persons.get(c["person_b"]) or {}).get("pk")
        c["person_a_ambiguous"] = bool(
            (_persons.get(c["person_a"]) or {}).get("ambiguous"))
        c["person_b_ambiguous"] = bool(
            (_persons.get(c["person_b"]) or {}).get("ambiguous"))
        if c["person_a_ambiguous"]:
            c["person_a_candidates"] = (_persons.get(c["person_a"])
                                        or {}).get("candidates")
        if c["person_b_ambiguous"]:
            c["person_b_candidates"] = (_persons.get(c["person_b"])
                                        or {}).get("candidates")

    # 排序按**加权有效分**而非裸次数——次数会骗人：日期级堆出来的
    # 15 次排在时刻级 3 次之前，等于把弱证据推到正兵眼前。
    companions.sort(key=lambda c: (-c["effective_meet_score"],
                                   -c["meet_count"],
                                   _SPATIAL_LEVELS.index(c["spatial_level"]),
                                   c["person_a"]))

    # 本次判定的**实际可比精度** = 所有参与行的最低档（木桶效应）。
    # 没有 pairs 时退化为全体行的最低档——同样是"只说最粗的"，不虚报。
    _involved = {p for pr in pairs for p in
                 (pr["event_pk_1"], pr["event_pk_2"])}
    _prec_rows = [(_prec[i], rows[i]) for i in range(n)
                  if not _involved or str(rows[i]["track_id"]) in _involved]
    overall = None
    for pr_, _r in _prec_rows:
        overall = pr_ if overall is None else _weaker_precision(overall, pr_)
    precision_dist: dict[str, int] = {}
    for pr_, _r in _prec_rows:
        precision_dist[pr_ or "unknown"] = precision_dist.get(pr_ or "unknown", 0) + 1

    repeated = [c for c in companions if c["repeated"]]
    return {
        # hit 口径随 repeated_only 变：规则路径要求"反复"才成立命题；
        # 镜头路径只要有同框结构就出观察。
        "hit": bool(repeated) if repeated_only else bool(pairs),
        "pairs": pairs,
        "pair_count": len(pairs),
        "companions": companions,
        "companion_count": len(companions),
        "repeated_companions": repeated,
        "repeated_count": len(repeated),
        "scanned_events": n,
        "radius_m": radius_m,
        "window_days": window_days,
        "min_meets": min_meets,
        # 时刻窗自陈：mode 告诉正兵这次是按分钟判的还是按天判的。
        # days = 数据无时刻（或全 NULL），结论只能到"±N 天先后"；
        # minutes = 确有配对走了真实秒间隔过滤，才可以说"同一时间窗"。
        "window_minutes": window_minutes,
        "time_window_mode": "minutes" if minute_windowed else "days",
        "minute_windowed_pairs": minute_windowed,
        # 口径自陈：**按本次实际可比精度**写，不写死"date"。
        # 全 NULL（当前数据）→ date 档，措辞与升级前一致（不支持同时）；
        # 接入时刻数据后自动升档，措辞随之改变——正兵看得到判定依据变了。
        "time_granularity": overall,
        "time_precision_dist": precision_dist,
        "simultaneous_judgeable": any(
            p.get("simultaneous_judgeable") for p in pairs),
        "time_note": _time_note(overall, window_days, window_minutes),
        "truncated": truncated,
        "degraded": truncated or any(p["degraded"] for p in pairs),
        "degraded_reason": (
            "达到 max_pairs 上限被截断" if truncated else
            ("部分事件对按区县质心或行政区划降级判定，空间精度不足"
             if any(p["degraded"] for p in pairs) else None)),
    }


# ----------------------------------------------------------------------
# Function 2：geo_co_located_radius —— 距离阈值同框
# ----------------------------------------------------------------------
def geo_co_located_radius(store, params: dict, ctx=None) -> dict:
    """异主体轨迹事件对：坐标距离 ≤radius_m 且 |Δdate| ≤window_days → 同框。

    补强 lnk_co_located 的文本硬 join（异名近址漏报）；坐标缺失的事件对
    回落同县级 admin_code 判近邻并标 method=admin_path 降级；坐标与区划
    均缺的对不判同（保守不误判）。
    """
    radius_m = int(params.get("radius_m", 200))
    if not 10 <= radius_m <= 10000:
        raise ValueError(f"radius_m 允许 10-10000，得到 {radius_m}")
    window_days = int(params.get("window_days", 1))
    if not 0 <= window_days <= 30:
        raise ValueError(f"window_days 允许 0-30，得到 {window_days}")
    max_pairs = int(params.get("max_pairs", 500))
    if not 1 <= max_pairs <= 5000:
        raise ValueError(f"max_pairs 允许 1-5000，得到 {max_pairs}")
    try:
        rows = store.query(_site_rows_sql(ctx, store=store)
                           + " ORDER BY d, t.track_id")
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            pairs=[], subject="", scanned_events=0)

    rows.sort(key=lambda r: (str(r["d"]), str(r["track_id"])))
    pairs: list[dict] = []
    truncated = False
    n = len(rows)
    for i in range(n):
        a = rows[i]
        if not a["d"]:
            continue
        for j in range(i + 1, n):
            b = rows[j]
            if not b["d"]:
                continue
            dd = (b["d"] - a["d"]).days
            if dd > window_days:
                break  # 按日期排序，后续只会更大
            if not a["person_raw"] or a["person_raw"] == b["person_raw"]:
                continue
            entry = {"person_1": a["person_raw"], "person_2": b["person_raw"],
                     "date_1": a["d"].isoformat(), "date_2": b["d"].isoformat(),
                     "event_pk_1": str(a["track_id"]),
                     "event_pk_2": str(b["track_id"]),
                     "location_id_1": a["location_id"],
                     "location_id_2": b["location_id"],
                     "std_address_1": a["std_address"] or a["raw_location"],
                     "std_address_2": b["std_address"] or b["raw_location"]}
            has_coord = all(r["lat"] is not None and r["lng"] is not None
                            for r in (a, b))
            if has_coord:
                dist = _haversine_m((a["lat"], a["lng"]), (b["lat"], b["lng"]))
                if dist > radius_m:
                    continue
                entry.update({"method": "geocode",
                              "distance_m": round(dist, 1), "degraded": False})
            else:
                # 文本轨回落：同县级区划判近邻（精度降级，不判距离）
                if not a["admin_code"] or a["admin_code"] != b["admin_code"]:
                    continue
                entry.update({"method": "admin_path", "distance_m": None,
                              "degraded": True,
                              "degraded_reason": "坐标缺失，回落同县级行政区划判近邻"})
            pairs.append(entry)
            if len(pairs) >= max_pairs:
                truncated = True
                break
        if truncated:
            break
    return {
        "hit": bool(pairs),
        "pairs": pairs,
        "pair_count": len(pairs),
        "subject": pairs[0]["person_1"] if pairs else "",
        "scanned_events": n,
        "radius_m": radius_m,
        "window_days": window_days,
        "geocode_pairs": sum(1 for p in pairs if p["method"] == "geocode"),
        "admin_path_pairs": sum(1 for p in pairs if p["method"] == "admin_path"),
        "truncated": truncated,
        "degraded": truncated or any(p["degraded"] for p in pairs),
        "degraded_reason": (
            "达到 max_pairs 上限被截断" if truncated else
            ("部分事件对坐标缺失，按同县级行政区划近邻降级判定"
             if any(p["degraded"] for p in pairs) else None)),
    }


# ----------------------------------------------------------------------
# Function 3：geo_buffer_scan —— 缓冲区环带扫描
# ----------------------------------------------------------------------
def geo_buffer_scan(store, params: dict, ctx=None) -> dict:
    """中心锚点的半径环带扫描（缓冲区理论：住所紧邻区反而低发）。

    锚点来源二选一：显式 center_lat/center_lng（编排层透传），或 target
    主体的带坐标落脚点重心。扫描全量轨迹事件按到锚点距离分三档：
    inner（≤内环，低发带对照）/ ring（内环-外环，排查带）/ outside。
    """
    inner = int(params.get("inner_radius_m", 1000))
    outer = int(params.get("outer_radius_m", 5000))
    if not 50 <= inner <= 100000:
        raise ValueError(f"inner_radius_m 允许 50-100000，得到 {inner}")
    if not inner < outer <= 200000:
        raise ValueError(f"outer_radius_m 须大于 inner 且 ≤200000，得到 {outer}")
    clat, clng = params.get("center_lat"), params.get("center_lng")
    target = (params.get("target") or params.get("target_subject") or "").strip()
    # 反向追踪的时间窗：date_from/date_to 为闭区间字符串（YYYY-MM-DD）
    date_from = (params.get("date_from") or "").strip() or None
    date_to = (params.get("date_to") or "").strip() or None
    # 锚点主体自身的事件是否排除。默认排除——"扫某人周围结果全是他自己"
    # 是循环论证，反向追踪要的是「谁（别人）在那儿」。
    exclude_self = params.get("exclude_anchor_subject", True)
    if isinstance(exclude_self, str):
        exclude_self = exclude_self.strip().lower() not in (
            "0", "false", "no", "off")

    anchor = None
    subject = None
    if clat is not None and clng is not None:
        anchor = {"lat": float(clat), "lng": float(clng), "source": "explicit",
                  "sites_used": None}
    elif target:
        from core.graph import resolve_subject
        try:
            subject = resolve_subject(store, target,
                                      params.get("target_type", "auto"), ctx)
        except ValueError as e:
            # 同名多类实体未消歧：不炸规则层，降级并建议显式 target_type
            return _degraded_result(
                f"主体 {target!r} 消歧失败：{str(e).splitlines()[0][:120]}"
                f"（请用 target_type 显式指定）",
                anchor=None, inner_events=[], ring_events=[], subjects=[], subject_count=0, coord_precision=None)
        if subject is None:
            return _degraded_result(
                f"主体 {target!r} 在语义层实体中不存在（未建档或未归一）",
                anchor=None, inner_events=[], ring_events=[], subjects=[], subject_count=0, coord_precision=None)
        sites_res = geo_subject_sites(store, params, ctx)
        coord_sites = [s for s in sites_res.get("sites", [])
                       if not s["coord_degraded"]]
        if not coord_sites:
            return _degraded_result(
                f"主体 {subject['name']} 无带坐标落脚点，锚点不可计算",
                subject=subject, anchor=None, inner_events=[], ring_events=[], subjects=[], subject_count=0, coord_precision=None)
        anchor = {"lat": sum(s["lat"] for s in coord_sites) / len(coord_sites),
                  "lng": sum(s["lng"] for s in coord_sites) / len(coord_sites),
                  "source": f"subject_centroid:{subject['name']}",
                  "sites_used": len(coord_sites)}
    else:
        return _degraded_result(
            "缺少锚点（target 主体或 center_lat/center_lng 须给其一）",
            anchor=None, inner_events=[], ring_events=[], subjects=[], subject_count=0, coord_precision=None)

    try:
        rows = store.query(_site_rows_sql(ctx, store=store) + " ORDER BY d, t.track_id")
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            subject=subject, anchor=anchor, inner_events=[], ring_events=[], subjects=[], subject_count=0, coord_precision=None)

    inner_events: list[dict] = []
    ring_events: list[dict] = []
    outside_count = 0
    no_coord_count = 0
    self_count = 0          # 锚点主体自身事件（默认已排除，仅计数留痕）
    out_of_window = 0       # 落在时间窗外的事件
    centroid_hits = 0       # 距离由区划质心推算的事件数
    ac = (anchor["lat"], anchor["lng"])
    for r in rows:
        d = r["d"].isoformat() if r["d"] else None
        # 时间窗过滤（反向追踪的核心：案发时谁在现场附近）
        if date_from and (not d or d < date_from):
            out_of_window += 1
            continue
        if date_to and (not d or d > date_to):
            out_of_window += 1
            continue
        if (exclude_self and subject
                and (r["person_raw"] or "").strip() == subject["name"]):
            self_count += 1
            continue
        if r["lat"] is None or r["lng"] is None:
            no_coord_count += 1
            continue
        dist = _haversine_m(ac, (r["lat"], r["lng"]))
        if dist > outer:
            outside_count += 1
            continue
        # 精度自陈：质心坐标下的米数是区级推算，不是实际间距。
        # 不做这步就会出现"三个不同地点距离都是 2156.8 米"的伪精确。
        centroid = _is_centroid_coord(r)
        if centroid:
            centroid_hits += 1
        ev = {"subject_raw": r["person_raw"],
              "event_pk": str(r["track_id"]),
              "date": d,
              "location_id": r["location_id"],
              "std_address": r["std_address"] or r["raw_location"],
              "lat": r["lat"], "lng": r["lng"],
              "distance_m": round(dist, 1),
              "coord_precision": "centroid" if centroid else "geocode",
              "distance_note": (_SPATIAL_LEVEL_NOTE["centroid"]
                                if centroid else None)}
        (inner_events if dist <= inner else ring_events).append(ev)
    inner_events.sort(key=lambda e: (e["distance_m"], e["event_pk"]))
    ring_events.sort(key=lambda e: (e["distance_m"], e["event_pk"]))

    # 主体聚合：反向追踪要回答「谁在那儿」，不是「有哪些事件」。
    # 事件列表按人归并后，正兵一眼看出涉及几个主体、各来过几次。
    subjects: dict[str, dict] = {}
    for ev in ring_events + inner_events:
        nm = (ev["subject_raw"] or "").strip() or "（未署名）"
        s = subjects.setdefault(nm, {
            "name": nm, "count": 0, "first_date": None, "last_date": None,
            "locations": [], "min_distance_m": None,
            "coord_precision": set()})
        s["count"] += 1
        s["coord_precision"].add(ev["coord_precision"])
        if ev["date"]:
            if s["first_date"] is None or ev["date"] < s["first_date"]:
                s["first_date"] = ev["date"]
            if s["last_date"] is None or ev["date"] > s["last_date"]:
                s["last_date"] = ev["date"]
        loc = ev["std_address"]
        if loc and loc not in s["locations"]:
            s["locations"].append(loc)
        if ev["distance_m"] is not None:
            if (s["min_distance_m"] is None
                    or ev["distance_m"] < s["min_distance_m"]):
                s["min_distance_m"] = ev["distance_m"]
    subject_list = sorted(
        ({"name": v["name"], "count": v["count"],
          "first_date": v["first_date"], "last_date": v["last_date"],
          "locations": v["locations"], "min_distance_m": v["min_distance_m"],
          "coord_precision": (v["coord_precision"].pop()
                              if len(v["coord_precision"]) == 1 else "mixed")}
         for v in subjects.values()),
        key=lambda s: (-s["count"], s["name"]))

    # 主体引用归一：反向追踪的答案「谁在那儿」要能直接点亮关系视图，
    # 故每个主体一并给出代理键；重名不给 pk，给候选（不静默挑一个）。
    _bpersons = _person_refs(store, [s["name"] for s in subject_list])
    for s in subject_list:
        info = _bpersons.get(s["name"]) or {}
        s["person_pk"] = info.get("pk")
        s["person_pk_ambiguous"] = bool(info.get("ambiguous"))
        if info.get("ambiguous"):
            s["person_pk_candidates"] = list(info.get("candidates") or [])
            s["person_pk_reason"] = info.get("reason")
    # 事件行同样挂引用，避免下游再按名字二次 join。
    for ev in (inner_events + ring_events):
        info = _bpersons.get((ev.get("subject_raw") or "").strip()) or {}
        ev["person_pk"] = info.get("pk")
        ev["person_pk_ambiguous"] = bool(info.get("ambiguous"))

    if centroid_hits and centroid_hits < (len(inner_events)
                                          + len(ring_events)):
        overall_precision = "mixed"
    elif centroid_hits:
        overall_precision = "centroid"
    else:
        overall_precision = "geocode"
    # hit 判据：环带内须有**可归因主体**的事件。
    # 旧判据 bool(ring_events) 在以某主体重心为锚点时恒真（他自己就在那儿），
    # 属循环论证；改为环带内有主体即命中，显式锚点同理。
    hit = bool(subject_list)
    return {
        "hit": hit,
        "subject": subject,
        "anchor": anchor,
        "inner_radius_m": inner,
        "outer_radius_m": outer,
        "date_from": date_from,
        "date_to": date_to,
        "subjects": subject_list,
        "subject_count": len(subject_list),
        "inner_events": inner_events[:150],
        "ring_events": ring_events[:150],
        "inner_count": len(inner_events),
        "ring_count": len(ring_events),
        "outside_count": outside_count,
        "no_coord_count": no_coord_count,
        "self_count": self_count,
        "out_of_window": out_of_window,
        "coord_precision": overall_precision,
        "distance_note": (_SPATIAL_LEVEL_NOTE["centroid"]
                          if overall_precision != "geocode" else None),
        "truncated": len(inner_events) > 150 or len(ring_events) > 150,
        "degraded": no_coord_count > 0 or overall_precision != "geocode",
        "degraded_reason": "; ".join(
            f_ for f_ in (
                (f"{no_coord_count} 起事件无坐标未纳入环带扫描"
                 if no_coord_count else None),
                (f"{centroid_hits} 起事件坐标系区划质心，距离不代表实际间距"
                 if centroid_hits else None),
            ) if f_) or None,
    }


# ----------------------------------------------------------------------
# Function 4：geo_profile_cgt —— Rossmo CGT 概率面
# ----------------------------------------------------------------------
def geo_profile_cgt(store, params: dict, ctx=None) -> dict:
    """Rossmo CGT 地理画像：系列事件点集 → 网格化概率面 + 优先排查区。

    target 给出时单主体画像（输出完整 GeoJSON）；缺省时全主体分组扫描
    （每组事件数 ≥min_events 才画像，输出每组摘要与顶格，不嵌 GeoJSON，
    供规则层批量判定）。坐标缺失事件丢弃并计数；有效事件 <min_events
    降级不命中（不得以缺口充数）。
    """
    min_events = int(params.get("min_events", 5))
    if not 2 <= min_events <= 100:
        raise ValueError(f"min_events 允许 2-100，得到 {min_events}")
    grid_meters = int(params.get("grid_meters", 200))
    if not 50 <= grid_meters <= 5000:
        raise ValueError(f"grid_meters 允许 50-5000，得到 {grid_meters}")
    buffer_m = int(params.get("buffer_m", 1000))
    if not 100 <= buffer_m <= 50000:
        raise ValueError(f"buffer_m 允许 100-50000，得到 {buffer_m}")
    top_n = int(params.get("top_n", 60))
    if not 1 <= top_n <= 200:
        raise ValueError(f"top_n 允许 1-200，得到 {top_n}")
    # 空间尺度自检（默认开启）：事件点离散/与 buffer_m 错配时不产出概率面
    scale_check = params.get("scale_check", True)
    if isinstance(scale_check, str):
        scale_check = scale_check.strip().lower() not in ("0", "false", "no", "off")
    max_isolated_ratio = float(params.get("max_isolated_ratio", 0.5))
    max_dispersion_ratio = float(params.get("max_dispersion_ratio", 2.0))
    target = (params.get("target") or params.get("target_subject") or "").strip()

    subject = None
    if target:
        from core.graph import resolve_subject
        subject = resolve_subject(store, target,
                                  params.get("target_type", "auto"), ctx)
        if subject is None:
            return _degraded_result(
                f"主体 {target!r} 在语义层实体中不存在（未建档或未归一）",
                subject=None, profiles=[])
        sql = _site_rows_sql(ctx, "WHERE t.person_raw = ? ", store)
        sql_params = (subject["name"],)
    else:
        sql = _site_rows_sql(ctx, "WHERE t.person_raw IS NOT NULL ", store)
        sql_params = ()
    try:
        rows = store.query(sql + "ORDER BY t.person_raw, d, t.track_id",
                           sql_params)
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            subject=subject, profiles=[])

    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["person_raw"]:
            groups.setdefault(r["person_raw"], []).append(r)

    profiles: list[dict] = []
    for name in sorted(groups):
        grows = groups[name]
        pts = sorted({(float(r["lat"]), float(r["lng"])) for r in grows
                      if r["lat"] is not None and r["lng"] is not None})
        total = len({str(r["track_id"]) for r in grows})
        dropped = len({str(r["track_id"]) for r in grows
                       if r["lat"] is None or r["lng"] is None})
        entry = {"subject_raw": name, "events_total": total,
                 "events_used": len(pts), "events_dropped_no_coord": dropped}
        if len(pts) < min_events:
            entry.update({"profiled": False, "hit": False, "degraded": True,
                          "degraded_reason": (
                              f"有效坐标事件 {len(pts)} 起 < min_events="
                              f"{min_events}，概率面不可计算（缺口不充数）")})
            profiles.append(entry)
            continue
        surf = cgt_surface(pts, grid_meters=grid_meters, buffer_m=buffer_m)
        scale = (cgt_scale_check(pts, buffer_m=buffer_m,
                                 max_isolated_ratio=max_isolated_ratio,
                                 max_dispersion_ratio=max_dispersion_ratio)
                 if scale_check else None)
        if scale is not None:
            entry["scale_check"] = scale
        if scale is not None and not scale["applicable"]:
            # 尺度不适用 → 不产出概率面（缺口不充数，伪峰比无结论更危险）
            entry.update({"profiled": False, "hit": False, "degraded": True,
                          "degraded_reason": f"CGT 不适用：{scale['reason']}"
                                             f"；建议 buffer_m≈{scale['suggested_buffer_m']}m"
                                             f" 或换用时空伴随/同框判据"})
            profiles.append(entry)
            continue
        zones = [{"rank": i + 1, "lat": c["lat"], "lng": c["lng"],
                  "probability": round(c["probability"], 6)}
                 for i, c in enumerate(surf["cells"][:top_n])
                 if c["probability"] > 0]
        entry.update({
            "profiled": True, "hit": bool(zones),
            "buffer_m": buffer_m,
            "grid_meters": surf["grid_meters"],
            "grid": {"rows": surf["rows"], "cols": surf["cols"]},
            "coord_coverage": (round(len(pts) / total, 4) if total else None),
            "top_zone": zones[0] if zones else None,
            "degraded": dropped > 0,
            "degraded_reason": (
                f"{dropped} 起事件无坐标未纳入概率面" if dropped else None)})
        if target:
            # 单主体模式附完整产物：优先排查区清单 + GeoJSON 概率面
            entry["priority_zones"] = zones
            entry["geojson"] = _cells_to_geojson(surf)
            # 纳入概率面的事件行键（坐标齐备）——供镜头层挂证据引用，
            # 截断在编排层做（镜头 150 上限）；全扫模式保持摘要紧凑不带
            entry["event_pks"] = sorted({
                str(r["track_id"]) for r in grows
                if r["lat"] is not None and r["lng"] is not None})
        profiles.append(entry)

    hit = any(p.get("hit") for p in profiles)
    # 降级原因优先透出"尺度不适用"这类可行动结论：通用文案会让正兵
    # 以为只是数据不足，从而去补数据，而真正的动作是调 buffer_m 或换模型。
    _scale_reasons = [f"{p.get('subject_raw')}：{p['degraded_reason']}"
                      for p in profiles
                      if p.get("degraded") and p.get("scale_check")
                      and not p["scale_check"].get("applicable")]
    if _scale_reasons:
        _reason = "CGT 尺度不适用 → " + "；".join(_scale_reasons[:3])
    elif any(p.get("degraded") for p in profiles):
        _reason = "部分主体有效事件不足或坐标缺失（各 profiles 条目自带原因）"
    else:
        _reason = None
    out = {"hit": hit, "subject": subject, "profiles": profiles,
           "profiled_count": sum(1 for p in profiles if p.get("profiled")),
           "min_events": min_events, "grid_meters": grid_meters,
           "buffer_m": buffer_m,
           "degraded": any(p.get("degraded") for p in profiles),
           "degraded_reason": _reason}
    if target and profiles:
        # 单主体模式顶层平铺，便于规则/镜头直接消费。
        # grid_meters 用实际生效值（max_cells 超限时格宽会倍增，
        # 若平铺请求参数值会让产物 basis 误报格宽）。
        p0 = profiles[0]
        for k in ("events_total", "events_used", "events_dropped_no_coord",
                  "grid", "grid_meters", "top_zone", "priority_zones",
                  "geojson", "event_pks", "coord_coverage"):
            if k in p0:
                out[k] = p0[k]
    return out


# ----------------------------------------------------------------------
# Function 6：geo_trajectory_segment —— 轨迹分段与停留点识别
# ----------------------------------------------------------------------
def _coord_precision(row: dict) -> str:
    """坐标精度档：exact（门牌级）/ centroid（区划质心）/ none（无坐标）。"""
    if row.get("lat") is None or row.get("lng") is None:
        return "none"
    return "centroid" if _is_centroid_coord(row) else "exact"


def geo_trajectory_segment(store, params: dict, ctx=None) -> dict:
    """原始轨迹 → 停留段 / 移动段（轨迹分段与停留点识别）。

    为什么时刻是硬前提
    ------------------
    "停留"的判据是**在某处持续多久**，只有日期无法计算持续时长。date 档
    （timestamp 全 NULL）下**不产出分段**，只给降级原因——拿日期编造停留
    时长是伪精确，与"质心坐标输出 0 米"是同一类错误（实测本案原 17 条
    轨迹 timestamp 全 NULL，此时分段恒无意义）。

    坐标精度决定距离口径
    --------------------
    centroid 档（区划质心）下同区任意两址距离恒为 0，停留判定改为**按地点
    实体是否相同**，移动段距离标 None 并说明，不给米数。
    """
    from core.graph import resolve_subject
    from core.time_semantics import derive_time_precision

    target = (params.get("target") or params.get("target_subject") or "").strip()
    if not target:
        return _degraded_result("缺少 target 参数（轨迹分段须指定主体）",
                                subject=None, stays=[], moves=[],
                                stay_count=0, move_count=0)
    subject = resolve_subject(store, target, params.get("target_type", "auto"), ctx)
    if subject is None:
        return _degraded_result(f"主体 {target!r} 在语义层实体中不存在",
                                subject=None, stays=[], moves=[],
                                stay_count=0, move_count=0)

    def _int(v, default):
        return default if v in (None, "") else int(v)
    stay_radius_m = _int(params.get("stay_radius_m"), 200)
    stay_min_minutes = _int(params.get("stay_min_minutes"), 30)

    try:
        rows = store.query(
            _site_rows_sql(ctx, "WHERE t.person_raw = ? ", store), (subject["name"],))
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            subject=subject, stays=[], moves=[], stay_count=0, move_count=0)

    pts = []
    for r in rows:
        ts = r.get("ts")
        prec = derive_time_precision(ts)
        pts.append({"ts": ts, "prec": prec,
                    "location_id": r.get("location_id"),
                    "std_address": r.get("std_address") or r.get("raw_location"),
                    "lat": r.get("lat"), "lng": r.get("lng"),
                    "cprec": _coord_precision(r),
                    "track_id": r.get("track_id")})

    # 只有 hour/minute/second 档可算持续时长。
    # 为什么 hour 档也收：分段只需**能算时长**（小时级已够判断"是否停留"），
    # 不像分钟窗过滤那样要求分钟级精度。全局仍把整点派生为 hour 档（防
    # "只到小时的记录"混进分钟窗判定），此处单独放宽，不影响那条红线。
    # date 档（timestamp 为 NULL / 00:00:00）依旧排除——那才是真无时刻。
    usable = [p for p in pts if p["prec"] in ("hour", "minute", "second")]
    if len(usable) < 2:
        return _degraded_result(
            f"轨迹时间精度为 {usable[0]['prec'] if usable else '无'} 档，"
            "无可用时刻（停留时长需 minute 及以上精度），不产出分段",
            subject=subject, stays=[], moves=[], stay_count=0, move_count=0,
            trackpoint_total=len(pts), trackpoint_usable=len(usable))
    usable.sort(key=lambda p: p["ts"])

    def _same_place(a: dict, b: dict) -> bool:
        if a["location_id"] and a["location_id"] == b["location_id"]:
            return True
        if a["cprec"] == "exact" and b["cprec"] == "exact":
            return _haversine_m((a["lat"], a["lng"]),
                                (b["lat"], b["lng"])) <= stay_radius_m
        return False

    # 按日切分：跨日必断簇。
    # 为什么：稀疏采样下只看地点会把整周的点串成一个"停留"——实测李志强
    # 每日 09:00/17:30 均在江陵路，不断簇会得到"停留 10080 分钟（7 天）"，
    # 那是把 7 个工作日误并成一次驻留。逐日分段后每天各自成段，语义正确。
    clusters, cur = [], [usable[0]]
    for p in usable[1:]:
        prev = cur[-1]
        if prev["ts"].date() != p["ts"].date():
            clusters.append(cur)
            cur = [p]
            continue
        if _same_place(prev, p):
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)

    stays, transits = [], []
    for cl in clusters:
        span_min = (cl[-1]["ts"] - cl[0]["ts"]).total_seconds() / 60.0
        item = {"location_id": cl[0]["location_id"],
                "std_address": cl[0]["std_address"],
                "lat": cl[0]["lat"], "lng": cl[0]["lng"],
                "cprec": cl[0]["cprec"],
                "start": cl[0]["ts"].isoformat(),
                "end": cl[-1]["ts"].isoformat(),
                "duration_minutes": round(span_min, 1),
                "point_count": len(cl),
                "event_pks": [str(p["track_id"]) for p in cl if p["track_id"]]}
        if len(cl) > 1 and span_min >= stay_min_minutes:
            stays.append(item)
        else:
            transits.append(item)

    moves = []
    for a, b in zip(stays, stays[1:]):
        # 移动段只在**同一天内**的相邻停留之间成立。跨日的相邻停留之间是
        # 过夜/无采样，不是移动——不断开会得到"880 分钟移动 0 米"这类
        # 虚假段（实测：前一日 18:10 离开单位、次日 08:50 回到单位）。
        if a["start"][:10] != b["start"][:10]:
            continue
        dur_min = round((_dt_iso(b["start"]) - _dt_iso(a["end"])).total_seconds() / 60.0, 1)
        dist = None
        if a["cprec"] == "exact" and b["cprec"] == "exact":
            dist = round(_haversine_m((a["lat"], a["lng"]), (b["lat"], b["lng"])), 1)
        moves.append({"from": a["std_address"], "to": b["std_address"],
                      "start": a["end"], "end": b["start"],
                      "duration_minutes": dur_min,
                      "distance_m": dist,
                      "distance_note": (None if dist is not None else
                                        "坐标为区县质心，距离不代表实际间距"),
                      "avg_kmh": (round(dist / 1000.0 / (dur_min / 60.0), 1)
                                  if dist is not None and dur_min > 0 else None)})

    coord_p = next((p["cprec"] for p in usable if p["cprec"] != "none"), "none")
    return {
        "hit": bool(stays),
        "subject": subject,
        "stays": stays, "moves": moves,
        "stay_count": len(stays), "move_count": len(moves),
        "transit_count": len(transits),
        "total_stay_minutes": round(sum(s["duration_minutes"] for s in stays), 1),
        "total_move_minutes": round(sum(m["duration_minutes"] for m in moves), 1),
        "stay_radius_m": stay_radius_m, "stay_min_minutes": stay_min_minutes,
        "sampling_note": ("轨迹为稀疏采样，duration_minutes 是当日首尾点间隔，"
                          "不等于连续驻留时长；密集采样（信令/GPS）下方可判真实停留"),
        "coord_precision": coord_p,
        "trackpoint_total": len(pts), "trackpoint_usable": len(usable),
        "degraded": False, "degraded_reason": None,
    }


def _dt_iso(s: str):
    from datetime import datetime
    return datetime.fromisoformat(s)


# ----------------------------------------------------------------------
# Function 7：geo_anomaly_trajectory —— 常驻基线之上的轨迹偏离
# ----------------------------------------------------------------------

def _hours_covered(start_iso: str, end_iso: str) -> set:
    """一段停留覆盖的整点小时集合（稀疏采样下只作粗粒度时段画像）。"""
    from datetime import datetime, timedelta
    a = datetime.fromisoformat(start_iso)
    b = datetime.fromisoformat(end_iso)
    if b < a:
        a, b = b, a
    cur = a.replace(minute=0, second=0, microsecond=0)
    out = set()
    while cur <= b and len(out) < 25:
        out.add(cur.hour)
        cur += timedelta(hours=1)
    return out


def _co_presence_index(store, ctx, limit: int = 5000) -> dict:
    """{(日期, 地点键): set(主体名)} —— 用于"异常停留点上还有谁"。

    为什么要看共现
    --------------
    "某人去了不常去的地方"只是弱信号；"他去的那个不常去的地方，另一个
    人同一天也在"才是研判抓手。共现不改变异常判定本身，只作为附加证据
    列出——是否构成接触仍由正兵核查，此处不出定性。
    """
    try:
        rows = store.query(_site_rows_sql(ctx, "", store))
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return {}
    idx: dict = {}
    for r in (rows or [])[:limit]:
        d = r.get("d")
        loc = r.get("location_id") or r.get("std_address")
        p = r.get("person_raw")
        if d is None or loc is None or not p:
            continue
        idx.setdefault((str(d), str(loc)), set()).add(str(p))
    return idx


# ----------------------------------------------------------------------
# 主体引用归一（三维联动的地基）
# ----------------------------------------------------------------------
def _person_refs(store, names: list[str]) -> dict:
    """批量解析主体引用 {name: {pk, ambiguous, candidates, ...}}。

    为什么必须在产出层做
    --------------------
    空间产出一直写姓名（"张卫国"），关系产出写代理键
    （"person_ecb52c3719fc"）。两把钥匙开不了同一把锁，于是"点地图
    上的点 → 关系视图高亮对应的人"只能靠手工 join——联动是假的。
    代理键规则与关系侧完全一致（person_<sha1(姓名)[:12]>），一次哈希
    即可对齐，不需要查表 join。

    失败不阻断：裁决层不可用时退化为按名归一，返回结构不变，调用方
    无需分支——空间结论照常产出，只是联动降级。
    """
    try:
        from core import entity_ref
    except Exception:
        return {}
    norm = [str(n) for n in (names or []) if n]
    if not norm:
        return {}
    conn = getattr(store, "conn", None)
    err = None
    try:
        res = entity_ref.resolve_person(norm, conn=conn)
    except Exception as e:      # noqa: BLE001 - 裁决失败不阻断空间结论
        err = repr(e)[:200]
        try:
            res = entity_ref.resolve_person(norm)
        except Exception:
            return {}
    # 一致性自陈：裁决层没给出任何 verdict（身份表缺失/不可用/裁决异常）
    # 时，本次是按名哈希兜底——同名异人会被**静默并成一个**，正是本模块
    # 要防的红线。不猜测、也不假装可靠，标出来让不一致可见。
    if not any((v or {}).get("verdict") for v in res.values()):
        for v in res.values():
            v["ref_source"] = (
                f"hash_fallback(store={type(store).__name__},"
                f"has_conn={conn is not None}"
                + (f",err={err}" if err else "") + ")")
    return res


def _attach_ref(target: dict, name: Any, *, persons: dict) -> dict:
    """往产出条目上挂三件套（纯新增字段，向后兼容）。"""
    try:
        from core import entity_ref
    except Exception:
        return target
    return entity_ref.attach_person_ref(target, name, persons=persons)


def _ref_dict(name: Any, *, persons: dict) -> dict:
    """单个主体的引用字典——用于列表字段（co_present_refs 等）。"""
    try:
        from core import entity_ref
    except Exception:
        return {"person_name": str(name or "") or None, "person_pk": None,
                "person_pk_ambiguous": False}
    return entity_ref.person_ref_dict(name, persons=persons)


def geo_anomaly_trajectory(store, params: dict, ctx=None) -> dict:
    """在**主体自身常驻基线**之上识别偏离（异常轨迹检测）。

    为什么必须先有基线
    ------------------
    "异常"是相对概念：不知道 habitual 是什么，就判不出偏离。这与通话
    高频规则当年因无常态基线、只能退而判"高频"是同一个缺陷。基线来自
    geo_trajectory_segment 的停留段——没有停留段（轨迹无时刻）就没有基线，
    此时**不产出任何异常**，而不是把每个地点都当成异常（样本越少，每个
    地点都「只去过一次」，那样全表皆异常，等于没有检测）。

    为什么"异常"不等于"可疑"
    ------------------------
    偏离常驻模式是结构事实，不是定性结论。临时出差、职务性外勤、采样
    缺失都会造成偏离。产出只陈述"偏离了什么、多少次"，并把这些成因写
    进证伪条件交给正兵核查——标题不出现"可疑""疑似"。

    三类偏离
    --------
    off_route  非常驻地点：到访天数占比低于 rare_ratio
    off_hours  常驻地点，但本次时段不在该地点的 habitual 时段内
    off_path   移动段起止组合不在 habitual 通勤组合内（样本足够才判）
    """
    from core.graph import resolve_subject

    target = (params.get("target") or params.get("target_subject") or "").strip()
    if not target:
        return _degraded_result("缺少 target 参数（异常轨迹检测须指定主体）",
                                subject=None, baseline={}, anomalies=[],
                                anomaly_count=0)

    def _int(v, default):
        return default if v in (None, "") else int(v)
    rare_ratio = float(params.get("rare_ratio") or 0.2)
    min_days = _int(params.get("min_baseline_days"), 7)
    max_out = _int(params.get("max_anomalies"), 50)
    seg_params = {"target": target,
                  "target_type": params.get("target_type", "auto")}
    for k, dflt in (("stay_radius_m", 200), ("stay_min_minutes", 30)):
        if params.get(k) not in (None, ""):
            seg_params[k] = _int(params.get(k), dflt)

    seg = geo_trajectory_segment(store, seg_params, ctx)
    subject = seg.get("subject")
    if subject is None:
        return _degraded_result(
            f"主体 {target!r} 在语义层实体中不存在",
            subject=None, baseline={}, anomalies=[], anomaly_count=0)
    if seg.get("degraded"):
        # 分段降级即基线不可建：透传原因，不另造一份"异常"。
        return _degraded_result(
            "异常检测依赖轨迹分段，分段不可用：" + str(seg.get("degraded_reason")),
            subject=subject, baseline={}, anomalies=[], anomaly_count=0,
            segmentation=seg)

    stays = seg.get("stays") or []
    moves = seg.get("moves") or []
    if not stays:
        return _degraded_result("无可停留段，无法建立常驻基线",
                                subject=subject, baseline={}, anomalies=[],
                                anomaly_count=0, segmentation=seg)

    def _key(s: dict) -> str:
        return str(s.get("location_id") or s.get("std_address") or "?")

    # ---- 建基线：到访天数 / 次数 / 累计时长 / 时段画像 ----
    site: dict = {}
    all_days: set = set()
    for s in stays:
        day = s["start"][:10]
        all_days.add(day)
        k = _key(s)
        e = site.setdefault(k, {"location_id": s.get("location_id"),
                                "std_address": s.get("std_address"),
                                "lat": s.get("lat"), "lng": s.get("lng"),
                                "days": set(), "visits": 0,
                                "minutes": 0.0, "hours": {}})
        e["days"].add(day)
        e["visits"] += 1
        e["minutes"] += float(s.get("duration_minutes") or 0)
        for h in _hours_covered(s["start"], s["end"]):
            e["hours"][h] = e["hours"].get(h, 0) + 1

    total_days = max(len(all_days), 1)
    if len(all_days) < min_days:
        return _degraded_result(
            f"停留仅覆盖 {len(all_days)} 天，低于基线下限 {min_days} 天，"
            "常驻模式不可靠（样本越少越容易出现「只去过一次」的伪异常）",
            subject=subject,
            baseline={"days": len(all_days), "min_baseline_days": min_days},
            anomalies=[], anomaly_count=0, segmentation=seg)

    habitual_sites, anomalies = [], []
    for k, e in site.items():
        ratio = round(len(e["days"]) / total_days, 3)
        # habitual 时段只取出现 ≥2 次的小时：出现一次的小时是噪声，
        # 拿它当基线会把"第一次晚归"误判成常态外。
        hab_hours = {h for h, c in e["hours"].items() if c >= 2}
        is_habitual = ratio >= rare_ratio
        rec = {"location_id": e["location_id"], "std_address": e["std_address"],
               "lat": e["lat"], "lng": e["lng"],
               "day_ratio": ratio, "visit_days": len(e["days"]),
               "visits": e["visits"],
               "total_minutes": round(e["minutes"], 1),
               "habitual": is_habitual,
               "habitual_hours": sorted(hab_hours) if is_habitual else []}
        habitual_sites.append(rec)
        if is_habitual:
            continue
        for s in stays:
            if _key(s) != k:
                continue
            anomalies.append({
                "kind": "off_route",
                "location_id": s.get("location_id"),
                "std_address": s.get("std_address"),
                "date": s["start"][:10],
                "start": s["start"], "end": s["end"],
                "duration_minutes": s["duration_minutes"],
                "day_ratio": ratio,
                "reason": f"该地点在 {total_days} 个停留日中仅出现 "
                          f"{len(e['days'])} 天（占比 {ratio}），低于常驻阈值 "
                          f"{rare_ratio}",
                "event_pks": s.get("event_pks") or [],
            })

    # ---- off_hours：常驻地点上的非常态时段 ----
    hab_of = {r["location_id"] or r["std_address"]: r for r in habitual_sites
              if r["habitual"]}
    for s in stays:
        rec = hab_of.get(s.get("location_id") or s.get("std_address"))
        if not rec or not rec["habitual_hours"] or rec["visits"] < 3:
            continue
        hs = _hours_covered(s["start"], s["end"])
        if hs and not (hs & set(rec["habitual_hours"])):
            anomalies.append({
                "kind": "off_hours",
                "location_id": s.get("location_id"),
                "std_address": s.get("std_address"),
                "date": s["start"][:10],
                "start": s["start"], "end": s["end"],
                "duration_minutes": s["duration_minutes"],
                "habitual_hours": rec["habitual_hours"],
                "reason": f"常驻地点（占比 {rec['day_ratio']}），但本次时段 "
                          f"{sorted(hs)} 与该地点常驻时段 "
                          f"{rec['habitual_hours']} 无重叠",
                "event_pks": s.get("event_pks") or [],
            })

    # ---- off_path：非常态通勤组合 ----
    # 移动样本太少时 habitual 通勤组合不可靠（每段都只出现一次），
    # 此时不判——否则所有移动段都是"异常"。
    if len(moves) >= 3:
        combo: dict = {}
        for m in moves:
            combo[(m.get("from"), m.get("to"))] = \
                combo.get((m.get("from"), m.get("to")), 0) + 1
        for m in moves:
            if combo.get((m.get("from"), m.get("to")), 0) >= 2:
                continue
            anomalies.append({
                "kind": "off_path",
                "from": m.get("from"), "to": m.get("to"),
                "date": (m.get("start") or "")[:10],
                "start": m.get("start"), "end": m.get("end"),
                "duration_minutes": m.get("duration_minutes"),
                "distance_m": m.get("distance_m"),
                "distance_note": m.get("distance_note"),
                "reason": f"移动组合「{m.get('from')}→{m.get('to')}」在 "
                          f"{len(moves)} 段移动中仅出现 1 次，非常态通勤",
            })

    # ---- 共现：异常停留点上还有谁 ----
    idx = _co_presence_index(store, ctx)
    me = str(subject.get("name") or "")
    for a in anomalies:
        if a["kind"] not in ("off_route", "off_hours"):
            continue
        others = idx.get((a["date"], str(a.get("location_id") or
                                         a.get("std_address"))), set())
        others = {p for p in others if p != me}
        if others:
            a["co_present"] = sorted(others)
            a["co_present_note"] = (
                "同一天同一地点另有上述主体出现；是否构成接触待核查")

    # ---- 主体引用归一：姓名 → 代理键（三维联动的结构性引用）----
    # 重名（两个张卫国）时 pk 置空并标 ambiguous，绝不在两个候选里
    # 静默挑一个——挑错了，后面所有联动都建在错误的那个人身上。
    _names = {me} | {p for a in anomalies for p in (a.get("co_present") or [])}
    persons = _person_refs(store, sorted(_names))
    for a in anomalies:
        _attach_ref(a, me, persons=persons)
        if a.get("co_present"):
            a["co_present_refs"] = [_ref_dict(p, persons=persons)
                                    for p in a["co_present"]]
    _me_ref = persons.get(me) or {}

    anomalies.sort(key=lambda a: (a["kind"], a.get("start") or ""))
    anomalies = anomalies[:max_out]
    by_kind: dict = {}
    for a in anomalies:
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1

    return {
        "hit": bool(anomalies),
        "subject": subject,
        # 主体引用：person_pk 供关系/时间维度直接引用；重名时 None +
        # ambiguous=True，候选见 person_pk_candidates，待人工裁决后落
        # entity_mapping 才可用。原名 person_name 保留用于展示。
        "person_pk": _me_ref.get("pk"),
        "person_name": me,
        "person_pk_ambiguous": bool(_me_ref.get("ambiguous")),
        "person_pk_candidates": (_me_ref.get("candidates")
                                 if _me_ref.get("ambiguous") else None),
        "person_refs": {k: {"pk": v.get("pk"),
                            "ambiguous": v.get("ambiguous")}
                        for k, v in persons.items()},
        "baseline": {
            "days": total_days,
            "stay_count": len(stays),
            "move_count": len(moves),
            "site_count": len(site),
            "habitual_sites": sorted(habitual_sites,
                                     key=lambda r: -r["day_ratio"]),
            "rare_ratio": rare_ratio,
            "min_baseline_days": min_days,
        },
        "anomalies": anomalies,
        "anomaly_count": len(anomalies),
        "by_kind": by_kind,
        "coord_precision": seg.get("coord_precision"),
        "anomaly_note": ("偏离常驻模式是结构事实，不等于可疑：临时出差、"
                         "职务性外勤、采样缺失均会造成偏离，须正兵核查"),
        "falsification": ("若偏离系临时出差/职务性外勤/数据补录，或该地点本就"
                          "属其工作范围，则偏离不成立"),
        "segmentation": seg,
        "degraded": False, "degraded_reason": None,
    }


# ----------------------------------------------------------------------
# Function 8：geo_activity_range —— 平均中心 / 标准差椭圆 / 核密度
# ----------------------------------------------------------------------

_M_PER_DEG_LAT = 111320.0


def _project_xy(lat, lng, lat0, lng0):
    """经纬度 → 局部米平面（等距圆柱近似，在 lat0 处定标）。"""
    import math
    kx = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return ((lng - lng0) * kx, (lat - lat0) * _M_PER_DEG_LAT)


def _unproject_xy(x, y, lat0, lng0):
    import math
    kx = _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return (lat0 + y / _M_PER_DEG_LAT, lng0 + x / kx)


def _weighted_ellipse(items):
    """加权标准差椭圆（Yuill 1971 的加权形式）。

    items: [(x, y, w)]。主轴方位角用 **GIS 惯例**（正北 0°、顺时针），而不
    是数学惯例（正东 0°、逆时针）——研判员按方位角描述"活动范围朝哪个方向
    延展"，用后者会把"南北向延展"读成"东西向"。
    """
    import math
    W = sum(w for _, _, w in items) or 1.0
    xb = sum(x * w for x, _, w in items) / W
    yb = sum(y * w for _, y, w in items) / W
    sxx = sum(w * (x - xb) ** 2 for x, _, w in items) / W
    syy = sum(w * (y - yb) ** 2 for _, y, w in items) / W
    sxy = sum(w * (x - xb) * (y - yb) for x, y, w in items) / W
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    half = (sxx + syy) / 2.0
    root = math.sqrt(max(((sxx - syy) / 2.0) ** 2 + sxy ** 2, 0.0))
    a = math.sqrt(max(half + root, 0.0))
    b = math.sqrt(max(half - root, 0.0))
    return {"cx": xb, "cy": yb,
            "sigma_major": a, "sigma_minor": b,
            "theta_rad": theta,
            "azimuth_deg": (90.0 - math.degrees(theta)) % 180.0,
            "flattening": (1.0 - b / a) if a > 0 else None,
            "std_distance": math.sqrt(sxx + syy)}


def _kde_density(items, x, y, h_m):
    """点 (x, y) 处的高斯核密度（按权重归一，4h 外截断）。"""
    import math
    W = sum(w for _, _, w in items) or 1.0
    two_h2 = 2.0 * h_m * h_m
    cut = (4.0 * h_m) ** 2
    acc = 0.0
    for px, py, w in items:
        d2 = (x - px) ** 2 + (y - py) ** 2
        if d2 > cut:
            continue
        acc += w * math.exp(-d2 / two_h2)
    return acc / W


def geo_activity_range(store, params: dict, ctx=None) -> dict:
    """活动范围画像：平均中心 / 标准距离 / 标准差椭圆 / 核密度热力图。

    为什么需要这一层
    ----------------
    CGT 回答"落脚点在哪"（单峰概率面，假设存在唯一犯罪据点），活动范围回答
    "这个人在多大范围里活动、朝哪个方向延展、密度集中在哪"。后者是**描述性
    的**，不预设单一据点——公职人员的空间行为本就不是"掠夺者型"，用 CGT 建模
    尺度错配（实测 5 个事件点全为孤岛被自检拦下），椭圆与核密度不需要那个
    假设即可成立。

    三处必须自陈
    ------------
    1. 坐标精度：centroid 档下距离/密度是区级推算，不代表实际间距；去重后
       位置数不足 3 时椭圆与核密度退化为一个点，不产出。
    2. 权重口径：默认按到访次数加权（频率加权平均中心，活动空间研究通行
       口径），uniform 可关闭。
    3. 椭圆倍率：1σ 椭圆对二元正态仅约含 39% 的点，2σ 约 86%。倍率写进
       产出，不让人把 1σ 椭圆当成"活动边界"。
    """
    import math
    from core.graph import resolve_subject

    target = (params.get("target") or params.get("target_subject") or "").strip()
    if not target:
        return _degraded_result("缺少 target 参数（活动范围须指定主体）",
                                subject=None, point_count=0)
    subject = resolve_subject(store, target, params.get("target_type", "auto"), ctx)
    if subject is None:
        return _degraded_result(f"主体 {target!r} 在语义层实体中不存在",
                                subject=None, point_count=0)

    def _int(v, default):
        return default if v in (None, "") else int(v)

    def _flt(v, default):
        return default if v in (None, "") else float(v)

    grid_n = max(8, min(_int(params.get("grid_size"), 40), 80))
    h_m = max(50.0, _flt(params.get("bandwidth_m"), 500.0))
    sigma_mult = max(0.5, min(_flt(params.get("sigma_multiplier"), 2.0), 3.0))
    min_points = max(3, _int(params.get("min_points"), 3))
    top_n = max(1, min(_int(params.get("top_hotspots"), 5), 20))
    weight_by = (params.get("weight_by") or "count").strip()

    where = "WHERE t.person_raw = ? "
    args = [subject["name"]]
    dfrom = (params.get("date_from") or "").strip()
    dto = (params.get("date_to") or "").strip()
    if dfrom and dto:
        where += "AND CAST(t.date AS DATE) BETWEEN ? AND ? "
        args += [dfrom, dto]
    elif dfrom:
        where += "AND CAST(t.date AS DATE) >= ? "
        args.append(dfrom)
    elif dto:
        where += "AND CAST(t.date AS DATE) <= ? "
        args.append(dto)

    try:
        rows = store.query(_site_rows_sql(ctx, where, store), tuple(args))
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            subject=subject, point_count=0)

    buckets: dict = {}
    for r in rows or []:
        lat, lng = r.get("lat"), r.get("lng")
        if lat is None or lng is None:
            continue
        key = r.get("location_id") or f"{lat},{lng}"
        b = buckets.setdefault(key, {
            "location_id": r.get("location_id"),
            "std_address": r.get("std_address") or r.get("raw_location"),
            "lat": lat, "lng": lng,
            "cprec": _coord_precision(r), "visits": 0,
            "first_date": None, "last_date": None})
        b["visits"] += 1
        d = r.get("d")
        if d is not None:
            s = str(d)
            if b["first_date"] is None or s < b["first_date"]:
                b["first_date"] = s
            if b["last_date"] is None or s > b["last_date"]:
                b["last_date"] = s

    pts = sorted(buckets.values(), key=lambda b: -b["visits"])
    total_events = len(rows or [])
    if not pts:
        return _degraded_result(
            f"{subject['name']} 的 {total_events} 条轨迹全部无坐标，"
            "活动范围统计不成立（需先完成地理编码）",
            subject=subject, point_count=0, trackpoint_total=total_events)
    if len(pts) < min_points:
        return _degraded_result(
            f"有效坐标点仅 {len(pts)} 个（下限 {min_points}），"
            "平均中心与椭圆统计不成立",
            subject=subject, point_count=len(pts),
            trackpoint_total=total_events)

    uniq = {(round(b["lat"], 6), round(b["lng"], 6)) for b in pts}
    if len(uniq) < 3:
        return _degraded_result(
            f"坐标去重后仅 {len(uniq)} 个不同位置（多为区划质心重合），"
            "椭圆与核密度退化为一个点，不成立",
            subject=subject, point_count=len(pts),
            trackpoint_total=total_events, coord_precision="centroid")

    lat0 = sum(b["lat"] for b in pts) / len(pts)
    lng0 = sum(b["lng"] for b in pts) / len(pts)
    xy = []
    for b in pts:
        x, y = _project_xy(b["lat"], b["lng"], lat0, lng0)
        b["x"], b["y"] = x, y
        xy.append((x, y, float(1 if weight_by == "uniform" else b["visits"])))

    ell = _weighted_ellipse(xy)
    clat, clng = _unproject_xy(ell["cx"], ell["cy"], lat0, lng0)

    a_m = ell["sigma_major"] * sigma_mult
    b_m = ell["sigma_minor"] * sigma_mult
    theta = ell["theta_rad"]
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    poly = []
    for k in range(64):
        t = 2.0 * math.pi * k / 64.0
        ex = a_m * math.cos(t)
        ey = b_m * math.sin(t)
        rx = ell["cx"] + ex * cos_t - ey * sin_t
        ry = ell["cy"] + ex * sin_t + ey * cos_t
        la, ln = _unproject_xy(rx, ry, lat0, lng0)
        poly.append({"lat": round(la, 6), "lng": round(ln, 6)})

    xs = [b["x"] for b in pts]
    ys = [b["y"] for b in pts]
    pad = h_m
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad
    dx = (x1 - x0) / (grid_n - 1) if x1 > x0 else 1.0
    dy = (y1 - y0) / (grid_n - 1) if y1 > y0 else 1.0
    cells, mx = [], 0.0
    for i in range(grid_n):
        for j in range(grid_n):
            x = x0 + i * dx
            y = y0 + j * dy
            d = _kde_density(xy, x, y, h_m)
            if d > mx:
                mx = d
            la, ln = _unproject_xy(x, y, lat0, lng0)
            cells.append({"lat": round(la, 6), "lng": round(ln, 6),
                          "density": d})
    # 点位密度可能高于任何网格采样值——网格未必恰好采到峰位。峰值须取「网格
    # 最大值」与「点位密度」的较大者，否则热点密度会 >1（实测张卫国热点出现
    # 1.2114），与"0-1 相对值"的自陈自相矛盾。
    pt_dens = [_kde_density(xy, b["x"], b["y"], h_m) for b in pts]
    peak = max([mx] + pt_dens) if pt_dens else mx
    for c in cells:
        c["density"] = round(c["density"] / peak, 4) if peak > 0 else 0.0

    hotspots = []
    for b, dens in zip(pts, pt_dens):
        hotspots.append({"location_id": b["location_id"],
                         "std_address": b["std_address"],
                         "lat": round(b["lat"], 6), "lng": round(b["lng"], 6),
                         "visits": b["visits"],
                         "first_date": b["first_date"],
                         "last_date": b["last_date"],
                         "density": round(dens / peak, 4) if peak > 0 else 0.0})
    hotspots.sort(key=lambda h: (-h["density"], -h["visits"]))
    hotspots = hotspots[:top_n]

    coord_p = "centroid" if any(b["cprec"] == "centroid" for b in pts) else "exact"
    containment = round((1.0 - math.exp(-(sigma_mult ** 2) / 2.0)) * 100, 1)

    return {
        "hit": True,
        "subject": subject,
        "point_count": len(pts),
        "trackpoint_total": total_events,
        "date_from": dfrom or None, "date_to": dto or None,
        "mean_center": {"lat": round(clat, 6), "lng": round(clng, 6)},
        "standard_distance_m": round(ell["std_distance"], 1),
        "std_ellipse": {
            "center": {"lat": round(clat, 6), "lng": round(clng, 6)},
            "semi_major_m": round(a_m, 1), "semi_minor_m": round(b_m, 1),
            "sigma_major_m": round(ell["sigma_major"], 1),
            "sigma_minor_m": round(ell["sigma_minor"], 1),
            "azimuth_deg": round(ell["azimuth_deg"], 1),
            "flattening": (round(ell["flattening"], 3)
                           if ell["flattening"] is not None else None),
            "sigma_multiplier": sigma_mult,
            "containment_note": (
                f"{sigma_mult:g}σ 椭圆对二元正态约含 {containment}% 的点"
                "（1σ≈39%、2σ≈86%、3σ≈99%），倍率越大越接近活动边界，"
                "但都不是绝对边界——稀疏采样下椭圆外仍可能有活动"),
            "area_km2": round(math.pi * a_m * b_m / 1e6, 3),
            "polygon": poly,
        },
        "kde": {
            "bandwidth_m": round(h_m, 1),
            "grid_size": grid_n,
            "cell_size_m": round(max(dx, dy), 1),
            "cells": cells,
            "density_note": "密度为按权重归一的高斯核密度（0-1 相对值），"
                            "不是绝对概率；带宽越大越平滑、越小越贴近点位",
        },
        "hotspots": hotspots,
        "bbox": {"min_lat": round(min(b["lat"] for b in pts), 6),
                 "max_lat": round(max(b["lat"] for b in pts), 6),
                 "min_lng": round(min(b["lng"] for b in pts), 6),
                 "max_lng": round(max(b["lng"] for b in pts), 6)},
        "span_km": round(math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 1000.0, 2),
        "weight_by": weight_by,
        "coord_precision": coord_p,
        "coord_note": (None if coord_p == "exact" else
                       "坐标含区划质心，距离与密度为区级推算，不代表实际间距"),
        "small_sample_note": (
            "样本不足 5 个落脚点，椭圆由少量点位近似确定、稳定性弱，"
            "宜结合落点明细与到访次数判读" if len(pts) < 5 else None),
        "degraded": False, "degraded_reason": None,
    }
