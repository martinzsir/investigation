"""
scripts/build_admin_ref.py —— 从五级区划 CSV 生成归一化 lookup 表

规则：重写实现（参考通用算法思想，不复制 AGPL 源码）
输出：data/ref/admin_ref.parquet（列：level/name/core_name/code/...）
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "data" / "ref"
OUT = REF_DIR / "admin_ref.parquet"

# 后缀剥离规则（按层级）
PROVINCE_SUFFIXES = ["特别行政区", "自治区", "直辖市", "省"]
PREFECTURE_SUFFIXES = ["自治州", "地区", "盟", "州", "市"]
COUNTY_SUFFIXES = ["自治县", "自治旗", "市辖区", "县级市", "特区", "林区", "旗", "县", "区", "市"]
TOWNSHIP_SUFFIXES = ["民族苏木", "民族乡", "街道办事处", "街道", "苏木", "镇", "乡"]
VILLAGE_SUFFIXES = ["社区居委会", "居民委员会", "居委会", "村民委员会", "村委会", "村"]

ALL_SUFFIXES = sorted(
    set(PROVINCE_SUFFIXES + PREFECTURE_SUFFIXES + COUNTY_SUFFIXES + TOWNSHIP_SUFFIXES + VILLAGE_SUFFIXES),
    key=len,
    reverse=True,
)

# 少数民族自治 pattern
ETHNIC_AUTONOMY_PATTERN = re.compile(r"^(.+?)(?:[\u4e00-\u9fff]{1,8}族)+自治(?:区|州|县|旗)$")

# 空格清洗
WHITESPACE_PATTERN = re.compile(r"[\s\u3000\xa0]+")


def clean_text(value):
    if pd.isna(value):
        return ""
    text = str(value).replace("\ufeff", "").replace("（", "(").replace("）", ")")
    text = WHITESPACE_PATTERN.sub("", text)
    return text.strip()


def strip_any_suffix(text: str) -> str:
    name = clean_text(text)
    if not name:
        return ""
    ethnic_match = ETHNIC_AUTONOMY_PATTERN.match(name)
    if ethnic_match:
        return ethnic_match.group(1)
    for suffix in ALL_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def extract_core(text: str, level: str) -> str:
    """按层级去后缀提取核心名"""
    name = clean_text(text)
    if not name:
        return ""
    ethnic_match = ETHNIC_AUTONOMY_PATTERN.match(name)
    if ethnic_match:
        return ethnic_match.group(1)
    suffix_map = {
        "province": PROVINCE_SUFFIXES,
        "prefecture": PREFECTURE_SUFFIXES,
        "county": COUNTY_SUFFIXES,
        "township": TOWNSHIP_SUFFIXES,
        "village": VILLAGE_SUFFIXES,
    }
    for suffix in suffix_map.get(level, []):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def split_aliases(alias_text: str) -> list[str]:
    text = clean_text(alias_text)
    if not text:
        return []
    parts = [clean_text(part) for part in re.split(r"[;；/、,，]|(?:\s{2,})", text)]
    return [p for p in parts if p]


def build_province():
    df = pd.read_csv(REF_DIR / "province.csv", dtype=str, encoding="utf-8-sig")
    rows = []
    for _, r in df.iterrows():
        name = clean_text(r.get("省", ""))
        code = clean_text(r.get("省级码", ""))
        if not name or not code:
            continue
        core = extract_core(name, "province")
        rows.append({
            "level": "province",
            "name": name,
            "core_name": core,
            "code": code,
            "parent_code": "",
            "province": name,
            "prefecture": "",
            "county": "",
            "township": "",
            "full_path": name,
            "alias": "",
        })
    return pd.DataFrame(rows)


def build_prefecture():
    df = pd.read_csv(REF_DIR / "prefecture.csv", dtype=str, encoding="utf-8-sig")
    rows = []
    for _, r in df.iterrows():
        name = clean_text(r.get("地级", "") or r.get("地名", ""))
        code = clean_text(r.get("地级码", ""))
        province = clean_text(r.get("省级", ""))
        province_code = clean_text(r.get("省级码", ""))
        alias = clean_text(r.get("曾用名", ""))
        if not name or not code:
            continue
        # 直辖市特殊处理：地级"不统计" → 省级名
        if name in {"不统计", "市辖区", "市辖县", "县"} and province_code[:2] in {"11", "12", "31", "50"}:
            name = province
        core = extract_core(name, "prefecture")
        rows.append({
            "level": "prefecture",
            "name": name,
            "core_name": core,
            "code": code,
            "parent_code": province_code,
            "province": province,
            "prefecture": name,
            "county": "",
            "township": "",
            "full_path": f"{province} > {name}",
            "alias": alias,
        })
    return pd.DataFrame(rows)


def build_county():
    df = pd.read_csv(REF_DIR / "county.csv", dtype=str, encoding="utf-8-sig")
    rows = []
    for _, r in df.iterrows():
        name = clean_text(r.get("县级", "") or r.get("地名", ""))
        code = clean_text(r.get("县级码", ""))
        province = clean_text(r.get("省级", ""))
        prefecture = clean_text(r.get("地级", ""))
        prefecture_code = clean_text(r.get("地级码", ""))
        alias = clean_text(r.get("曾用名", ""))
        if not name or not code:
            continue
        core = extract_core(name, "county")
        rows.append({
            "level": "county",
            "name": name,
            "core_name": core,
            "code": code,
            "parent_code": prefecture_code,
            "province": province,
            "prefecture": prefecture,
            "county": name,
            "township": "",
            "full_path": f"{province} > {prefecture} > {name}",
            "alias": alias,
        })
    return pd.DataFrame(rows)


def build_township():
    df = pd.read_csv(REF_DIR / "township.csv", dtype=str, encoding="utf-8-sig")
    rows = []
    for _, r in df.iterrows():
        name = clean_text(r.get("乡镇", ""))
        code = clean_text(r.get("乡镇代码", ""))
        province = clean_text(r.get("省", ""))
        prefecture = clean_text(r.get("市", ""))
        county = clean_text(r.get("县", ""))
        county_code = clean_text(r.get("县代码", ""))
        if not name or not code:
            continue
        core = extract_core(name, "township")
        rows.append({
            "level": "township",
            "name": name,
            "core_name": core,
            "code": code,
            "parent_code": county_code,
            "province": province,
            "prefecture": prefecture,
            "county": county,
            "township": name,
            "full_path": f"{province} > {prefecture} > {county} > {name}",
            "alias": "",
        })
    return pd.DataFrame(rows)


def load_centroids() -> dict[str, tuple[float, float]]:
    """从 DataV all_districts.json 加载区划质心（GCJ-02）。

    返回 {adcode: (lng, lat)}。DataV 覆盖省/地/县三级（无乡镇级）。
    """
    import json
    centroid_file = REF_DIR / "all_districts.json"
    if not centroid_file.exists():
        print(f"WARNING: {centroid_file} not found, no centroids")
        return {}
    with open(centroid_file, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for d in data:
        adcode = str(d.get("adcode", ""))
        lng = d.get("lng")
        lat = d.get("lat")
        if adcode and lng is not None and lat is not None:
            out[adcode] = (float(lng), float(lat))
    return out


def main():
    parts = [build_province(), build_prefecture(), build_county(), build_township()]
    combined = pd.concat(parts, ignore_index=True)

    # 合并质心坐标（GCJ-02）
    centroids = load_centroids()
    combined["lng"] = combined["code"].map(lambda c: centroids.get(c, (None, None))[0])
    combined["lat"] = combined["code"].map(lambda c: centroids.get(c, (None, None))[1])

    combined.to_parquet(OUT, index=False)
    print(f"Written: {OUT}")
    print(f"Total rows: {len(combined)}")
    for level in ["province", "prefecture", "county", "township"]:
        subset = combined[combined["level"] == level]
        n = len(subset)
        n_coord = subset["lng"].notna().sum()
        print(f"  {level}: {n} (with coords: {n_coord})")


if __name__ == "__main__":
    main()
