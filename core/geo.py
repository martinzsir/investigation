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
