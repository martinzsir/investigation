"""研判层画布（案件级）：域、节点语义、精度与符号口径。

为什么单独成文件（而不是塞进 canvas_seed）
------------------------------------------
canvas_seed 是**线索级溯源画布**的种子：节点键 ``fact_ref = sha1("{clue_id}|fact|{i}")``
固定在一条线索内，长不成案件总图；节点类型全是证据溯源语义
（fact/object/source_row/source_file），没有研判对象语义（人/地/事/结论）。

研判层画布要的是另一回事：正兵在**案件总图**上摆人、地、事、假设，随手调
镜头，发现的东西长在图上。所以本文件只提供研判层那套域与节点语义，
**不改动 canvas_seed 的既有行为**——线索级画布继续做溯源，两者分工明确。

两套节点键必须可区分，否则同一份画布文档里会撞键：
    线索级（既有，只读）  ``{kind}:{ref}``
    案件级（本文件）      ``case#{case_id}:{kind}:{ref}``

红线（本文件全部落死，逐条有单测）
--------------------------------
R-1 重名/重号绝不自裁：多候选时 pk 置 None、标 ambiguous，绝不静默挑一个。
    系统替正兵决定"这个张卫国是哪个"，后面所有联动都建在可能错误的地基上。
R-2 不可判定返回 None，绝不塞默认值：坐标取不到就是 None，"不知道"不得
    伪装成"精确门牌位置"或"当天过桥"那类伪精确。
R-3 精度档单一真相源：符号（线型/线宽/色）由本文件给出，卡内迷你符号、
    连线、窗口三处共用。否则「12 条 date 档」和「4 条 minute 档」画出来
    一样，前面所有精度加权又被一张漂亮图抹平。
"""
from __future__ import annotations

import hashlib
from typing import Any

# ----------------------------------------------------------------------
# 一、域：案件级节点键（CAN-01）
# ----------------------------------------------------------------------
CASE_KEY_PREFIX = "case#"

# 研判层节点类型（CAN-02）
#   与 canvas_seed.NODE_KINDS 的关系：既有 7 类是**证据溯源**语义，
#   下面 4 类是**研判对象**语义。同一张案件画布上两层共存。
SUBJECT_KINDS = frozenset({"subject"})              # 人 / 组织 / 项目
# 重点物品（车 / 房产 / 发票 / 药品批号）**独立成 kind，不做 subject 子类型**。
# 判据不是"它是不是研判主体"（是），而是**窗口分化**：前端
# canvas-window.ts 的 KIND_TO_WINDOW 里 subject→relation、item→holding，
# 两者窗口语义完全不同。若把物品压成 subject 的子类型，windowKindFor 只看
# kind，物品会弹出关系窗口而持有链窗口永远是死代码——蛇形布局也就永远画不出来。
ITEM_KINDS = frozenset({"item"})
PLACE_KINDS = frozenset({"place"})                  # 地点
EVENT_KINDS = frozenset({"event"})                  # 事件 / 时间窗
RESULT_KINDS = frozenset({"analysis_result"})       # 研判结论（统一来源）

CASE_NODE_KINDS = (SUBJECT_KINDS | ITEM_KINDS | PLACE_KINDS
                   | EVENT_KINDS | RESULT_KINDS)

# 与既有画布合并后的全集（供校验层兜底）
ALL_NODE_KINDS = CASE_NODE_KINDS | frozenset({
    "rule", "fact", "object", "source_row", "source_file",
    "verify_item", "evidence", "hypothesis", "note", "function_result",
})

# 系统边关系（新增提升类）
# P1 补「同现」「联系」：研判边由镜头重建层产出，与人工 4 类零交集。
# 案件级 PATCH 不经 can_connect 校验，但 system_only_rels() 用于防线
# 兜底，少了会让"系统边"集合残缺，将来加校验会踩坑。
SYSTEM_RELS_CASE = frozenset({"提升为", "位于", "发生于", "支撑", "涉及",
                              "研判得出", "持有", "同现", "联系"})

# person → item 的持有关系（R2/D2 时态边）。
# 为什么必须是时态边：只画静态边"甲→车、乙→车"会被读成"两人共用一辆车"，
# 而实际可能是"甲卖给了乙"——**性质完全相反的两种事实**。
# start/end 缺失即区间不可判定，由 core/hold_chain.py 标 unknownRange，
# 绝不静默默认为"无约束"（那会把"时间没记录"伪装成"一直持有"）。
HOLD_REL = "持有"

# object → subject 的提升关系（CAN-08）
PROMOTE_REL = "提升为"

# 靶心 → 研判结论（CAN-19）：结论挂在**发起它的**那个节点下，不游离。
# 与人工关系 4 类（推断为/证实/查否/补充说明）零交集，人工连不出这条边。
LENS_RESULT_REL = "研判得出"

# 镜头重建层产出标记（案件级画布持久化用）
# ------------------------------------------------------------------
# 为什么需要：案件级画布要落库，但**镜头层不该落**——它由定向观察档案
# 每次 GET 重建，若连它一起存，重扫后库里那份就是过期的，且再也不会被
# 覆盖（档案变了、库里没变），正兵看到的是一张僵死的图。
#
# 反过来，**人工发起的东西必须落**：人工 hypothesis/note、提升产生的
# subject、人工边、以及所有节点的坐标与钉住——这些档案里没有，丢了就
# 再也回不来。
#
# 所以判据不是"system 真假"（提升的 subject 也是 system=True，但它必须
# 落库），而是**来源**：镜头重建层统一打这个标记，落库前按它剥离。
GENERATED_BY_LENS = "lens_layer"

# 物品数据源重建层：与镜头层同属"每次重建、不落库"，但来源不同，
# 分开标记才能在落库剥离与前端标注时区分（正兵看得出这批节点从哪来）。
GENERATED_BY_ITEM = "item_source"


def case_node_id(kind: str, case_id: str, ref: str) -> str:
    """案件级节点画布内稳定 id。

    带 ``case#`` 前缀是为了与线索级 ``{kind}:{ref}`` **天然不撞键**——
    两者共存于同一文档时，靠前缀即可分辨，无需额外作用域字段。
    """
    return f"{CASE_KEY_PREFIX}{case_id}:{kind}:{ref}"


def case_edge_id(source: str, rel: str, target: str) -> str:
    """案件级边 id：与既有人工边同口径 ``e:{src}--{rel}--{tgt}``，天然幂等。"""
    return f"e:{source}--{rel}--{target}"


def parse_case_node_id(node_id: str) -> dict[str, str] | None:
    """反解案件级节点 id；非案件级返回 None（旧画布只读不迁的依据）。"""
    s = str(node_id or "")
    if not s.startswith(CASE_KEY_PREFIX):
        return None
    body = s[len(CASE_KEY_PREFIX):]
    cid, sep, rest = body.partition(":")
    if not sep:
        return None
    kind, sep2, ref = rest.partition(":")
    if not sep2:
        return None
    return {"case_id": cid, "kind": kind, "ref": ref}


# ----------------------------------------------------------------------
# 二、精度档（SYM-01）：单一真相源
# ----------------------------------------------------------------------
# 为什么必须有：date 档只意味着"前后一天先后出现"，是同地异时，每一次都是
# 独立巧合；minute 档能判时间窗真重叠。权重与符号都必须体现这个差别。
PRECISION_RANK = {
    "unknown": 0,
    "year": 1,
    "month": 2,
    "date": 3,
    "hour": 4,
    "minute": 5,
    "second": 6,
}
PRECISION_WEIGHT = {
    "unknown": 0.0,
    "year": 0.05,
    "month": 0.1,
    "date": 0.2,
    "hour": 0.5,
    "minute": 1.0,
    "second": 1.0,
}
# 可判"时间窗真重叠"的档位（业务上才配叫"同框/见面"）
OVERLAPPABLE = frozenset({"minute", "second"})


def norm_precision(value: Any) -> str:
    """把各种写法归一到档位名；无法识别返回 'unknown'（R-2：不猜）。"""
    s = str(value or "").strip().lower()
    if s in PRECISION_RANK:
        return s
    alias = {
        "min": "minute", "minute级": "minute", "时刻": "minute",
        "sec": "second", "秒": "second",
        "day": "date", "日": "date", "日期级": "date",
        "h": "hour", "时": "hour",
        "mon": "month", "月": "month",
        "y": "year", "年": "year",
    }
    return alias.get(s, "unknown")


def can_overlap(precision: Any) -> bool:
    """该精度能否判定时间窗真重叠。unknown 一律 False——不可判定按无处理。"""
    return norm_precision(precision) in OVERLAPPABLE


def weaker_precision(a: Any, b: Any) -> str:
    """木桶效应：取较低档。

    坑（曾在地图侧踩过）：初始值若用 'unknown'（最弱档），一参与比较就
    永远赢，把真实精度吞掉。所以首次命中必须**直接采用**，之后才比较——
    调用方传入 ``None`` 表示"尚未命中"。
    """
    pa, pb = norm_precision(a), norm_precision(b)
    if a is None:
        return pb
    if b is None:
        return pa
    return pa if PRECISION_RANK[pa] <= PRECISION_RANK[pb] else pb


# ----------------------------------------------------------------------
# 三、符号口径（SYM-02）：卡内 / 连线 / 窗口三处共用
# ----------------------------------------------------------------------
def line_style(precision: Any) -> dict[str, Any]:
    """连线样式：实线粗边=可判重叠；虚线细边=仅同地异时。"""
    p = norm_precision(precision)
    if can_overlap(p):
        return {"stroke_dasharray": None, "line_width": 3.0, "precision": p,
                "note": "时刻级：可判定时间窗真重叠"}
    return {"stroke_dasharray": "4 3", "line_width": 1.5, "precision": p,
            "note": ("日期级：仅知前后一天先后出现，是同地异时，非同时"
                     if p == "date" else f"{p} 档：精度不足以判定重叠")}


def precision_color(precision: Any) -> str:
    """精度色点：与前端 dimDotColor 同口径。"""
    p = norm_precision(precision)
    return {
        "second": "#1a7f37", "minute": "#1a7f37",
        "hour": "#9a6700", "date": "#9a6700",
        "month": "#6e7781", "year": "#6e7781",
        "unknown": "#8b949e",
    }.get(p, "#8b949e")


def symbol(precision: Any) -> dict[str, Any]:
    """节点符号口径汇总（卡内迷你符号 + 地图点 + 连线共用一份）。"""
    p = norm_precision(precision)
    return {
        "precision": p,
        "weight": PRECISION_WEIGHT.get(p, 0.0),
        "color": precision_color(p),
        "solid": can_overlap(p),          # 实心=可判重叠；空心=仅同地异时
        "line_width": line_style(p)["line_width"],
        "dash": line_style(p)["stroke_dasharray"],
    }


# ----------------------------------------------------------------------
# 三·补、观察的维度与精度派生（CAN-19 建节点与窗口读侧共用一份）
# ----------------------------------------------------------------------
# 为什么上提到领域层：这两个函数原先定义在 canvas_window_view（读侧装配），
# 而 CAN-19 的"观察 → 研判结论节点"同样要用。**再写一份就是分叉**——同一条
# 观察在证据窗口里是 minute 档、挂到画布上却标成 date 档，正是本项目反复
# 踩过的坑。故上提到领域层，读侧改为导入（模块级名字保持不变，既有调用方
# 与单测无感）。

def dim_of(skill_id: Any) -> str | None:
    """skill_id → 维度。与 convergence_view 同口径，不另写一套。"""
    sid = str(skill_id or "")
    if sid.startswith("geo_"):
        return "space"
    if sid.startswith("timeline_"):
        return "time"
    if sid.startswith("relation_"):
        return "relation"
    return None


def better_precision(a: str, b: str) -> str:
    """取较高档（一条观察内部：最精确的那条事实行说了算）。"""
    try:
        from core.convergence import _weaker
        return b if _weaker(a, b) == a else a
    except Exception:
        return a


def obs_precision(o: dict) -> str:
    """一条观察的精度 = 它最精确的那条事实行。

    派生口径复用 core.convergence（_dt_precision / _weaker），不在这里重写。

    兜底：geo 类镜头（伴随/异常轨迹等）不落 ``facts`` 事实行，但会在
    ``detail.time_granularity`` 显式输出事件时间档（如 "minute"——
    "事件精确到分钟及以上"）。不读它，12 次分钟级同框会被降级成
    unknown 虚线细边，与"线型跟着精度走"的规矩直接冲突。脏值经
    norm_precision 归一，认不出一律 unknown，不硬猜。
    """
    try:
        from core.convergence import _dt_precision
    except Exception:
        return "unknown"
    best = "unknown"
    for f in (o.get("facts") or [])[:20]:
        if not isinstance(f, dict):
            continue
        for key in ("at", "time", "timestamp", "start", "date", "end"):
            v = f.get(key)
            if not v:
                continue
            p = _dt_precision(v) or "unknown"
            best = better_precision(best, p)
    if best == "unknown":
        detail = o.get("detail")
        if isinstance(detail, dict):
            best = norm_precision(detail.get("time_granularity"))
    return best or "unknown"


# ----------------------------------------------------------------------
# 四、subject 节点（CAN-03）：锚 person_pk，重名不自裁
# ----------------------------------------------------------------------
def build_subject_node(*, case_id: str, name: Any, kind: str = "subject",
                       sub_type: str = "person", conn: Any = None,
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造研判主体节点。

    R-1：名字对应多个主键时 **pk 置 None + ambiguous**，绝不在候选里挑一个。
    正兵看到的是"这个人指代不明"，而不是系统替他选好的某个人——后者会让
    空间/时间/关系三维证据错配到另一个人身上，且完全看不出错了。
    """
    from core.entity_ref import pk_candidates

    nm = str(name or "").strip()
    pks = pk_candidates_for(conn, nm, sub_type) if (conn is not None and nm) else []
    unique_pk = pks[0] if len(pks) == 1 else None

    # 同名异人（identity 层强证据互斥）必须与"多候选"分开看：
    # obj_person 分列未落库时 pks 只有 1 个，但人确实有两个。此时若照常
    # 判 anchored/非 ambiguous，等于静默替正兵挑了一个人。
    cf = identity_conflict(conn, nm) if (conn is not None and nm) else {}
    conflict = bool(cf.get("conflict"))

    # 无连接/查不到：不得伪造 pk（R-2）。标 unavailable 让调用方看得见原因。
    ref = unique_pk or (hashlib.sha1(nm.encode("utf-8")).hexdigest()[:12]
                        if nm else "unknown")
    node = {
        "id": case_node_id(kind, case_id, ref),
        "kind": kind,
        "system": True,
        "label": nm or "未命名主体",
        "props": {
            "sub_type": sub_type,
            "name": nm,
            "person_pk": unique_pk,              # 多候选时为 None（R-1）
            # 多候选（已分列）与"同名异人待分列"都算指代不明，但处置不同，
            # 用 pk_status 区分，前端不得只凭一个布尔值下判断。
            "person_pk_ambiguous": len(pks) > 1 or conflict,
            "pk_status": ("multi" if len(pks) > 1 else
                          "homonym_pending" if conflict else
                          "unique" if unique_pk else "none"),
            "pk_candidates": pks,
            "identity_rows": int(cf.get("row_count") or 0),
            "identity_evidence": str(cf.get("evidence") or ""),
            "pk_resolution": _pk_resolution_text(conn, pks, nm, conflict, cf),
            # 同名异人待分列时**不算已锚定**：pk 只是"暂用"，不得据此
            # 得出确定性结论。person_pk 仍保留（节点要有 ref，且正兵要
            # 看得见暂用的是哪一个），但 anchored=False 让依赖它的逻辑
            # 一律走"须先裁决"分支。
            "anchored": unique_pk is not None and not conflict,
        },
    }
    if extra:
        node["props"].update(extra)
    return node


def org_pk_candidates(conn: Any, name: str) -> list[str]:
    """组织主键候选（obj_org）。

    为什么不能复用 ``pk_candidates``：它只查 ``obj_person`` /
    ``obj_person_identity``，组织一律查不到——于是名录里"宏业建设"这种
    真实收录的组织被判成"语义层未收录"，usable=False。正兵看到的是
    "库里没这家单位"，而实际上它就在库里，只是走错了表。
    """
    if conn is None or not str(name or "").strip():
        return []
    try:
        rows = conn.execute(
            "SELECT org_id FROM obj_org WHERE raw_name = ?",
            [str(name).strip()]).fetchall()
    except Exception:
        return []
    return sorted({str(r[0]) for r in rows or [] if r and r[0]})


def pk_candidates_for(conn: Any, name: str, sub_type: str = "person") -> list[str]:
    """按主体子类型分派主键查询——人走 identity，组织走 obj_org。

    未来 item（物品）走 obj_item；此刻没有该表则返回空（不编键）。
    """
    from core.entity_ref import pk_candidates
    if sub_type == "organization":
        return org_pk_candidates(conn, name)
    return list(pk_candidates(conn, name) or [])


def identity_conflict(conn: Any, name: str) -> dict[str, Any]:
    """同名异人判定（identity 层），只看**强证据互斥**，不露明文。

    为什么必须单独查这一层
    ----------------------
    ``pk_candidates`` 只查 ``obj_person``（分列未落库时同名的两行仍挤在
    1 个主键里），命中即返回，于是"库里有两个张卫国（两个身份证号）"
    这个事实被**静默吞掉**——系统照常锚定唯一主键，界面显示"已锚定"，
    正兵完全看不出这个人指代不明，后续所有研判都建在可能错的地基上。

    这是本轮实测抓到的红线失效：``build_subject_node('张卫国')`` 原本
    返回 ``ambiguous=False / anchored=True``。

    只输出**性质**文案（"身份证号互斥"），绝不输出号码本身——
    id_card/phone 属敏感数据元，候选区分信息只能给性质不能给明文。
    """
    out: dict[str, Any] = {"conflict": False, "row_count": 0,
                           "evidence": "", "checked": False}
    if conn is None or not str(name or "").strip():
        return out
    def _col(row: Any, i: int) -> str:
        """按位置取值，缺列返回空串——不假设 SELECT 的列数一定齐。"""
        try:
            v = row[i]
        except (IndexError, TypeError, KeyError):
            return ""
        return str(v or "").strip()
    try:
        rows = conn.execute(
            "SELECT id_card, phone FROM obj_person_identity WHERE raw_name = ?",
            [str(name).strip()]).fetchall()
    except Exception:
        return out          # 表不存在/查询失败 → 未核对，不谎报无冲突
    out["checked"] = True
    out["row_count"] = len(rows)
    if len(rows) < 2:
        return out

    # 列数不可假设：obj_person_identity 在不同装载批次下可能只有 id_card
    # 没有 phone（就像早前「证号列索引写死 row[2]」那次——无电话列时越界，
    # 且是静默的）。取值一律走 _col，缺列当空处理，绝不 IndexError。
    cards = {_col(r, 0) for r in rows}
    phones = {_col(r, 1) for r in rows}
    cards.discard("")
    phones.discard("")
    ev: list[str] = []
    if len(cards) > 1:
        ev.append(f"{len(cards)} 个身份证号互斥")
    if len(phones) > 1:
        ev.append(f"{len(phones)} 个手机号互斥")
    if ev:
        out["conflict"] = True
        out["evidence"] = "、".join(ev)
    return out


def _pk_resolution_text(conn: Any, pks: list[str], name: str,
                        conflict: bool = False,
                        cf: dict[str, Any] | None = None) -> str:
    """主体指代状态的自陈文案——正兵据此决定要不要先消歧。"""
    if not name:
        return "主体名为空，未锚定"
    if conn is None:
        return ("未连接语义层，无法核对同名异人；主键未锚定，"
                "该节点无可用研判")
    if len(pks) > 1:
        return (f"「{name}」存在 {len(pks)} 个候选主键（同名异人），"
                f"未锚定；须人工裁决后再挂接证据")
    if conflict:
        ev = str((cf or {}).get("evidence") or "强证据互斥")
        n = int((cf or {}).get("row_count") or 0)
        return (f"「{name}」已判定同名异人（{ev}，共 {n} 条身份记录），"
                f"但实体分列尚未落库：语义层仍只有 1 个主键，"
                f"须人工确认分列后重建")
    if not pks:
        return f"语义层未收录「{name}」，该节点未锚定，无可用研判"
    return "已锚定唯一主键"


# ----------------------------------------------------------------------
# 五、place 节点（CAN-04）：经纬度 + 坐标精度档
# ----------------------------------------------------------------------
def build_place_node(*, case_id: str, location_id: Any,
                     label: str = "", conn: Any = None,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造地点节点。

    坐标**必须带来源自陈**（coord_degraded）。区划质心与门牌级在地图上
    长得一样，只给经纬度，正兵会把"区级推算"读成"精确门牌位置"——
    那是空间侧 distance_m=0.0 那类伪精确在地图上的翻版，而地图天生看上去
    精确，误导性比列表更强。

    质心判定复用 core.geo._is_centroid_coord（单一真相源），不在此复制常量。
    """
    lid = str(location_id or "").strip()
    row: dict[str, Any] | None = None
    if conn is not None and lid:
        try:
            cur = conn.execute(
                "SELECT location_id, std_address, lat, lng, coord_sys, "
                "geocode_source, geocode_confidence FROM obj_location "
                "WHERE location_id = ?", [lid])
            cols = [d[0] for d in cur.description]
            r = cur.fetchone()
            if r:
                row = dict(zip(cols, r))
        except Exception:
            row = None

    lat = lng = None
    degraded: bool | None = None
    coord_note: str | None = None
    if row:
        lat, lng = row.get("lat"), row.get("lng")
        if lat is not None and lng is not None:
            try:
                from core.geo import _is_centroid_coord
                degraded = bool(_is_centroid_coord(row))
                coord_note = ("坐标为区划质心，非门牌位置，不代表实际间距"
                              if degraded else None)
            except Exception:
                degraded = None      # 判不出来就声明判不出来（R-2）
                coord_note = "坐标精度档未能判定，地图按未知档处理"
        else:
            coord_note = "该地点无坐标，地图视图不可用，仅保留地址文本"
    else:
        coord_note = ("未取到地点实体（无坐标或不达语义层）"
                      if conn is not None else "未连接语义层，坐标不可知")

    node = {
        "id": case_node_id("place", case_id, lid or "unknown"),
        "kind": "place",
        "system": True,
        "label": str(label or (row or {}).get("std_address") or lid or "未命名地点"),
        "props": {
            "location_id": lid or None,
            "std_address": (row or {}).get("std_address"),
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "coord_sys": (row or {}).get("coord_sys"),
            "geocode_source": (row or {}).get("geocode_source"),
            # 地图视觉分档的唯一依据：质心档不得画成实心精确点
            "coord_degraded": degraded,
            "coord_note": coord_note,
            "mappable": lat is not None and lng is not None,
        },
    }
    if extra:
        node["props"].update(extra)
    return node


# ----------------------------------------------------------------------
# 六、event 节点（CAN-05）：时刻 + 精度档
# ----------------------------------------------------------------------
def build_event_node(*, case_id: str, event_ref: Any, label: str = "",
                     occurred_at: Any = None, precision: Any = None,
                     location_id: Any = None,
                     extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造事件节点。

    时刻与精度档**必须一起给**：只有时刻没有精度，时间窗口无从判断能否
    重叠；只有精度没有时刻，时间轴无从落位。两者缺一都标 incomplete。
    """
    ref = str(event_ref or "").strip() or "unknown"
    p = norm_precision(precision)
    has_time = occurred_at is not None and str(occurred_at).strip() != ""
    node = {
        "id": case_node_id("event", case_id, ref),
        "kind": "event",
        "system": True,
        "label": str(label or ref),
        "props": {
            "event_ref": ref,
            "occurred_at": occurred_at,
            "precision": p,
            "precision_weight": PRECISION_WEIGHT.get(p, 0.0),
            "can_overlap": can_overlap(p),
            "location_id": str(location_id) if location_id else None,
            "time_axis_ready": has_time,
            "incomplete": (not has_time) or p == "unknown",
            "precision_note": (None if has_time and p != "unknown"
                               else "时刻或精度档缺失，时间轴不可用"),
        },
    }
    if extra:
        node["props"].update(extra)
    return node


# ----------------------------------------------------------------------
# 七、analysis_result 节点（CAN-06）：统一命名，来源写进 props
# ----------------------------------------------------------------------
def build_result_node(*, case_id: str, result_ref: Any, label: str = "",
                      lens_id: Any = None, function_id: Any = None,
                      assumption: Any = None, precision: Any = None,
                      dims: Any = None, dim_cells: dict[str, Any] | None = None,
                      extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """研判结论节点。

    为什么不用既有的 ``function_result``：canvas_seed._lens_intervals 把
    Lens 产出也叫 function_result，**同一名字两种来源**——前端无法按 kind
    判断该节点该怎么渲染。这里统一成 analysis_result，来源写进 props。

    ``label`` 同时写顶层与 props：案件画布前端取名链只读 props
    （CanvasView.nodeLabelOf），只写顶层会让节点回退显示裸 kind
    "analysis_result"。dims 保留字符串列表（既有调用方/测试契约），三维
    矩阵另放 ``dim_cells``——前端证据窗口 dimCellsOf 消费的是
    ``{space:{hit,count,precision,weight}}`` 对象，发字符串数组会让三维
    全 miss、加权分恒 0。
    """
    ref = str(result_ref or "").strip() or "unknown"
    p = norm_precision(precision)
    dl = [str(d) for d in (dims or []) if str(d)]
    text_label = str(label or ref)
    node = {
        "id": case_node_id("analysis_result", case_id, ref),
        "kind": "analysis_result",
        "system": True,
        "label": text_label,
        "props": {
            # 与顶层同值：前端两条取名链（顶层 label / props 取名）都能命中
            "label": text_label,
            "result_ref": ref,
            "lens_id": str(lens_id) if lens_id else None,
            "function_id": str(function_id) if function_id else None,
            "origin": ("lens" if lens_id else
                       ("function" if function_id else "unknown")),
            "assumption": assumption,          # 单一归属；多归属见 attach
            "assumptions": ([str(assumption)] if assumption else []),
            "precision": p,
            "precision_weight": PRECISION_WEIGHT.get(p, 0.0),
            "dims": dl,
            # 三维证据矩阵（对象形状，前端 dimCellsOf 直接消费）
            "dim_cells": dict(dim_cells or {}),
            "dim_count": len(dl),
        },
    }
    if extra:
        node["props"].update(extra)
    # 标记必须最后写且强制：extra 是调用方自由传入的，不该有能力抹掉
    # 重建层标记——标记一丢，落库时就剥不干净，系统层会僵死在库里。
    node["props"]["generated_by"] = GENERATED_BY_LENS
    return node


# 物品类型代码 → 规范中文名：真源在本体 enum_meta（DE_ITEM_TYPE），
# 由 core.item 派生，此处不再落字面量（改词汇改 JSON，不改本文件）。
from core.item import ITEM_TYPE_LABELS


def build_item_node(*, case_id: str, title: Any, item_type: Any,
                    identifiers: list | None = None,
                    descriptors: dict | None = None,
                    lat: Any = None, lng: Any = None,
                    precision: Any = None,
                    holder_raw: Any = None,
                    acquire_date: Any = None, dispose_date: Any = None,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造物品节点（人工登记入图，ITM 画布层）。

    三条不能越的线
    --------------
    1. **身份键必须含 item_type**。本体 identity_key 是
       ``[item_type, primary_digest]``，而 digest 只带标识符种类前缀、不带
       物品类型——车牌 "A123" 与序列号 "A123" 的 digest **完全一样**。若 id
       只取 digest，一辆车和一台设备会被合并成同一个节点。

    2. **凭证与特征全空时拒绝建节点**，不静默给一个共享 ref。返回空 digest
       的两件物品若共用 ref，会被当成同一件——这是"无凭证不静默合并"（D4）
       在画布层的落点，报错好过静默混淆。

    3. **坐标缺失标 mappable=False，绝不塞 0**。塞 0 会把"不知道在哪"
       画成"在几内亚湾"，而地图天生看上去精确。
    """
    from core.item import ITEM_IDENTIFIER_KINDS, build_identifier, primary_digest

    it = str(item_type or "").strip()
    if it not in ITEM_TYPE_LABELS:
        raise ValueError(
            f"未知物品类型：{item_type!r}（允许 {sorted(ITEM_TYPE_LABELS)}）")

    ids: list[dict] = []
    for raw in (identifiers or []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        if kind not in ITEM_IDENTIFIER_KINDS:
            raise ValueError(
                f"未知标识符种类：{kind!r}（允许 {list(ITEM_IDENTIFIER_KINDS)}）")
        ids.append(build_identifier(
            kind, raw.get("value"),
            issuer=str(raw.get("issuer") or ""),
            verified=raw.get("verified")))

    desc = {str(k): v for k, v in (descriptors or {}).items()
            if str(k) and v not in (None, "")}
    dig = primary_digest(ids, desc or None)
    if not dig:
        raise ValueError(
            "物品既无凭证也无特征描述：无法建立可区分实体。"
            "请至少提供一项 identifiers 或 descriptors，否则会与其他无凭证"
            "物品静默合并（D4）。")

    ref = f"{it}:{dig}"
    has_cred = any(i.get("kind") != "none" and i.get("value_digest") for i in ids)
    nlat, nlng = lat, lng
    if nlat is None or nlng is None:
        nlat = nlng = None
    p = norm_precision(precision)
    node = {
        "id": case_node_id("item", case_id, ref),
        "kind": "item",
        "system": False,                 # 人工登记：可改可删，不是系统产出
        "label": str(title or "").strip() or ITEM_TYPE_LABELS[it],
        "props": {
            "sub_type": it,
            "item_type": it,
            "item_type_title": ITEM_TYPE_LABELS[it],
            "title": str(title or "").strip(),
            "primary_digest": dig,
            # 标识符只存摘要（build_identifier 已保证无明文字段）
            "identifiers": ids,
            "identifier_kinds": [i.get("kind") for i in ids],
            "sensitive_kinds": sorted({i.get("kind") for i in ids
                                       if i.get("sensitive")}),
            "credentialed": bool(has_cred),
            "unidentified": not has_cred,
            "descriptors": desc or None,
            "lat": nlat, "lng": nlng,
            "mappable": nlat is not None and nlng is not None,
            "coord_note": (None if (nlat is not None and nlng is not None)
                           else "未提供坐标，不参与地图落点"),
            "holder_raw": str(holder_raw or "").strip() or None,
            "acquire_date": acquire_date, "dispose_date": dispose_date,
            "precision": p,
            "precision_weight": PRECISION_WEIGHT.get(p, 0.0),
        },
    }
    if extra:
        node["props"].update(extra)
    return node


def item_hold_records(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """从画布图里抽持有记录，喂给 core.hold_chain。

    方向只认 **person/org → item**：反向边是建模错，静默纠正会让错误一直
    藏着——宁可不画，也不画反。
    """
    kind_by: dict[str, str] = {}
    label_by: dict[str, str] = {}
    for n in (nodes or []):
        nid = str(n.get("id") or "")
        if not nid:
            continue
        kind_by[nid] = str(n.get("kind") or "")
        label_by[nid] = str(n.get("label") or "")
    out: list[dict] = []
    for e in (edges or []):
        s = str(e.get("source") or e.get("from") or "")
        t = str(e.get("target") or e.get("to") or "")
        if kind_by.get(t) != "item":
            continue
        if kind_by.get(s) not in ("subject", "person", "org"):
            continue
        pr = (e.get("props") or {}) if isinstance(e.get("props"), dict) else {}
        out.append({
            "holder": label_by.get(s, s),
            "item": label_by.get(t, t),
            "start_date": pr.get("start_date"),
            "end_date": pr.get("end_date"),
        })
    return out


# ----------------------------------------------------------------------
# 八、提升：object → subject（CAN-08）
# ----------------------------------------------------------------------
def promote_edge(*, case_id: str, object_node_id: str,
                 subject_node: dict[str, Any]) -> dict[str, Any]:
    """证据实体 → 研判主体的系统提升边。

    为什么是"新建 + 连边"而不是"原地改"
    -----------------------------------
    既有 object 节点 ``system=true``，人工增删改一律拒绝；其 props 只有
    type/type_title/pk，写死不可变。所以提升只能是：**新建一个锚定真实主键
    的 subject 节点，与原节点建边**，原节点保留在溯源层（保真，随时能钻回去）。
    """
    return {
        "id": case_edge_id(object_node_id, PROMOTE_REL,
                           subject_node["id"]),
        "source": object_node_id,
        "target": subject_node["id"],
        "rel": PROMOTE_REL,
        "system": True,           # 可删不可改
        "note": "由证据实体提升为研判主体",
    }


# ----------------------------------------------------------------------
# 九、假设自动入图与自动连线（HYP-01 ~ HYP-04）
# ----------------------------------------------------------------------
# 为什么能自动：线索层早就自动挂假设了（hypothesis_patterns.json 的
# rule_ids → 假设），7 条线索全部带 assumption_chain。断的只是画布层三处：
#   1) hypothesis 只存在于 MANUAL_NODE_KINDS（只能手工加）
#   2) 画布节点不带假设归属
#   3) 没有"系统推断边"这种边（只有系统溯源边与人工边）
# 本节补齐这三处，让假设节点自己长出来、证据自己连上去。
#
# 两条克制（刻意不做）：
#   · 系统只出「推断为」，**不出「查否」**——反驳方向需要判断，系统没有
#     "这条证据反驳了假设"的知识，它只知道归属。方向留人工改。
#   · 多归属**如实出多条边**，绝不静默挑一条（与 R-1 同源）。

# 系统推断边：视觉上必须与人工确认边可辨（灰细线 vs 实粗线）
INFER_REL = "推断为"


def _hypothesis_index(pack: str = "default",
                      base_dir=None) -> dict[str, dict[str, Any]]:
    """从本体加载假设索引 {H1: {...}}——标题/证伪条件一律取本体，不手写。"""
    from core.lens_assumption import _load_patterns
    out: dict[str, dict[str, Any]] = {}
    for p in _load_patterns(pack, base_dir):
        h = (p or {}).get("hypothesis") or {}
        hid = str(h.get("id") or "").strip()
        if hid:
            out[hid] = h
    return out


def collect_assumptions(nodes: list[dict[str, Any]]) -> list[str]:
    """扫描画布上研判节点，收集出现过的假设 id（去重、按首次出现序）。"""
    seen: list[str] = []
    for n in (nodes or []):
        props = n.get("props") or {}
        vals: list[str] = []
        a = props.get("assumption")
        if a:
            vals.append(str(a))
        for x in (props.get("assumptions") or []):
            if x:
                vals.append(str(x))
        for v in vals:
            if v not in seen:
                seen.append(v)
    return seen


def build_hypothesis_node(*, case_id: str, hypothesis_id: str,
                          pack: str = "default",
                          base_dir=None) -> dict[str, Any]:
    """构造假设节点；标题与证伪条件取本体，未知 id 也要如实标注。"""
    idx = _hypothesis_index(pack, base_dir)
    h = idx.get(str(hypothesis_id)) or {}
    desc = str(h.get("description") or "").strip()
    fals = str(h.get("falsification") or "").strip()
    known = str(hypothesis_id) in idx
    return {
        "id": case_node_id("hypothesis", case_id, str(hypothesis_id)),
        "kind": "hypothesis",
        "system": True,
        "label": f"{hypothesis_id}　{desc}" if desc else str(hypothesis_id),
        "props": {
            "hypothesis_id": str(hypothesis_id),
            "title": desc or None,
            "falsification": fals or None,
            "known_in_ontology": known,
            # 镜头重建层标记：假设节点由结论的归属反查得出，随档案重建
            "generated_by": GENERATED_BY_LENS,
            "note": (None if known else
                     f"假设 {hypothesis_id} 未在本体模式库声明，"
                     f"无法给出证伪条件"),
        },
    }


def build_hypothesis_layer(*, case_id: str, nodes: list[dict[str, Any]],
                           pack: str = "default",
                           base_dir=None) -> dict[str, Any]:
    """HYP-01/02：把画布上出现过的假设自动建成节点。

    同一假设只建一个节点，无论多少证据指向它——集中呈现才能回答
    "这个假设下挂了几条证据、几维命中"。
    """
    ids = collect_assumptions(nodes)
    hyp_nodes = [build_hypothesis_node(case_id=case_id, hypothesis_id=h,
                                       pack=pack, base_dir=base_dir)
                 for h in ids]
    return {"nodes": hyp_nodes, "ids": ids,
            "missing": [n["props"]["hypothesis_id"] for n in hyp_nodes
                        if not n["props"]["known_in_ontology"]]}


def build_infer_edges(*, case_id: str, result_nodes: list[dict[str, Any]],
                      ) -> list[dict[str, Any]]:
    """HYP-03/04：研判结论 → 假设 的系统推断边。

    精度档跟随结论节点（R-3）：时刻级实线粗边、日期级虚线细边，
    否则「12 条 date 档」和「4 条 minute 档」在图上长得一样。
    """
    edges: list[dict[str, Any]] = []
    for n in (result_nodes or []):
        props = n.get("props") or {}
        hyps = [str(h) for h in (props.get("assumptions")
                                 or ([props["assumption"]]
                                     if props.get("assumption") else [])) if h]
        p = props.get("precision")
        st = line_style(p)
        for h in hyps:
            tgt = case_node_id("hypothesis", case_id, h)
            edges.append({
                "id": case_edge_id(n["id"], INFER_REL, tgt),
                "source": n["id"],
                "target": tgt,
                "rel": INFER_REL,          # 只出推断为，不出查否
                "system": True,            # 可删不可改
                "precision": st["precision"],
                "stroke_dasharray": st["stroke_dasharray"],
                "line_width": st["line_width"],
                "generated_by": GENERATED_BY_LENS,
                "note": f"系统推断为 {h}；方向（证实/查否）待人工确认",
            })
    return edges
