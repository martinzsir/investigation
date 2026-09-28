"""
server/app/canvas_case.py
案件级研判画布（P1 / REQ-P1-01~02，PRD V1.0.0 功能 1）。

域升级：线索级（canvas_seed.py，`clue_id` 域）→ 案件级（本模块，`case#` 域）。
纪律：
  - **不改动** canvas_seed.py / canvas_edit.py 的线索域行为；本模块以
    参数化钩子（canvas_edit 的 allowed/validator/labeler）复用其受控变更逻辑；
  - 案件级画布复用 clue_canvas 存储表，`canvas_id = clue_id = "case#<case_id>"`
    （合成域键 eid("case", case_id)），零改表；
  - 节点键一律 `case#` 前缀（人工节点 `case#cn_xxx`），与线索快照域天然隔离；
    旧线索快照不迁移、不受影响（使用文档拍板：新画布不影响已有线索）；
  - 研判节点 4 类（subject/place/event/analysis_result）仅案件级可成节点：
    线索画布形状校验（canvas_seed.validate_doc_shape）不含这些 kind，天然拒入；
  - analysis_result 仅由镜头观察回写产生（P3），人工不可添加。

纯函数纪律与 canvas_edit.py 一致：不开库、不读 Parquet；调用方负责深拷贝/落库。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from server.app import canvas_edit

# 案件级节点全集 = 线索域 10 类 + 研判层 4 类（PRD §2.2）
CASE_NODE_KINDS = frozenset({
    "rule", "fact", "object", "source_row", "source_file",
    "verify_item", "evidence", "hypothesis", "note", "function_result",
    "subject", "place", "event", "analysis_result",
})

# 案件级人工可摆节点：假设/备注 + 人/地/事（analysis_result 仅回写产生）
CASE_MANUAL_NODE_KINDS = frozenset(
    {"hypothesis", "note", "subject", "place", "event"})

# 案件级画布存储域键前缀（canvas_id = "case#<case_id>"）
CASE_PREFIX = "case#"

PERSON_NAME_MAX = 50
EVENT_TITLE_MAX = 50
LOCATION_ID_MAX = 64
PERSON_PK_MAX = 64
STD_ADDRESS_MAX = 200
# 庙算假设编号（如 "H6"）：镜头回写挂「支撑」边的匹配键（P3）
ASSUMPTION_ID_MAX = 16

# 地点坐标精度档（PRD 字段规范；与地理画像降级链路同口径）
COORD_PRECISIONS = frozenset({"门牌级", "区划质心", "无坐标"})
# 事件时间精度档（红线 R1：与前端 canvas-window.ts 同口径）
TIME_PRECISIONS = frozenset({"minute", "date"})

TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d")


class CanvasCaseError(ValueError):
    """案件级画布业务校验失败：路由层统一转 400 VALIDATION。"""


# ----------------------------------------------------------------------
# 域键（eid("case", case_id)）
# ----------------------------------------------------------------------
def case_canvas_id(case_id: str) -> str:
    """案件级画布存储键（复用 clue_canvas 表，canvas_id=clue_id=case#<cid>）。"""
    return f"{CASE_PREFIX}{case_id}"


def is_case_canvas_id(value: Any) -> bool:
    """案件级域识别：旧线索快照（无前缀）返回 False，只读不迁。"""
    return isinstance(value, str) and value.startswith(CASE_PREFIX)


def case_node_id() -> str:
    """案件级人工节点 id：`case#` 前缀 + cn_ 随机段（画布内稳定、全域不撞）。"""
    return f"{CASE_PREFIX}{canvas_edit.manual_node_id()}"


def seed_case_doc() -> dict[str, Any]:
    """案件级种子：空白作战地图（空画布），用户从摆第一个节点开始。"""
    return {"nodes": [], "edges": []}


# ----------------------------------------------------------------------
# 形状校验（结构同 canvas_seed.validate_doc_shape，kind 集换案件级全集）
# ----------------------------------------------------------------------
def validate_case_doc_shape(doc: Any) -> list[str]:
    """案件级文档结构基础校验，返回错误列表（空=通过）。"""
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["画布文档必须是对象 {nodes, edges}"]
    nodes = doc.get("nodes")
    edges = doc.get("edges")
    if not isinstance(nodes, list):
        errs.append("nodes 必须是数组")
    if not isinstance(edges, list):
        errs.append("edges 必须是数组")
    if errs:
        return errs
    node_ids: set[str] = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            errs.append(f"nodes[{i}] 必须是对象")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            errs.append(f"nodes[{i}] 缺少 id")
        elif nid in node_ids:
            errs.append(f"节点 id 重复：{nid}")
        else:
            node_ids.add(nid)
        if n.get("kind") not in CASE_NODE_KINDS:
            errs.append(f"nodes[{i}] 非法 kind：{n.get('kind')!r}")
        for c in ("x", "y"):
            if not isinstance(n.get(c), (int, float)):
                errs.append(f"nodes[{i}].{c} 必须是数值")
        if not isinstance(n.get("pinned"), bool):
            errs.append(f"nodes[{i}].pinned 必须是布尔")
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            errs.append(f"edges[{i}] 必须是对象")
            continue
        if not isinstance(e.get("id"), str) or not e["id"]:
            errs.append(f"edges[{i}] 缺少 id")
        if e.get("source") not in node_ids:
            errs.append(f"edges[{i}] source 不存在：{e.get('source')!r}")
        if e.get("target") not in node_ids:
            errs.append(f"edges[{i}] target 不存在：{e.get('target')!r}")
    return errs


# ----------------------------------------------------------------------
# 研判节点 props 校验（PRD 功能 1 字段规范；前后端同口径，后端硬拒绝）
# ----------------------------------------------------------------------
def _case_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CanvasCaseError(f"{field}必须是文本")
    return value.strip()


def _case_limit(value: str, field: str, limit: int) -> str:
    if not value:
        raise CanvasCaseError(f"请填写{field}")
    if len(value) > limit:
        raise CanvasCaseError(f"{field}不超过 {limit} 字")
    return value


def _case_coord(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanvasCaseError(f"{field}必须是数值")
    return float(value)


def _case_time(value: Any, field: str) -> str:
    """时间串校验：ISO / 常见格式；返回原串（不重排时区，画布只承载表达）。"""
    if not isinstance(value, str) or not value.strip():
        raise CanvasCaseError(f"请填写{field}")
    raw = value.strip()
    for fmt in (*TIME_FORMATS,):
        try:
            datetime.strptime(raw, fmt)
            return raw
        except ValueError:
            continue
    try:
        datetime.fromisoformat(raw)
        return raw
    except ValueError:
        raise CanvasCaseError(f"{field}格式无效（应为 YYYY-MM-DD 或含时刻）")


def validate_case_node_props(kind: str, props: Any) -> dict[str, Any]:
    """校验并清洗案件级人工节点 props；失败抛 CanvasCaseError。

    subject：person_name 必填 1..50；person_pk 可选（重名待裁决时留空，红线 R2）；
             person_ambiguous 归一为布尔，缺省 False
    place  ：location_id 或 lng+lat 二选一必填；coord_precision 枚举可选；
             std_address 可选（label 兜底）
    event  ：title 必填 1..50；time_start 必填、time_end 可选（end≥start）；
             time_precision ∈ {minute,date}，缺省 date
    hypothesis/note：委托线索域校验（canvas_edit.validate_node_props）；
             hypothesis 追加可选 assumption_id（庙算假设编号，回写挂假设
             边的匹配键，P3 canvas_writeback）
    """
    if kind not in CASE_MANUAL_NODE_KINDS:
        raise CanvasCaseError(f"非法案件级人工节点类型：{kind}")
    if not isinstance(props, dict):
        raise CanvasCaseError("节点内容必须是对象")

    if kind == "subject":
        cleaned: dict[str, Any] = {}
        name = _case_limit(_case_text(props.get("person_name"), "主体名称"),
                           "主体名称", PERSON_NAME_MAX)
        cleaned["person_name"] = name
        # 先归一 person_ambiguous（布尔或 "true"/"false" 字符串），
        # 再做 R2 主键检查——否则字符串 "false" 会被误判为重名待裁决
        amb = props.get("person_ambiguous")
        amb_norm: bool | None = None
        if amb is not None:
            if isinstance(amb, bool):
                amb_norm = amb
            elif isinstance(amb, str) and amb.lower() in ("true", "false", "1", "0"):
                amb_norm = amb.lower() in ("true", "1")
            else:
                raise CanvasCaseError("person_ambiguous 必须是布尔")
            cleaned["person_ambiguous"] = amb_norm
        pk = props.get("person_pk")
        if pk is not None and str(pk).strip():
            if amb_norm:
                raise CanvasCaseError("重名待裁决主体主键必须留空（红线 R2）")
            pk = _case_text(pk, "主体主键")
            if len(pk) > PERSON_PK_MAX:
                raise CanvasCaseError(f"主体主键不超过 {PERSON_PK_MAX} 字")
            cleaned["person_pk"] = pk
        return cleaned

    if kind == "place":
        cleaned = {}
        loc = props.get("location_id")
        if loc is not None and str(loc).strip():
            loc = _case_text(loc, "地点标识")
            if len(loc) > LOCATION_ID_MAX:
                raise CanvasCaseError(f"地点标识不超过 {LOCATION_ID_MAX} 字")
            cleaned["location_id"] = loc
        has_lng, has_lat = props.get("lng") is not None, props.get("lat") is not None
        if has_lng:
            lng = _case_coord(props["lng"], "经度")
            if not -180 <= lng <= 180:
                raise CanvasCaseError("经度超出范围（-180~180）")
            cleaned["lng"] = lng
        if has_lat:
            lat = _case_coord(props["lat"], "纬度")
            if not -90 <= lat <= 90:
                raise CanvasCaseError("纬度超出范围（-90~90）")
            cleaned["lat"] = lat
        if has_lng != has_lat:
            raise CanvasCaseError("经纬度必须成对填写")
        if "location_id" not in cleaned and not (has_lng and has_lat):
            raise CanvasCaseError("请选择地点或填写坐标")
        addr = props.get("std_address")
        if addr is not None and str(addr).strip():
            addr = _case_text(addr, "标准地址")
            if len(addr) > STD_ADDRESS_MAX:
                raise CanvasCaseError(f"标准地址不超过 {STD_ADDRESS_MAX} 字")
            cleaned["std_address"] = addr
        prec = props.get("coord_precision")
        if prec is not None and str(prec).strip():
            prec = _case_text(prec, "坐标精度")
            if prec not in COORD_PRECISIONS:
                raise CanvasCaseError("坐标精度必须是 门牌级/区划质心/无坐标")
            cleaned["coord_precision"] = prec
        return cleaned

    if kind == "event":
        cleaned = {}
        title = _case_limit(_case_text(props.get("title"), "事件名称"),
                            "事件名称", EVENT_TITLE_MAX)
        cleaned["title"] = title
        start = _case_time(props.get("time_start"), "开始时间")
        end = props.get("time_end")
        if end is not None and str(end).strip():
            end = _case_time(end, "结束时间")
            if _parse_ts(end) < _parse_ts(start):
                raise CanvasCaseError("结束时间不能早于开始时间")
            cleaned["time_end"] = end
        cleaned["time_start"] = start
        prec = props.get("time_precision")
        prec = "date" if prec is None or not str(prec).strip() else prec
        if prec not in TIME_PRECISIONS:
            raise CanvasCaseError("时间精度必须是 minute/date")
        cleaned["time_precision"] = prec
        return cleaned

    # hypothesis / note：线索域同口径；hypothesis 追加可选 assumption_id
    # （如 "H6"，画布内匹配值不设白名单，仅文本/长度校验）
    cleaned = canvas_edit.validate_node_props(kind, props)
    if kind == "hypothesis":
        aid = props.get("assumption_id")
        if aid is not None and str(aid).strip():
            aid = _case_text(aid, "假设编号")
            if len(aid) > ASSUMPTION_ID_MAX:
                raise CanvasCaseError(
                    f"假设编号不超过 {ASSUMPTION_ID_MAX} 字")
            cleaned["assumption_id"] = aid
    return cleaned


def _parse_ts(raw: str) -> float:
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return datetime.fromisoformat(raw).timestamp()


def case_label(kind: str, props: dict[str, Any]) -> str:
    """案件级节点 label：subject→人名、place→地址/标识/坐标、event→标题；
    其余（hypothesis/note）回落线索域口径。"""
    if kind == "subject":
        return canvas_edit._truncate(str(props.get("person_name", "")))
    if kind == "place":
        text = (str(props.get("std_address", ""))
                or str(props.get("location_id", "")))
        if not text and "lng" in props:
            text = f"{props['lng']},{props['lat']}"
        return canvas_edit._truncate(text or "未命名地点")
    if kind == "event":
        return canvas_edit._truncate(str(props.get("title", "")))
    return canvas_edit._manual_label(kind, props)


# ----------------------------------------------------------------------
# 文档受控变更（委托 canvas_edit 钩子实现，单实现零分叉）
# ----------------------------------------------------------------------
def add_case_node(doc: dict[str, Any], *, kind: str, props: dict[str, Any],
                  node_id: str, x: Any, y: Any,
                  operator: str, now: str) -> dict[str, Any]:
    """新增案件级人工节点（id 由调用方经 case_node_id() 签发）。"""
    return canvas_edit.add_manual_node(
        doc, kind=kind, props=props, node_id=node_id, x=x, y=y,
        operator=operator, now=now, label=case_label(kind, props))


def update_case_node(doc: dict[str, Any], *, node_id: str,
                     props: dict[str, Any], now: str) -> dict[str, Any]:
    """编辑案件级人工节点（按既有 kind 重校验，不允许借编辑改类型）。"""
    return canvas_edit.update_manual_node(
        doc, node_id=node_id, props=props, now=now,
        validator=validate_case_node_props, labeler=case_label,
        allowed=CASE_MANUAL_NODE_KINDS)


def delete_case_node(doc: dict[str, Any], *, node_id: str) -> list[str]:
    """删除案件级人工节点（级联删人工边，同线索域口径）。"""
    return canvas_edit.delete_manual_node(doc, node_id=node_id,
                                          allowed=CASE_MANUAL_NODE_KINDS)
