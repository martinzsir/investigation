"""物品数据源装配（应用层）：语义层 → 画布物品层。

它接的是哪一段断链
------------------
物品数据已收编进本体（方案 A）：``scripts/prep_item_registry.py`` 把
物品台账/持有登记/物品轨迹三表预装配为 物品登记/物品持有 两宽表，
bindings 物化为 ``obj_item`` / ``obj_hold_record`` / ``lnk_holds``。
本模块**只读语义层**（obj_item / obj_hold_record），产出 item 节点、
持有人 subject 节点与持有的时态边——与 Function/规则/图库/MCP 同一
数据轨，画布里看得到的物品，检测器也查得到，不再双轨。

为什么不直读 parquet（历史包袱，已消除）
---------------------------------------
旧版直读 parquet 的原因是 ``primary_digest`` 是 sha256 摘要、SQL 算不出来。
该复杂性已前移到 prep 脚本（py 层，口径唯一真源 core/item.py），编译期
bindings 只剩等值投影——本层随之单轨化，不再保留直读分支。

红线
----
R-1 物品类型中文 → (item_type, identifier_kind) 的映射在 ``core/item.py``
    **只此一份**（本模块 re-export 供旧引用兼容）。散在两处迟早分叉。
R-2 缺表返回空并给原因，**不静默当"查无此事"**。数据源未接入与确实没有
    物品，后续动作完全不同（前者要接入数据，后者可结案）。
R-3 坐标缺失标 mappable=False，绝不补 0（既有口径）。
R-4 无凭证物品必须带 descriptors，否则 prep 层已拒绝成行（D4 落点）；
    本层再防一手：primary_digest 为空的行跳过并留痕，不静默建节点。
R-5 起止日期缺失保持空串，由 hold_chain 落 unknown_overlaps；绝不填
    "1900-01-01"/"9999-12-31" 之类的哨兵值，那会把不可判定伪装成可判定。
R-6 物品轨迹挂在 obj_item.track（json 属性），不并入「轨迹出行」——
    后者经 bindings 映射到 ``trackpoint.person_raw``，属性名就是人；把车牌
    塞进去会让空间镜头把车当人跑。
R-7 轨迹点精度一律经 ``core.time_semantics`` 派生，**不自创判定**。整点
    （14:00:00）会派生为 hour 档，而"同时/同时段"只认 minute 及以上——
    判不出的点必须落 unknown，绝不计入命中（伪精确）。
"""

from __future__ import annotations

import json
from datetime import date as _date, datetime as _dt
from typing import Any

from core.item import ITEM_TYPE_SOURCE_MAP  # noqa: F401  re-export（R-1 真源在 core/item.py）

from server.app.canvas_case import (
    GENERATED_BY_ITEM,
    HOLD_REL,
    ITEM_TYPE_LABELS,
    PRECISION_WEIGHT,
    build_subject_node,
    case_edge_id,
    case_node_id,
    norm_precision,
)

# T5 防污染断言仍引用这两个常量：物品轨迹独立表，不得并入「轨迹出行」
TRACK_TABLE = "物品轨迹.parquet"
PERSON_TRACK_TABLE = "轨迹出行.parquet"


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _fmt_temporal(v: Any) -> str:
    """DATE/TIMESTAMP 列值 → 画布边 props 字符串。

    datetime → "YYYY-MM-DD HH:MM:SS"；date → "YYYY-MM-DD"；str 直通。
    **date 绝不补零时刻**——补成 00:00:00 会把日期级伪装成时刻级，
    与 time_semantics 派生口径（全零时刻判 hour/date 档）同源。
    """
    if v is None:
        return ""
    if isinstance(v, _dt):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, _date):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()


def _loads_json(v: Any) -> Any:
    """obj_* 的 json 列（VARCHAR 存的 json 文本）→ 对象；脏值容错为 None。"""
    if v is None or not isinstance(v, str) or not v.strip():
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _table_exists(conn: Any, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:
        return False


def _fetch(conn: Any, sql: str) -> list[dict]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _item_node_from_row(*, case_id: str, row: dict) -> dict | None:
    """obj_item 行 → 画布 item 节点。

    与 canvas_case.build_item_node 的产出 schema 对齐，但**不重算 digest**
    ——obj_item.primary_digest/identifiers 已是 core.item 的摘要口径（R14
    唯一真源），重算既多余又会引入第二口径。
    """
    item_type = _text(row.get("item_type"))
    digest = _text(row.get("primary_digest"))
    title = _text(row.get("title"))
    if not digest:
        # R-4：空摘要意味着「无身份」——prep 层已拒造，能漏进来就是脏数据，
        # 跳过并留痕，绝不建节点（两件空摘要物品会被合并成同一件，D4）
        return None
    ids = _loads_json(row.get("identifiers")) or []
    if not isinstance(ids, list):
        ids = []
    desc = _loads_json(row.get("descriptors"))
    if not isinstance(desc, dict):
        desc = None
    lat, lng = row.get("lat"), row.get("lng")
    if lat is None or lng is None:
        lat = lng = None
    has_cred = any(isinstance(i, dict) and i.get("kind") != "none"
                   and i.get("value_digest") for i in ids)
    type_title = ITEM_TYPE_LABELS.get(item_type) or item_type
    p = norm_precision("date")
    return {
        "id": case_node_id("item", case_id, f"{item_type}:{digest}"),
        "kind": "item",
        "system": False,
        "label": title or type_title,
        "props": {
            "sub_type": item_type,
            "item_type": item_type,
            "item_type_title": type_title,
            "title": title,
            "primary_digest": digest,
            # 标识符只存摘要（prep 层 build_identifier 已保证无明文字段）
            "identifiers": ids,
            "identifier_kinds": [i.get("kind") for i in ids
                                 if isinstance(i, dict)],
            "sensitive_kinds": sorted({i.get("kind") for i in ids
                                       if isinstance(i, dict) and i.get("sensitive")}),
            "credentialed": bool(has_cred),
            "unidentified": not has_cred,
            "descriptors": desc,
            "lat": lat, "lng": lng,
            "mappable": lat is not None and lng is not None,
            "coord_note": (None if (lat is not None and lng is not None)
                           else "未提供坐标，不参与地图落点"),
            # 单值持有属性已从本体移除（多段时态归 holds 边），
            # 保留 None 占位维持节点 props schema 稳定
            "holder_raw": None,
            "acquire_date": None, "dispose_date": None,
            "precision": p,
            "precision_weight": PRECISION_WEIGHT.get(p, 0.0),
            "location": _text(row.get("location")) or None,
            "generated_by": GENERATED_BY_ITEM,
        },
    }


def build_item_layer(*, case_id: str, conn: Any = None) -> dict[str, Any]:
    """语义层 → 画布物品层（重建层，每次从 obj_* 重建，不落库）。

    返回结构与 canvas_growth 对齐（nodes/edges/meta），便于在 GET 里用同一
    条合并路径并入。
    """
    if conn is None:
        return {"nodes": [], "edges": [], "subjects": [],
                "meta": {"items": 0, "holds": 0,
                         "reason": "未连接语义层，物品层不可用",
                         "source": "item_source"}}
    # R-2：缺表给原因，不静默当"查无此事"
    if not _table_exists(conn, "obj_item"):
        return {"nodes": [], "edges": [], "subjects": [],
                "meta": {"items": 0, "holds": 0,
                         "reason": "语义层无 obj_item 表（物品数据源未接入或未重建语义层）"
                                   "——非『查无物品』",
                         "source": "item_source"}}

    items = _fetch(conn, "SELECT item_id, item_type, title, identifiers, "
                         "primary_digest, descriptors, location, lat, lng, track "
                         "FROM obj_item")
    holds = (_fetch(conn, "SELECT hold_id, holder_raw, item_title, item_type, "
                          "primary_digest, start_date, end_date, start_time, "
                          "end_time, reg_order FROM obj_hold_record")
             if _table_exists(conn, "obj_hold_record") else [])

    if not items and not holds:
        return {"nodes": [], "edges": [], "subjects": [],
                "meta": {"items": 0, "holds": 0,
                         "reason": "物品数据源已接入但语义层无任何物品/持有记录",
                         "source": "item_source"}}

    # ---- 物品节点：一件物品一个节点（identity_key=[item_type, primary_digest]）----
    node_by_key: dict[tuple[str, str], dict] = {}
    skipped: list[str] = []
    track_points = 0
    for r in items:
        node = _item_node_from_row(case_id=case_id, row=r)
        if node is None:
            skipped.append(
                f"{_text(r.get('title')) or '未命名'}：标识摘要为空，无法成实体")
            continue
        pts = _loads_json(r.get("track")) or []
        if not isinstance(pts, list):
            pts = []
        node["props"]["track"] = pts
        # 摘要一并给出：正兵要看的是"三日下午在哪"，不是十行原始点
        node["props"]["track_summary"] = summarize_item_track(pts)
        track_points += len(pts)
        node_by_key[(_text(r.get("item_type")),
                      _text(r.get("primary_digest")))] = node

    # ---- 持有边 + 持有人主体节点 ----
    subject_by_name: dict[str, dict] = {}
    edges: list[dict] = []
    for i, r in enumerate(holds):
        holder = _text(r.get("holder_raw"))
        item = node_by_key.get((_text(r.get("item_type")),
                                _text(r.get("primary_digest"))))
        if item is None:
            # 持有流水指向语义层没有的物品：如实跳过，不猜（猜了会把不同
            # 物品的持有记录挂到同一件上，链看着完整但全错）
            skipped.append(
                f"持有记录 #{i + 1}：物品未入语义层（{_text(r.get('item_title'))}）")
            continue
        if not holder:
            skipped.append(f"持有记录 #{i + 1}：持有人为空")
            continue

        if holder not in subject_by_name:
            # conn 走真实重名裁决：张卫国有两个证号，绝不代选
            # 持有人节点也属重建层：源表撤掉后它不该留在库里。
            # 若正兵此前已手加过同名主体，merge 层会保住人工层那份不被
            # 标记（见 canvas_case_doc.merge_lens_layer 的 manual_ids）。
            subject_by_name[holder] = build_subject_node(
                case_id=case_id, name=holder, conn=conn,
                extra={"generated_by": GENERATED_BY_ITEM})

        subj = subject_by_name[holder]
        # R-5：缺失保持空串，由 hold_chain 判为不可判定；时刻优先（套牌
        # 判定依赖时刻级），日期级回落 date 列——精度由 hold_chain 从值派生
        start = _fmt_temporal(r.get("start_time")) or _fmt_temporal(r.get("start_date"))
        end = _fmt_temporal(r.get("end_time")) or _fmt_temporal(r.get("end_date"))
        eid = case_edge_id(str(subj["id"]), HOLD_REL, str(item["id"]))
        edges.append({
            "id": f"{eid}#{i}",
            "source": subj["id"],
            "target": item["id"],
            "rel": HOLD_REL,
            "system": True,
            "props": {
                "rel": HOLD_REL,
                "holder": holder,
                "start_date": start or "",
                "end_date": end or "",
                "order": r.get("reg_order"),
                "hold_id": _text(r.get("hold_id")) or None,
                "generated_by": GENERATED_BY_ITEM,
            },
        })

    nodes = list(node_by_key.values()) + list(subject_by_name.values())
    return {
        "nodes": nodes,
        "edges": edges,
        "subjects": list(subject_by_name.values()),
        "meta": {
            "items": len(node_by_key),
            "holds": len(edges),
            "track_points": track_points,
            "skipped": skipped,
            "reason": "" if nodes else
                      "物品数据源已接入但未能构造出任何节点",
            "source": "item_source",
            # 语义层连接存在即完成重名裁决（obj_person 同源）
            "disambiguated": True,
            "disambiguated_note": "",
        },
    }


def _hour_of(ts: Any) -> int | None:
    """取时刻的小时；无时刻/不可解析 → None（不可判定，不是 0 点）。"""
    s = str(ts or "").strip().replace("T", " ")
    if " " not in s:
        return None
    try:
        return int(s.split(" ", 1)[1][:2])
    except (ValueError, IndexError):
        return None


def summarize_item_track(track: Any, *, location: str = "",
                         afternoon: tuple[int, int] = (12, 18)) -> dict:
    """物品轨迹摘要：把「能判定的」与「判不出的」分开列。

    返回
    ----
    hit_days      可判定「下午在某地」的日期（去重升序）——唯一能支撑结论的
    unknown_days  判不出的日期：无时刻 / 精度不到 minute / 地点缺失
    other_days    同物异地或非下午——它非空才说明 hit 不是同义反复

    为什么必须分开：无时刻的点若混进 hit，"三个下午"会虚高成四个；而
    hour 档（整点）看似有时刻，实际判不出"同时/同时段"——把它算进命中
    就是伪精确，与 E16 那条"unknown 绝不并入 conflicts"同源。
    """
    pts = list(track or [])
    hit: list[str] = []
    unknown: list[str] = []
    other: list[str] = []

    for p in pts:
        ts = p.get("timestamp")
        d = str(p.get("date") or (str(ts)[:10] if ts else "")).strip()
        if not d:
            continue
        # R-7：只有 minute 及以上才谈得上"下午"
        if p.get("time_precision") not in ("minute", "second"):
            unknown.append(d)
            continue
        hh = _hour_of(ts)
        loc = str(p.get("location") or "")
        if hh is None:
            unknown.append(d)
            continue
        if location:
            if location not in loc:
                other.append(d)
                continue
        elif not loc:
            unknown.append(d)
            continue
        if afternoon[0] <= hh < afternoon[1]:
            hit.append(d)
        else:
            other.append(d)

    mappable = sum(1 for p in pts if p.get("mappable"))
    if not pts:
        reason = "无物品轨迹数据（数据源可能未接入）"
    elif not hit:
        where = f"在『{location}』" if location else ""
        reason = (f"有轨迹但无『可判定的下午{where}出现』记录——"
                  "可能是精度不足，不是没去过")
    else:
        reason = ""
    return {
        "points": len(pts),
        "mappable_points": mappable,
        # 筛选条件一并回传：不给它，调用方会把"下午在任何地方出现"
        # 误读成"下午在某地出现"——字段叫 hit_days 就带了这个歧义。
        "filter_location": location or None,
        "hit_days": sorted(set(hit)),
        "hit_points": len(hit),
        "unknown_days": sorted(set(unknown)),
        "other_days": sorted(set(other)),
        "reason": reason,
    }
