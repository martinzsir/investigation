#!/usr/bin/env python3
"""
scripts/enrich_location.py —— 地点实体物化后处理

在 build_ontology 物化 obj_location 之后执行：
  1) AdminMatcher 离线匹配 → 填充 province/prefecture/county/township/admin_code/confidence
  2) dual_segment_key 修正 std_address（路名主干+行政区划双段归并）
  3) geocode_cache 二次 JOIN（可选）：填充 lat/lng/geocode_source/geocode_confidence/geocoded_at
     优先级：geocode_cache（高德在线）> AdminMatcher（区划离线）> raw_fallback（无坐标）
  4) 兜底：raw_address 为 NULL 的行直接删除（防御，source_sql 已过滤）

设计红线：
  - 只读 obj_location + geocode_cache，UPDATE obj_location（唯一写目标）
  - 不修改编译器核心代码（解耦物化与富化）
  - 无 geocode_cache 时不报错，降级为 admin_offline/raw_fallback
"""
from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.geo import parse_admin_path, dual_segment_key  # noqa: E402


def load_geocode_cache() -> pd.DataFrame:
    """加载地理编码缓存（可选）。"""
    cache_path = ROOT / "data" / "geocode_cache.parquet"
    if not cache_path.exists():
        return pd.DataFrame(columns=[
            "raw_address", "std_address", "lat", "lng", "coord_sys",
            "source", "confidence", "geocoded_at"
        ])
    return pd.read_parquet(cache_path)


def enrich_locations(db_path: str = "data/investigation.duckdb") -> dict:
    """主入口：富化 obj_location。

    返回统计信息：{total, offline_matched, cache_matched, fallback}
    """
    import duckdb
    con = duckdb.connect(db_path)
    try:
        # 检查 obj_location 是否存在
        tables = con.execute("SHOW TABLES").fetchall()
        if ("obj_location",) not in tables:
            print("obj_location not found, skip enrich")
            return {}

        # 加载现有地点
        df = con.execute("SELECT * FROM obj_location").fetchdf()
        if df.empty:
            print("obj_location is empty")
            return {"total": 0}

        # 加载编码缓存
        cache = load_geocode_cache()
        cache_dict = {r["raw_address"]: r for _, r in cache.iterrows()} if not cache.empty else {}

        stats = {"total": len(df), "offline_matched": 0, "cache_matched": 0, "fallback": 0}
        updates = []

        for _, row in df.iterrows():
            raw = row["raw_address"]
            if pd.isna(raw) or not raw:
                continue

            # Step 1: AdminMatcher 离线匹配
            admin = parse_admin_path(raw)
            std_key = dual_segment_key(raw)

            update = {
                "location_id": row["location_id"],
                "std_address": std_key,
                "province": admin.get("province"),
                "prefecture": admin.get("prefecture"),
                "county": admin.get("county"),
                "township": admin.get("township"),
                "admin_code": admin.get("admin_code"),
                "geocode_source": "admin_offline",
                "geocode_confidence": admin.get("confidence"),
                "geocoded_at": None,
            }

            # AdminMatcher 坐标（区划质心）；NaN/Inf 不算有效坐标
            lat_v = admin.get("lat")
            lng_v = admin.get("lng")
            coord_ok = (
                isinstance(lat_v, (int, float)) and isinstance(lng_v, (int, float))
                and math.isfinite(float(lat_v)) and math.isfinite(float(lng_v))
            )
            if coord_ok:
                update["lng"] = admin["lng"]
                update["lat"] = admin["lat"]
                stats["offline_matched"] += 1
            else:
                update["lng"] = None
                update["lat"] = None
                update["geocode_source"] = "raw_fallback"
                stats["fallback"] += 1

            # Step 2: geocode_cache 覆盖（优先级更高）
            if raw in cache_dict:
                cached = cache_dict[raw]
                if pd.notna(cached.get("lat")) and pd.notna(cached.get("lng")):
                    update["lat"] = cached["lat"]
                    update["lng"] = cached["lng"]
                    update["geocode_source"] = cached["source"]
                    update["geocode_confidence"] = cached.get("confidence")
                    update["geocoded_at"] = cached.get("geocoded_at")
                    if update["geocode_source"] == "admin_offline":
                        stats["offline_matched"] += 1
                    else:
                        stats["cache_matched"] += 1
                        stats["fallback"] -= 1  # 从 fallback 中移除

            updates.append(update)

        # 批量 UPDATE
        if updates:
            now = datetime.now().isoformat()
            for u in updates:
                con.execute("""
                    UPDATE obj_location SET
                        std_address = ?,
                        province = ?,
                        prefecture = ?,
                        county = ?,
                        township = ?,
                        admin_code = ?,
                        lat = ?,
                        lng = ?,
                        geocode_source = ?,
                        geocode_confidence = ?,
                        geocoded_at = ?
                    WHERE location_id = ?
                """, [
                    u["std_address"],
                    u["province"],
                    u["prefecture"],
                    u["county"],
                    u["township"],
                    u["admin_code"],
                    u["lat"],
                    u["lng"],
                    u["geocode_source"],
                    u["geocode_confidence"],
                    u.get("geocoded_at") or now,
                    u["location_id"],
                ])

        print(f"Enriched {len(updates)} locations:")
        print(f"  admin_offline: {stats['offline_matched']}")
        print(f"  cache_matched: {stats['cache_matched']}")
        print(f"  raw_fallback:  {stats['fallback']}")
        return stats

    finally:
        con.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="富化 obj_location")
    parser.add_argument("--db", type=str, default="data/investigation.duckdb",
                        help="DuckDB 数据库路径")
    args = parser.parse_args()
    enrich_locations(args.db)
