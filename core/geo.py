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
def _site_rows_sql(ctx, where: str = "") -> str:
    t_tp = _tbl(ctx, "trackpoint")
    t_ta = _lnk(ctx, "trackpoint_at")
    t_loc = _tbl(ctx, "location")
    return (
        f'SELECT t.track_id, t.person_raw, CAST(t.date AS DATE) AS d, '
        f't.location AS raw_location, l.location_id, l.std_address, '
        f'l.lat, l.lng, l.coord_sys, l.province, l.prefecture, l.county, '
        f'l.township, l.admin_code, l.geocode_source, l.geocode_confidence '
        f'FROM {t_tp} t '
        f'LEFT JOIN {t_ta} ta ON ta.track_id = t.track_id '
        f'LEFT JOIN {t_loc} l ON l.location_id = ta.location_id '
        f'{where}')


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
            _site_rows_sql(ctx, "WHERE t.person_raw = ? ")
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
        rows = store.query(_site_rows_sql(ctx)
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

    anchor = None
    subject = None
    if clat is not None and clng is not None:
        anchor = {"lat": float(clat), "lng": float(clng), "source": "explicit",
                  "sites_used": None}
    elif target:
        from core.graph import resolve_subject
        subject = resolve_subject(store, target,
                                  params.get("target_type", "auto"), ctx)
        if subject is None:
            return _degraded_result(
                f"主体 {target!r} 在语义层实体中不存在（未建档或未归一）",
                anchor=None, inner_events=[], ring_events=[])
        sites_res = geo_subject_sites(store, params, ctx)
        coord_sites = [s for s in sites_res.get("sites", [])
                       if not s["coord_degraded"]]
        if not coord_sites:
            return _degraded_result(
                f"主体 {subject['name']} 无带坐标落脚点，锚点不可计算",
                subject=subject, anchor=None, inner_events=[], ring_events=[])
        anchor = {"lat": sum(s["lat"] for s in coord_sites) / len(coord_sites),
                  "lng": sum(s["lng"] for s in coord_sites) / len(coord_sites),
                  "source": f"subject_centroid:{subject['name']}",
                  "sites_used": len(coord_sites)}
    else:
        return _degraded_result(
            "缺少锚点（target 主体或 center_lat/center_lng 须给其一）",
            anchor=None, inner_events=[], ring_events=[])

    try:
        rows = store.query(_site_rows_sql(ctx) + " ORDER BY d, t.track_id")
    except Exception as e:
        if not _is_semantic_gap(e):
            raise
        return _degraded_result(
            f"空间语义表缺失/不符：{str(e).splitlines()[0][:120]}",
            subject=subject, anchor=anchor, inner_events=[], ring_events=[])

    inner_events: list[dict] = []
    ring_events: list[dict] = []
    outside_count = 0
    no_coord_count = 0
    ac = (anchor["lat"], anchor["lng"])
    for r in rows:
        if r["lat"] is None or r["lng"] is None:
            no_coord_count += 1
            continue
        dist = _haversine_m(ac, (r["lat"], r["lng"]))
        if dist > outer:
            outside_count += 1
            continue
        ev = {"subject_raw": r["person_raw"],
              "event_pk": str(r["track_id"]),
              "date": r["d"].isoformat() if r["d"] else None,
              "location_id": r["location_id"],
              "std_address": r["std_address"] or r["raw_location"],
              "lat": r["lat"], "lng": r["lng"],
              "distance_m": round(dist, 1)}
        (inner_events if dist <= inner else ring_events).append(ev)
    inner_events.sort(key=lambda e: (e["distance_m"], e["event_pk"]))
    ring_events.sort(key=lambda e: (e["distance_m"], e["event_pk"]))
    return {
        "hit": bool(ring_events),
        "subject": subject,
        "anchor": anchor,
        "inner_radius_m": inner,
        "outer_radius_m": outer,
        "inner_events": inner_events[:150],
        "ring_events": ring_events[:150],
        "inner_count": len(inner_events),
        "ring_count": len(ring_events),
        "outside_count": outside_count,
        "no_coord_count": no_coord_count,
        "truncated": len(inner_events) > 150 or len(ring_events) > 150,
        "degraded": no_coord_count > 0,
        "degraded_reason": (
            f"{no_coord_count} 起事件无坐标未纳入环带扫描"
            if no_coord_count else None),
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
        sql = _site_rows_sql(ctx, "WHERE t.person_raw = ? ")
        sql_params = (subject["name"],)
    else:
        sql = _site_rows_sql(ctx, "WHERE t.person_raw IS NOT NULL ")
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
    out = {"hit": hit, "subject": subject, "profiles": profiles,
           "profiled_count": sum(1 for p in profiles if p.get("profiled")),
           "min_events": min_events, "grid_meters": grid_meters,
           "buffer_m": buffer_m,
           "degraded": any(p.get("degraded") for p in profiles),
           "degraded_reason": (
               "部分主体有效事件不足或坐标缺失（各 profiles 条目自带原因）"
               if any(p.get("degraded") for p in profiles) else None)}
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
