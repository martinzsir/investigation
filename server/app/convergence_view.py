"""三维交汇读面：把关系、时间、空间三类证据归到同一「人-时-地」锚点下。

与观察档案的关系
----------------
交汇**不新增事实**，只做归集：输入是观察档案（批量 + 定向），输出是
"同一个锚点上有几个维度命中"。故它复用 `clues_artifact` 的观察装载，
不另开一套取数——否则观察改了、交汇读的是旧口径，又会分叉。

三条读面红线（与 core/convergence.py 一致，这里负责不破坏它）
------------------------------------------------------------
1. **分数不是结论。** score 只用于排序；每条必须带三维各自的
   count/precision/weight。只给一个总分就等于把精度加权重新掩盖掉——
   正兵看不出"时间维是日期级、只有 0.6 权重"。

2. **重名不猜。** person_ambiguous=true 的锚点显式标出，前端必须单列，
   不能混进普通锚点排序。系统一旦替正兵决定"这个张卫国是哪个"，
   后面所有联动都建在可能错误的地基上。

3. **裁决未生效要看得见。** 拿不到数据库连接时，同名异人裁决**不执行**，
   所有主体按名归一。这是危险的静默失效（代码在跑、有输出、看不出问题），
   故 diagnostics.homonym_resolution 必须如实报 unavailable 及原因，
   不允许静默降级。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from server.app.clues_artifact import (load_case_observations,
                                       load_directed_observations)

# build_convergence 的 top_n 只做截断，分页在**本层**做——
# 若在 core 层截断，total 会失真、分页页码全错。
_NO_TRUNCATE = 10 ** 6


def _load_observations(case_dir: str | Path, version: int) -> list[dict]:
    """批量（随版本）+ 定向（案件级）观察，合并去重。

    与 observations_view 同口径：定向深挖的观察也要进交汇，否则正兵
    在画布上跑出的东西不会出现在交汇清单里——那才是最该看的部分。
    """
    obs = list(load_case_observations(case_dir, version))
    seen = {x.observation_id for x in obs}
    obs += [o for o in load_directed_observations(case_dir)
            if o.observation_id not in seen]
    return [o.to_dict() if hasattr(o, "to_dict") else dict(o) for o in obs]


def _build(case_dir: str | Path, version: int, conn: Any,
           min_dims: int) -> dict:
    from core.convergence import build_convergence
    return build_convergence(_load_observations(case_dir, version),
                             conn=conn, min_dims=min_dims,
                             top_n=_NO_TRUNCATE)


def _match(c: dict, *, person: str | None, dim: str | None,
           date_from: str | None, date_to: str | None,
           ambiguous: bool | None) -> bool:
    if person:
        hay = " ".join([str(c.get("person_key") or "")] +
                       [str(x) for x in (c.get("person_names") or [])] +
                       [str(x) for x in (c.get("co_present") or [])])
        if person not in hay:
            return False
    if dim and dim not in (c.get("hit_dimensions") or []):
        return False
    d = str(c.get("date") or "")
    if date_from and d and d < date_from:
        return False
    if date_to and d and d > date_to:
        return False
    if ambiguous is not None and bool(c.get("person_ambiguous")) != ambiguous:
        return False
    return True


def _stats(rows: list[dict]) -> dict:
    by_dim = {"space": 0, "time": 0, "relation": 0}
    by_hit = {"1": 0, "2": 0, "3": 0}
    ambiguous = 0
    top = 0.0
    for c in rows:
        for d in (c.get("hit_dimensions") or []):
            if d in by_dim:
                by_dim[d] += 1
        n = str(int(c.get("dim_hit_count") or 0))
        if n in by_hit:
            by_hit[n] += 1
        if c.get("person_ambiguous"):
            ambiguous += 1
        top = max(top, float(c.get("score") or 0.0))
    return {
        "by_dimension": by_dim,
        "by_hit_count": by_hit,
        "ambiguous": ambiguous,
        "max_score": round(top, 4),
    }


def _attach_coords(rows: list[dict], conn: Any) -> dict:
    """给本页交汇锚点挂坐标——地图视图的取数口。

    为什么要单独挂（而不是让前端拿 location_id 自己去查）
    -------------------------------------------------
    坐标**必须带来源自陈**（coord_degraded + geocode_source）。区划质心的
    坐标在地图上和门牌级长得一模一样——若只给一个经纬度，正兵会把"区级
    推算"读成"精确门牌位置"。那是空间侧 distance_m=0.0 那类伪精确在地图
    上的翻版，而地图天生看上去精确，误导性比列表更强。

    质心判定复用 core.geo._is_centroid_coord，不在此复制常量——
    否则空间判据改了、地图视觉分档还是旧口径，又要分叉。
    """
    if not rows:
        return {"status": "ok", "detail": "本页无锚点", "attached": 0,
                "missing": 0}
    # 契约一致：先统一置空，后续各分支只做覆盖——前端不必处理 undefined
    for c in rows:
        c["coords"] = []
    if conn is None:
        return {"status": "unavailable",
                "detail": "未连接语义层，无法读取 obj_location 坐标",
                "effect": "地图视图不可用；锚点仅有地址文本，不得据此打点"}
    ids: list[str] = []
    # 地址兜底候选：geo_accompany 的空间锚点 locations 是**地址文本**
    # （函数输出只到地址级，不带 location_id），_space_anchors 原样塞进了
    # location_id——单靠 location_id 查询必然全空。故同时收集锚点的
    # std_addresses 与 location_ids 里的非 loc_* 值，按 std_address 兜底。
    addrs: list[str] = []
    for c in rows:
        for x in (c.get("location_ids") or []):
            s = str(x)
            if not s:
                continue
            if s.startswith("loc_"):
                if s not in ids:
                    ids.append(s)
            elif s not in addrs:
                addrs.append(s)
        for x in (c.get("std_addresses") or []):
            s = str(x)
            if s and s not in addrs:
                addrs.append(s)
    if not ids and not addrs:
        return {"status": "ok", "detail": "本页锚点无地点实体",
                "attached": 0, "missing": len(rows)}
    try:
        from core.geo import _is_centroid_coord
        where: list[str] = []
        params: list[str] = []
        if ids:
            where.append(f"location_id IN ({','.join(['?'] * len(ids))})")
            params.extend(ids)
        if addrs:
            where.append(f"std_address IN ({','.join(['?'] * len(addrs))})")
            params.extend(addrs)
        cur = conn.execute(
            "SELECT location_id, std_address, lat, lng, coord_sys, "
            "geocode_source, geocode_confidence FROM obj_location "
            f"WHERE {' OR '.join(where)} "
            # 同一 std_address 可能有多条门牌（编码抖动），优先高置信行
            "ORDER BY geocode_confidence DESC NULLS LAST", params)
        cols = [d[0] for d in cur.description]
        by_id: dict[str, dict] = {}
        by_addr: dict[str, dict] = {}
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            by_id.setdefault(str(row.get("location_id")), row)
            if row.get("std_address"):
                by_addr.setdefault(str(row["std_address"]), row)
    except Exception as e:      # 取不到坐标不该让列表不可用
        return {"status": "error", "detail": f"{type(e).__name__}: {e}",
                "effect": "地图视图不可用；清单与时间轴不受影响"}

    attached = 0
    for c in rows:
        out: list[dict] = []
        seen_lids: set[str] = set()
        # 先按 location_id 精确匹配，未命中再按地址兜底；同一 location_id
        # 不重复落点（id 与地址可能指到同一行）。
        keys = [str(x) for x in (c.get("location_ids") or [])]
        keys += [str(x) for x in (c.get("std_addresses") or [])]
        for k in keys:
            r = by_id.get(k) or by_addr.get(k)
            if not r or r.get("lat") is None or r.get("lng") is None:
                continue
            lid = str(r.get("location_id"))
            if lid in seen_lids:
                continue
            seen_lids.add(lid)
            # 单一真相源：质心判定与空间判据同一函数
            degraded = bool(_is_centroid_coord(r))
            out.append({
                "location_id": r.get("location_id"),
                "std_address": r.get("std_address"),
                "lat": float(r["lat"]),
                "lng": float(r["lng"]),
                "coord_sys": r.get("coord_sys"),
                "geocode_source": r.get("geocode_source"),
                "geocode_confidence": r.get("geocode_confidence"),
                # 地图视觉分档的唯一依据：质心档不得画成实心精确点
                "coord_degraded": degraded,
                "coord_note": ("坐标为区划质心，非门牌位置，不代表实际间距"
                               if degraded else None),
            })
        c["coords"] = out
        if out:
            attached += 1
    return {"status": "ok",
            "detail": f"已挂 {attached}/{len(rows)} 条锚点坐标",
            "attached": attached, "missing": len(rows) - attached}


def assemble_convergence(*, case_dir: str | Path, version: int,
                         conn: Any = None, conn_error: str | None = None,
                         min_dims: int = 2, dim: str | None = None,
                         person: str | None = None,
                         date_from: str | None = None,
                         date_to: str | None = None,
                         ambiguous: bool | None = None,
                         page: int = 1, page_size: int = 20) -> dict:
    """交汇列表：筛选 + 分页 + 统计 + 口径自陈。

    观察档案为空（未 BUILD）→ available=False，不报错：
    没跑过研判就没有交汇，这是合法状态。
    """
    rows_all: list[dict] = []
    diag_extra: dict[str, Any] = {}
    try:
        res = _build(case_dir, version, conn, min_dims)
        rows_all = list(res.get("convergences") or [])
        diag_extra = dict(res.get("diagnostics") or {})
        weight_model = res.get("weight_model") or {}
        note = res.get("note") or ""
    except Exception as e:      # 交汇失败不该让整页不可用
        return {
            "available": False,
            "convergences": [], "total": 0, "returned": 0,
            "page": page, "page_size": page_size,
            "note": f"交汇构建失败：{type(e).__name__}: {e}",
            "stats": _stats([]), "weight_model": {},
            "diagnostics": {"homonym_resolution": {
                "status": "error", "detail": f"{type(e).__name__}: {e}"}},
        }

    matched = [c for c in rows_all
               if _match(c, person=person, dim=dim, date_from=date_from,
                         date_to=date_to, ambiguous=ambiguous)]
    total = len(matched)
    start = max(0, (page - 1) * page_size)
    page_rows = matched[start:start + page_size]

    # 坐标只给本页挂（统计不需要坐标），且**必须带来源自陈**
    coords_diag = _attach_coords(page_rows, conn)

    # 裁决是否真的生效了——不生效必须写进诊断，不静默按名归一
    if conn_error:
        homonym = {"status": "unavailable", "detail": conn_error,
                   "effect": "同名异人裁决未执行，主体按名归一；"
                             "person_ambiguous 恒为 false，重名将被静默合并"}
    else:
        homonym = {"status": "ok", "detail": "已连接语义层，同名异人裁决生效"}

    diag = dict(diag_extra)
    diag["homonym_resolution"] = homonym
    diag["coords"] = coords_diag
    diag["observations_used"] = len(_load_observations(case_dir, version))

    return {
        "available": True,
        "convergences": page_rows,
        "total": total,
        "returned": len(page_rows),
        "page": page, "page_size": page_size,
        "min_dims": min_dims,
        "stats": _stats(matched),
        "weight_model": weight_model,
        "note": note,
        "diagnostics": diag,
    }


def assemble_convergence_detail(*, case_dir: str | Path, version: int,
                                conv_key: str, conn: Any = None,
                                conn_error: str | None = None,
                                fact_limit: int = 20) -> dict | None:
    """单条交汇详情：三维命中明细 + 支撑观察（判据/证伪/事实行）。

    详情要把"这条交汇凭什么成立"摊开——正兵要能顺着 obs_id 回到观察档案，
    再顺 evidence_refs 回到真实行。找不到 → None（路由转 404）。
    """
    try:
        res = _build(case_dir, version, conn, min_dims=1)
    except Exception:
        return None
    target = None
    for c in (res.get("convergences") or []):
        if str(c.get("key")) == str(conv_key):
            target = c
            break
    if target is None:
        return None

    obs_index = {str(o.get("observation_id")): o
                 for o in _load_observations(case_dir, version)}
    support: dict[str, list[dict]] = {"space": [], "time": [], "relation": []}
    for o in _load_observations(case_dir, version):
        sid = str(o.get("skill_id") or "")
        dim = ("space" if sid.startswith("geo_")
               else "time" if sid.startswith("timeline_")
               else "relation" if sid.startswith("relation_") else None)
        if dim is None:
            continue
        support[dim].append({
            "observation_id": o.get("observation_id"),
            "skill_id": sid,
            "lens_name": o.get("lens_name") or sid,
            "title": o.get("title"),
            "subject": o.get("subject"),
            "basis": o.get("basis"),
            "falsification": o.get("falsification"),
            "degraded": o.get("degraded"),
            "degraded_reason": o.get("degraded_reason"),
            "facts": (o.get("facts") or [])[:fact_limit],
            "facts_total": len(o.get("facts") or []),
        })
    return {
        "available": True,
        "convergence": target,
        "support": support,
        "observations_index": len(obs_index),
        "weight_model": res.get("weight_model") or {},
        "note": res.get("note") or "",
        "diagnostics": {
            "homonym_resolution": (
                {"status": "unavailable", "detail": conn_error,
                 "effect": "同名异人裁决未执行，主体按名归一"}
                if conn_error else
                {"status": "ok", "detail": "已连接语义层，裁决生效"}),
        },
    }
