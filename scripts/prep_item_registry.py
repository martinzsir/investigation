"""scripts/prep_item_registry.py —— 物品源表预装配（L3 原始三表 → L3 宽表两表）。

为什么需要这一层
----------------
物品身份键是 sha256 摘要（``core/item.py``，R14 只存摘要不落明文），DuckDB
SQL 层若重算 digest 会与 py 口径分叉（salt/编码任一漂移，两轨全对不上）。
故 digest、中文类型映射、轨迹序列化全部在本层（py）完成，bindings 只留
**等值 JOIN**——这是「类型层与管道层分离」在物品域的落点：py 复杂性止步
于编译前的数据准备，不进声明层。

输入（data/ 根，default 包）
--------------------------
- 物品台账.parquet      一件物品一行（物品名称/物品类型/凭证号/所在地/特征描述/经度/纬度）
- 物品持有登记.parquet  一次持有一行（物品名称/凭证号/持有人/开始日期/结束日期/登记序号）
- 物品轨迹.parquet      一个轨迹点一行（物品名称/凭证号/日期/时刻/地点/经度/纬度；**可缺**）

输出（data/ 根，同名覆盖）
------------------------
- 物品登记.parquet  obj_item 源宽表：名称/类型(英文枚举)/标识(digest json)/标识摘要/
                    特征(json)/地点/纬度/经度/轨迹(json)
- 物品持有.parquet  obj_hold_record 源：持有人/物品名称/类型/标识摘要/
                    开始日期/结束日期/开始时刻/结束时刻/登记序号

红线（与画布物品层同源，改动前务必读）
------------------------------------
R-1  中文类型 → (item_type, identifier_kind) 映射只在 ``core/item.py`` 一份。
R-4  无凭证且无特征的行**跳过并打印原因**——静默合成一件是 D4 最危险形态。
R-5  起止日期缺失写 None（编译期即 NULL），绝不填哨兵日期。
R-6  物品轨迹序列化为 obj_item.track（json 属性），**不并入「轨迹出行」**——
     那张表的主体列是人（person_raw），把车牌塞进去会让空间镜头把车当人跑。
R-7  轨迹点精度一律经 ``core.time_semantics.derive_time_precision`` 派生。
R-14 产物不落明文：物品持有.parquet 不带凭证号列，关联键是 (类型, 标识摘要)。
     凭证明文只在 prep 进程内参与比对（``prep_items`` 返回的索引），随进程结束消散。

日期/时刻分流：源值含时刻部分（hour/minute/second 档）落 开始时刻/结束时刻
（DE_DATETIME），纯日期落 开始日期/结束日期（DE_DATE）——单一时态列两头不讨好：
时刻列收日期会补 00:00:00 伪装时刻，日期列收时刻会截断丢信号（套牌判定要
时刻级）。与 trackpoint 的 date/timestamp 双列同口径。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.item import (
    ITEM_TYPE_SOURCE_MAP,
    build_identifier,
    primary_digest,
)
from core.time_semantics import derive_time_precision

ITEM_TABLE = "物品台账.parquet"
HOLD_TABLE = "物品持有登记.parquet"
TRACK_TABLE = "物品轨迹.parquet"   # R-6：独立表，不得并入「轨迹出行」

OUT_REGISTRY = "物品登记.parquet"
OUT_HOLDS = "物品持有.parquet"

_TIME_PRECISIONS = ("hour", "minute", "second")

# 写出列类型：数值列显式声明，其余 VARCHAR（空行集也按此建空表，下游 schema 稳定）
_COL_TYPES = {"纬度": "DOUBLE", "经度": "DOUBLE", "登记序号": "BIGINT"}

REGISTRY_COLUMNS = ["名称", "类型", "标识", "标识摘要", "特征",
                    "地点", "纬度", "经度", "轨迹"]
HOLD_COLUMNS = ["持有人", "物品名称", "类型", "标识摘要",
                "开始日期", "结束日期", "开始时刻", "结束时刻", "登记序号"]


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _json(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False)


def _cred_key(cred: str, name: str) -> str:
    """源表行 → 物品的去重键。

    有凭证按凭证（同码即同一件）；无凭证按物品名称——若统一返回空串，
    所有无凭证赃物会被合成**一件**（D4），比不消歧更危险。
    无凭证时用名称而非特征描述：持有流水与台账都带物品名称，而特征描述
    只在台账里——按描述做键会让持有记录关联不上。
    """
    return f"c:{cred}" if cred else f"n:{name}"


def _read_parquet(path: Path) -> list[dict]:
    """读 parquet 为 dict 行；缺文件返回空列表（缺表的判定留给调用方）。

    用 duckdb 而不是 pandas：本环境缺 pyarrow，而 duckdb 自带 parquet
    reader——为读表引入新依赖不值得（装依赖有触发环境重置的先例）。
    """
    if not path.exists():
        return []
    import duckdb

    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()
    return [dict(zip(cols, r)) for r in rows]


def _pick(row: dict, *keys: str) -> str:
    for k in keys:
        v = _text(row.get(k))
        if v:
            return v
    return ""


def _split_datetime(v: Any) -> tuple[str | None, str | None]:
    """源时间值 → (date 列值, time 列值)。缺失 → (None, None)（R-5）。

    含时刻部分（hour/minute/second 档）落 time 列；其余（date 档与不可
    解析脏值）落 date 列——脏值由编译期 TRY_CAST 降级 NULL 并留痕，
    本层不预设判官。
    """
    s = _text(v)
    if not s:
        return None, None
    if derive_time_precision(s) in _TIME_PRECISIONS:
        return None, s
    return s, None


def _track_point(r: dict) -> dict:
    """源表一行 → 一个轨迹点。坐标缺失保持 None，**绝不补 0**。"""
    date = _pick(r, "日期", "date")
    ts = _pick(r, "时刻", "timestamp", "时间")
    loc = _pick(r, "地点", "位置", "所在地")
    lat, lng = r.get("纬度"), r.get("经度")
    # R-7：精度一律由 core.time_semantics 派生，不自创判定。
    # 时刻优先；纯日期行会被派生为 date 档（整点则是 hour 档）。
    prec = derive_time_precision(ts) or derive_time_precision(date)
    return {
        "date": date or None,
        "timestamp": ts or None,
        "location": loc or None,
        "lat": lat,
        "lng": lng,
        "mappable": lat is not None and lng is not None,
        "time_precision": prec,
    }


def prep_items(item_rows: list[dict],
               track_rows: list[dict] | None = None
               ) -> tuple[list[dict], dict[str, tuple[str, str, str]], list[str]]:
    """台账行 + 轨迹行 → (物品登记宽表行, 进程内关联索引, skipped 原因列表)。

    关联索引键即 ``_cred_key`` 口径（有凭证 c:凭证明文 / 无凭证 n:名称），
    值为 (item_type, primary_digest, title)。**明文只在进程内**——索引不
    落盘，随进程结束消散（R-14）。
    """
    # 轨迹先按键分组：指向未登记物品的轨迹在建完物品后再判（如实跳过，不猜归属）
    track_by_key: dict[str, list[dict]] = {}
    for r in track_rows or []:
        k = _cred_key(_pick(r, "凭证号", "凭证", "标识号"),
                      _pick(r, "物品名称", "名称"))
        track_by_key.setdefault(k, []).append(_track_point(r))

    rows: list[dict] = []
    index: dict[str, tuple[str, str, str]] = {}
    skipped: list[str] = []
    for r in item_rows:
        title = _pick(r, "物品名称", "名称")
        type_cn = _pick(r, "物品类型", "类型")
        cred = _pick(r, "凭证号", "凭证", "标识号")
        desc = _pick(r, "特征描述", "特征", "描述")
        loc = _pick(r, "所在地", "地点", "位置")
        lat, lng = r.get("纬度"), r.get("经度")

        mapped = ITEM_TYPE_SOURCE_MAP.get(type_cn)
        if not mapped:
            skipped.append(f"{title or '未命名'}：未知物品类型 {type_cn!r}")
            continue
        item_type, kind = mapped

        identifiers = ([build_identifier(kind, cred)] if cred else [])
        descriptors = ({"特征描述": desc} if desc else None)
        dig = primary_digest(identifiers, descriptors)
        if not dig:
            # R-4/D4：既无凭证也无特征 —— 拒绝成行，不静默合成一件
            skipped.append(f"{title or '未命名'}：无凭证且无特征描述，无法成实体")
            continue

        key = _cred_key(cred, title)
        index[key] = (item_type, dig, title)
        # 名称退路：持有行无凭证号时只能按名称关联（无凭证类一件一名称）
        index.setdefault(f"n:{title}", (item_type, dig, title))
        rows.append({
            "名称": title,
            "类型": item_type,
            "标识": _json(identifiers),
            "标识摘要": dig,
            "特征": _json(descriptors),
            "地点": loc or None,
            "纬度": lat,
            "经度": lng,
            "轨迹": _json(track_by_key.get(key) or []),
        })
    for k in track_by_key:
        if k not in index:
            skipped.append(f"轨迹：{k} 未在物品台账中登记，未挂载")
    return rows, index, skipped


def prep_holds(hold_rows: list[dict],
               index: dict[str, tuple[str, str, str]]
               ) -> tuple[list[dict], list[str]]:
    """持有登记行 → 物品持有表行。

    关联键从 ``prep_items`` 的进程内索引查（有凭证按凭证明文查、无凭证按
    名称查），**产物只带 (类型, 标识摘要)**——凭证明文不进宽表（R-14）。
    """
    rows: list[dict] = []
    skipped: list[str] = []
    for i, r in enumerate(hold_rows):
        title = _pick(r, "物品名称", "名称")
        cred = _pick(r, "凭证号", "凭证", "标识号")
        holder = _pick(r, "持有人", "主体", "姓名")
        hit = index.get(_cred_key(cred, title))
        if hit is None:
            # 持有流水指向台账里没有的物品：如实跳过，不猜（猜了会把不同
            # 物品的持有记录挂到同一件上，链看着完整但全错）
            skipped.append(f"持有记录 #{i + 1}：物品未在台账中登记")
            continue
        if not holder:
            skipped.append(f"持有记录 #{i + 1}：持有人为空")
            continue
        item_type, dig, item_title = hit
        start_d, start_t = _split_datetime(
            _pick(r, "开始日期", "起始日期", "start_date"))
        end_d, end_t = _split_datetime(
            _pick(r, "结束日期", "处置日期", "end_date"))
        # 0 是合法登记序号：用 `or` 取值会短路成 None，链序随之错乱
        order = r.get("登记序号")
        if order is None:
            order = r.get("序号")
        rows.append({
            "持有人": holder,
            "物品名称": item_title,
            "类型": item_type,
            "标识摘要": dig,
            "开始日期": start_d,
            "结束日期": end_d,
            "开始时刻": start_t,
            "结束时刻": end_t,
            "登记序号": order,
        })
    return rows, skipped


def _write_parquet(path: Path, rows: list[dict], columns: list[str]) -> None:
    """dict 行 → parquet。空行集也按声明列类型建空表（下游 CTAS schema 稳定）。"""
    import duckdb

    con = duckdb.connect()
    try:
        cols_def = ", ".join(
            f'"{c}" {_COL_TYPES.get(c, "VARCHAR")}' for c in columns)
        con.execute(f"CREATE TABLE t ({cols_def})")
        if rows:
            ph = "(" + ", ".join(["?"] * len(columns)) + ")"
            con.executemany(
                f"INSERT INTO t VALUES {ph}",
                [[r.get(c) for c in columns] for r in rows])
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()


def run(data_dir: Path | str = "data") -> dict:
    """预装配主流程：读三表 → 写两表。返回统计（含 skipped 原因）。"""
    d = Path(data_dir)
    item_rows = _read_parquet(d / ITEM_TABLE)
    hold_rows = _read_parquet(d / HOLD_TABLE)
    track_rows = _read_parquet(d / TRACK_TABLE)   # 缺表=空，轨迹属增强数据
    if not item_rows and not hold_rows:
        return {"items": 0, "holds": 0, "track_points": 0,
                "skipped": ["未接入物品数据源（无物品台账/持有登记表）"],
                "out": []}

    wide, index, skipped = prep_items(item_rows, track_rows)
    holds, h_skipped = prep_holds(hold_rows, index)
    skipped += h_skipped

    _write_parquet(d / OUT_REGISTRY, wide, REGISTRY_COLUMNS)
    _write_parquet(d / OUT_HOLDS, holds, HOLD_COLUMNS)
    return {
        "items": len(wide),
        "holds": len(holds),
        "track_points": sum(
            len(json.loads(r["轨迹"])) for r in wide if r.get("轨迹")),
        "skipped": skipped,
        "out": [str(d / OUT_REGISTRY), str(d / OUT_HOLDS)],
    }


def main() -> None:
    stats = run(Path("data"))
    print(f"物品登记：{stats['items']} 件 / 持有：{stats['holds']} 条"
          f" / 轨迹点：{stats['track_points']} 个")
    for s in stats.get("skipped") or []:
        print(f"  跳过：{s}")


if __name__ == "__main__":
    main()
