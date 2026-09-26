#!/usr/bin/env python3
"""
scripts/geocode_locations.py —— 三轨地理编码 CLI 工具

轨道路由：
  - 默认（仅 --offline）：区划离线匹配，坐标=县级质心（confidence=0.7）
  - 加 --online（需 key）：高德在线优先（门牌级，confidence=0.9），
    在线无结果时回落离线质心；两者皆空则跳过
  - 人工标注（manual）：预留，不落本脚本

输出：data/geocode_cache.parquet（列：raw_address/std_address/lat/lng/coord_sys/source/confidence/geocoded_at）
内核编译时 optional JOIN；未编码地址 NULL 降级，不报错。

用法：
  python scripts/geocode_locations.py --offline          # 仅离线轨（默认）
  python scripts/geocode_locations.py --online --amap-key XXX  # 离线+在线（需授权）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.geo import parse_admin_path, dual_segment_key  # noqa: E402

GEOCODE_CACHE = ROOT / "data" / "geocode_cache.parquet"


def load_pending_addresses() -> pd.DataFrame:
    """从 obj_trackpoint 提取待编码地址（去重）。"""
    import duckdb
    db_path = ROOT / "data" / "investigation.duckdb"
    if not db_path.exists():
        print(f"ERROR: {db_path} not found")
        return pd.DataFrame(columns=["raw_address"])
    con = duckdb.connect(str(db_path), read_only=True)
    df = con.execute("""
        SELECT DISTINCT location AS raw_address
        FROM obj_trackpoint
        WHERE location IS NOT NULL AND location <> ''
    """).fetchdf()
    con.close()
    return df


def load_existing_cache() -> pd.DataFrame:
    """加载已有编码缓存（增量模式）。"""
    if GEOCODE_CACHE.exists():
        return pd.read_parquet(GEOCODE_CACHE)
    return pd.DataFrame(columns=[
        "raw_address", "std_address", "lat", "lng", "coord_sys",
        "source", "confidence", "geocoded_at"
    ])


def geocode_offline(raw_address: str) -> dict:
    """轨道 1：区划离线匹配。"""
    result = parse_admin_path(raw_address)
    if not result:
        return {}
    # 优先用最深层级坐标（乡镇 > 县 > 地 > 省）
    lng = result.get("lng")
    lat = result.get("lat")
    if lng is None or lat is None:
        return {}
    return {
        "raw_address": raw_address,
        "std_address": dual_segment_key(raw_address),
        "lat": float(lat),
        "lng": float(lng),
        "coord_sys": "GCJ-02",
        "source": "admin_offline",
        "confidence": result.get("confidence", 0.7),
        "geocoded_at": datetime.now().isoformat(),
    }


def geocode_amap(raw_address: str, key: str) -> dict:
    """轨道 2：高德在线（门牌级精度）。"""
    import urllib.request
    import urllib.parse
    url = f"https://restapi.amap.com/v3/geocode/geo?address={urllib.parse.quote(raw_address)}&key={key}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "1" or not data.get("geocodes"):
            return {}
        geo = data["geocodes"][0]
        location = geo.get("location", "")
        if not location:
            return {}
        lng, lat = map(float, location.split(","))
        return {
            "raw_address": raw_address,
            "std_address": dual_segment_key(raw_address),
            "lat": lat,
            "lng": lng,
            "coord_sys": "GCJ-02",
            "source": "amap_online",
            "confidence": 0.9,
            "geocoded_at": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"  WARN: amap geocode failed for '{raw_address}': {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="三轨地理编码工具")
    parser.add_argument("--offline", action="store_true", default=True,
                        help="轨道 1：区划离线匹配（默认）")
    parser.add_argument("--online", action="store_true",
                        help="轨道 2：高德在线编码（需 --amap-key）")
    parser.add_argument("--amap-key", type=str, default="",
                        help="高德 Web 服务 Key")
    parser.add_argument("--force", action="store_true",
                        help="强制重新编码所有地址（默认增量）")
    args = parser.parse_args()

    if args.online and not args.amap_key:
        print("ERROR: --online 需要 --amap-key")
        sys.exit(1)

    # 加载待编码地址
    pending = load_pending_addresses()
    if pending.empty:
        print("No pending addresses")
        return

    # 增量模式：过滤已编码
    existing = load_existing_cache()
    if not args.force and not existing.empty:
        pending = pending[~pending["raw_address"].isin(existing["raw_address"])]

    print(f"待编码地址: {len(pending)} 个（online={args.online}）")

    results = []
    for addr in pending["raw_address"]:
        r = None
        # 授权在线时：高德门牌级优先（QPS 限速，10021 CUQPS）
        if args.online:
            r = geocode_amap(addr, args.amap_key)
            if r:
                results.append(r)
                print(f"  [online]  {addr} → ({r['lng']:.4f}, {r['lat']:.4f})")
            else:
                print(f"  [online-miss] {addr} → 回落离线")
            time.sleep(0.3)
        # 离线轨：默认模式直接走；在线未命中时兜底
        if r is None:
            r = geocode_offline(addr)
            if r:
                results.append(r)
                print(f"  [offline] {addr} → ({r['lng']:.4f}, {r['lat']:.4f})")
            else:
                print(f"  [skip]    {addr} (no result)")

    if not results:
        print("No addresses encoded")
        return

    # 合并写入缓存
    new_df = pd.DataFrame(results)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined.to_parquet(GEOCODE_CACHE, index=False)
    print(f"\n缓存已更新: {GEOCODE_CACHE}")
    print(f"总缓存条数: {len(combined)}")
    print(f"  admin_offline: {len(combined[combined['source'] == 'admin_offline'])}")
    print(f"  amap_online:   {len(combined[combined['source'] == 'amap_online'])}")


if __name__ == "__main__":
    main()
