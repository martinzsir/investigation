"""案件级研判画布端点（研判层画布持久化）。

为什么单独一套端点，而不是复用 routers/canvas.py
------------------------------------------------
两者 id 体系不同，混用会互相覆盖：
  线索级 {kind}:{ref}                  —— 锁在单条线索内
  案件级 case#{case_id}:{kind}:{ref}   —— 可跨线索并图
服务用途也不同：线索画布回答"这条线索凭什么"（溯源），研判画布回答
"这个案子查到什么、下一步查什么"（生长）。放一起会让两者都被迫迁就对方。

GET 合并镜头重建层、PATCH 剥离它
--------------------------------
镜头层由定向观察档案每次 GET 重建，**不落库**——落了就会僵死（档案变了、
库里没变、且永不覆盖）。所以：
  GET   = 库里的人工层 + 档案重建的镜头层（合并后返回）
  PATCH = 前端整文档 → 剥掉镜头层 → 只存人工层
剥离/合并都是纯函数（server/app/canvas_case_doc.py），可单测。

纪律
----
- 越权统一 404（_get_owned_case），与既有读面同口径 fail-closed；
- 版本基准过期 → 409（RC-205），服务端不静默覆盖他人版本；
- 写审计链（RC-401）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.audit import AuditChain
from core.item import normalize_identifier_kind

from server.app import canvas_case_doc, canvas_growth
from server.app import canvas_item_source
from server.app.canvas_case import build_item_node
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import (APIError, ERR_CONFLICT, ERR_NOT_FOUND,
                                 ERR_VALIDATION, ok)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_store import (
    CanvasNotFound,
    CanvasVersionConflict,
    StateStore,
)

router = APIRouter(tags=["canvas-case"])
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class CaseCanvasPatch(BaseModel):
    doc: dict
    expected_version: int | None = None


def _state(ctx: WebContext, case_id: str) -> StateStore:
    return StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")


def _audit(state: StateStore, *, case_id: str, version: int | None,
           operator: str, action: str) -> None:
    """写审计（RC-401）；缺失版本 anchor missing 兜底，不让审计拖垮写路径。"""
    try:
        ver = f"v{version}" if version else "unknown"
        chain = AuditChain(state.conn, case_id=case_id, backend="sqlite",
                           ontology_version=ver)
        chain.append(operator=operator, before=None, after=None,
                     source_row_ids=[], ontology_version=ver)
    except Exception:
        logger.warning("case canvas audit 失败 case=%s action=%s",
                       case_id, action, exc_info=True)


def _case_read_conn(ctx, case_id: str):
    """取案件只读连接，供物品层做持有人重名裁决。

    拿不到就返回 None（不抛）：物品层会用名字哈希建节点，同时在 meta 里
    标注"未连接语义层"。**不静默假装已消歧**——张卫国有两个证号，未消歧
    的主体节点 id 与已锚定那份不同，图上会出现两张"张卫国"，必须让正兵
    看得出原因。
    """
    try:
        store = ctx.factory.for_case(case_id, mode="read")
        return getattr(store, "read_conn", None)
    except Exception:
        logger.warning("案件只读连接获取失败 case=%s", case_id, exc_info=True)
        return None


@router.get("/cases/{case_id}/case-canvas")
def get_case_canvas(case_id: str,
                    pack: str = "default",
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """取案件级研判画布：惰性建空文档 → 读 → 并入镜头重建层。

    镜头层每次都重建，所以**重扫后新跑的镜头会自己出现在图上**，不需要
    前端记住跑过什么、也不需要清库。
    """
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    case_dir = ctx.factory.case_dir(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    state = _state(ctx, case_id)

    row = state.get_case_canvas(case_id)
    if row is None:
        row = state.insert_case_canvas(
            case_id=case_id, doc={"nodes": [], "edges": []},
            created_by=p.operator, created_at=_now()) or state.get_case_canvas(
            case_id)
    if row is None:                      # 并发：他方已建，回读即可
        row = state.get_case_canvas(case_id)

    persistent = row.get("doc") if isinstance(row.get("doc"), dict) else {}
    persistent_meta = persistent.get("meta") \
        if isinstance(persistent.get("meta"), dict) else {}
    # §2/§4：从已有画布提取靶心名字，用于跳过共现人物中的靶心本人
    target_labels: dict[str, str] = {}
    for n in (persistent.get("nodes") or []):
        if isinstance(n, dict) and n.get("kind") == "subject":
            nid = str(n.get("id") or "")
            lbl = str(n.get("label") or "")
            if nid and lbl:
                target_labels[nid] = lbl
    # 渐进式生成：只重建正兵已揭示的组（清单仍全量，供面板逐组揭示）
    revealed = canvas_case_doc.parse_revealed_groups(persistent_meta)
    on_canvas_ids = {str(n.get("id") or "")
                     for n in (persistent.get("nodes") or [])
                     if isinstance(n, dict) and str(n.get("id") or "")}
    try:
        growth = canvas_growth.load_growth_layer(
            case_dir=case_dir, case_id=case_id, target_node_id=None,
            pack=pack, base_dir=base_dir, target_labels=target_labels,
            revealed=revealed, on_canvas_ids=on_canvas_ids,
            conn=_case_read_conn(ctx, case_id))
    except Exception:
        # 档案读失败不拖垮画布：如实标注，人工层照常返回（比整页报错有用）
        logger.warning("镜头重建层读取失败 case=%s", case_id, exc_info=True)
        growth = {"nodes": [], "edges": [], "hypothesis_nodes": [],
                  "infer_edges": [],
                  "meta": {"groups": [], "reason": "镜头重建层读取失败",
                           "node_id_scope": "*"}}

    # 物品层与镜头层同属重建层：每次从语义层重建，不落库（存了会僵死）。
    # 读取失败不拖垮画布——与镜头层同款降级，如实标注原因。
    try:
        item_layer = canvas_item_source.build_item_layer(
            case_id=case_id, conn=_case_read_conn(ctx, case_id))
    except Exception:
        logger.warning("物品层读取失败 case=%s", case_id, exc_info=True)
        item_layer = {"nodes": [], "edges": [],
                      "meta": {"items": 0, "holds": 0,
                               "reason": "物品层读取失败", "source": "item_source"}}

    merge_stats: dict[str, int] = {}
    doc = canvas_case_doc.merge_lens_layer(persistent, growth,
                                           stats_sink=merge_stats)
    doc = canvas_case_doc.merge_lens_layer(doc, item_layer,
                                           stats_sink=merge_stats)
    return ok({
        "canvas_id": row.get("canvas_id") or case_id,
        "case_id": case_id,
        "doc": doc,
        "version": int(row.get("version") or 1),
        "created_by": row.get("created_by", ""),
        "created_at": row.get("created_at", ""),
        "updated_by": row.get("updated_by", ""),
        "updated_at": row.get("updated_at", ""),
        # 重建层元信息单独下发：正兵能看出"本次重建了哪些组、被排除了什么"
        "lens_layer": {
            "groups": (growth.get("meta") or {}).get("groups") or [],
            "excluded": growth.get("excluded") or [],
            "missing_hypotheses": growth.get("missing_hypotheses") or [],
            "reason": (growth.get("meta") or {}).get("reason") or "",
            "revealed_count": len(revealed),
            "edges_dropped": merge_stats.get("edges_dropped", 0),
        },
        "item_layer": {
            "items": (item_layer.get("meta") or {}).get("items", 0),
            "holds": (item_layer.get("meta") or {}).get("holds", 0),
            "skipped": (item_layer.get("meta") or {}).get("skipped") or [],
            "reason": (item_layer.get("meta") or {}).get("reason") or "",
        },
    })


@router.patch("/cases/{case_id}/case-canvas")
def patch_case_canvas(case_id: str, body: CaseCanvasPatch,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """自动保存：整文档入，剥掉镜头层后落库，version+1。

    剥离镜头层是硬要求——存进去就会僵死（档案变了库里不变、且永不覆盖）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = _state(ctx, case_id)

    persistent, stripped = canvas_case_doc.split_persistent(body.doc)
    # 揭示集是唯一由前端维护的 meta：落库前规范化，剔脏项、保稳定序
    pmeta = persistent.get("meta")
    if isinstance(pmeta, dict) \
            and canvas_case_doc.META_REVEALED in pmeta:
        pmeta[canvas_case_doc.META_REVEALED] = \
            canvas_case_doc.serialize_revealed_groups(
                canvas_case_doc.parse_revealed_groups(pmeta))
    try:
        row = state.update_case_canvas_doc(
            case_id, persistent, operator=p.operator, updated_at=_now(),
            expected_version=body.expected_version)
    except CanvasNotFound:
        row = state.insert_case_canvas(
            case_id=case_id, doc=persistent, created_by=p.operator,
            created_at=_now()) or state.get_case_canvas(case_id)
        logger.warning("案件级画布不存在即建 case=%s", case_id)
    except CanvasVersionConflict as e:
        # 必须用既有码 CONFLICT（ERR_CONFLICT）：前端 client.ts 的错误码白名单
        # 只认 7 个，换成 CANVAS_VERSION_CONFLICT 会被 codeOf() 归成 INTERNAL，
        # 于是 409 重试分支永不命中——正兵改坐标遇到他人已更新时，看到的不是
        # "已按最新版本合并"而是"请求失败"，且自动保存持续报错。
        raise APIError(
            ERR_CONFLICT,
            f"画布已被他人更新（你的基准 v{e.expected}，"
            f"服务端 v{e.current}），请确认后覆盖重试", 409)

    _audit(state, case_id=case_id, version=version, operator=p.operator,
           action="case_canvas.autosave")
    return ok({
        "canvas_id": row.get("canvas_id") or case_id,
        "case_id": case_id,
        "version": int(row.get("version") or 1),
        "updated_at": row.get("updated_at", ""),
        # 剥离统计如实回传：跑过镜头却剥离 0 个，说明标记没打上（缺陷信号）
        "stripped": stripped,
    })


class CaseCanvasToVerifyIn(BaseModel):
    """案件级 hypothesis → 待核查：文本（默认带入假设内容）+ 期望版本。"""
    text: str = ""
    expected_version: int | None = None


@router.post(
    "/cases/{case_id}/case-canvas/nodes/{node_id}/to-verify",
    status_code=202)
def case_canvas_to_verify(case_id: str, node_id: str,
                          body: CaseCanvasToVerifyIn,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """案件级画布 hypothesis 节点 → 案件级核查项（P2）。

    与线索级 ``/clues/{clue_id}/canvas/nodes/{node_id}/to-verify`` 对应：
    线索级走 TASK_VERIFY（强依赖 clue_id），案件级画布节点没有 clue_id，
    走独立写通道：直接落 case_verify_item 表 + 202。

    校验：节点存在 + kind=hypothesis + 非 system；text 非空。
    """
    _get_owned_case(case_id, p, ctx.cases)
    # 与线索级 _reject_agent_canvas 同口径：agent 身份不得触发案件级核查写
    if p.operator.startswith("agent:"):
        raise APIError(
            ERR_VALIDATION,
            f"Agent 身份 {p.operator!r} 不得将假设转为待核实"
            "（核查写操作须由具名人工触发）", 403)
    state = _state(ctx, case_id)
    try:
        row = state.get_case_canvas(case_id)
        if row is None:
            raise APIError(ERR_NOT_FOUND,
                           "案件画布不存在，请先打开画布", 404)
        doc = row.get("doc") if isinstance(row.get("doc"), dict) else {}
        nodes = doc.get("nodes") or []
        node = next((n for n in nodes if isinstance(n, dict)
                     and n.get("id") == node_id), None)
        if node is None:
            raise APIError(ERR_NOT_FOUND,
                           f"画布节点不存在：{node_id}", 404)
        if node.get("kind") != "hypothesis":
            raise APIError(ERR_VALIDATION,
                           "仅假设节点可转为待核实（当前节点类型："
                           f"{node.get('kind')}）", 400)
        if bool(node.get("system")):
            raise APIError(ERR_VALIDATION,
                           "系统节点不可转为待核实（系统假设由镜头产出，"
                           "应通过线索级核查通道）", 400)
        text = (body.text or "").strip()
        if not text:
            # 默认带入假设标题/内容，避免正兵多填一次
            props = node.get("props") if isinstance(
                node.get("props"), dict) else {}
            text = str(props.get("title") or node.get("label")
                       or props.get("content") or "").strip()
        if not text:
            raise APIError(ERR_VALIDATION,
                           "核查项文本不能为空（text 或节点标题）", 400)
        item = state.add_case_verify_item(
            case_id=case_id, text=text, kind="manual",
            node_id=node_id, operator=p.operator, updated_at=_now())
        _audit(state, case_id=case_id, version=row.get("version"),
               operator=p.operator,
               action=f"case_canvas.to_verify:{node_id}")
        return ok({
            "item": item,
            "node_id": node_id,
            "effective_text": text,
            "mode": "case_manual",
        }, data_version=ctx.repo.current_version(case_id))
    finally:
        state.close()


@router.get("/cases/{case_id}/case-verify-items")
def list_case_verify_items(case_id: str,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """案件级核查项列表（P2）。"""
    _get_owned_case(case_id, p, ctx.cases)
    state = _state(ctx, case_id)
    try:
        items = state.list_case_verify_items(case_id)
    finally:
        state.close()
    return ok({"items": items, "total": len(items)})


class CaseVerifyTransitionIn(BaseModel):
    status: str
    conclusion: str = ""


@router.patch(
    "/cases/{case_id}/case-verify-items/{item_id}")
def transition_case_verify_item(case_id: str, item_id: str,
                               body: CaseVerifyTransitionIn,
                               p: Principal = Depends(get_principal),
                               ctx: WebContext = Depends(get_ctx)):
    """案件级核查项状态转移（待核查→核查中→已证实/已查否/无法核实）。

    与线索级 transition_verify_item 同口径，合法转移校验在路由层简化
    （status 非空即可），不引入 state machine 复杂度——案件级核查项
    是人工写通道，由正兵负责状态合法性。
    """
    _get_owned_case(case_id, p, ctx.cases)
    status = (body.status or "").strip()
    if not status:
        raise APIError(ERR_VALIDATION, "status 不能为空", 400)
    state = _state(ctx, case_id)
    try:
        item = state.transition_case_verify_item(
            item_id, status=status, conclusion=body.conclusion or "",
            operator=p.operator, updated_at=_now())
        if item is None:
            raise APIError(ERR_NOT_FOUND,
                           f"案件级核查项不存在：{item_id}", 404)
        _audit(state, case_id=case_id, version=None,
               operator=p.operator,
               action=f"case_verify.transition:{item_id}")
        return ok({"item": item})
    finally:
        state.close()


class CaseCanvasItemIn(BaseModel):
    title: str = ""
    item_type: str = ""
    identifiers: list[dict] = []
    descriptors: dict = {}
    lat: float | None = None
    lng: float | None = None
    precision: str | None = None
    holder_raw: str | None = None
    acquire_date: str | None = None
    dispose_date: str | None = None


@router.post("/cases/{case_id}/case-canvas/items")
def build_case_canvas_item(case_id: str, body: CaseCanvasItemIn,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """界面登记的物品 → 规范节点；**只构造、不落库**。

    必须由服务端构造的理由：节点 id 含 primary_digest（摘要），前端若自己
    算一遍，摘要规则就成了两份，而两份迟早分叉——分叉的症状不是报错，
    是同一辆车登记两次变成两个节点，或两件不同物品被合成一个
    （车牌 "A123" 与序列号 "A123" 的摘要完全相同，只差 item_type 前缀）。

    只构造不落库：落库统一走 PATCH，避免"登记即存、撤销留痕"两套写路径。
    """
    _get_owned_case(case_id, p, ctx.cases)

    ids: list[dict] = []
    for raw in body.identifiers or []:
        if not isinstance(raw, dict):
            continue
        kind = normalize_identifier_kind(raw.get("kind"))
        if kind is None:
            # 识别不了绝不猜：猜成 none 会把有凭证的车记成无凭证赃物（D4），
            # 从此永不参与消歧——实体永久丢失，且过程中没有任何报错。
            raise APIError(
                ERR_VALIDATION,
                f"无法识别的标识符种类：{raw.get('kind')!r}。"
                f"请从下拉中选择（如「车牌号」「权证号」「发票代码+号码」）", 400)
        value = raw.get("value")
        if value in (None, ""):
            continue
        ids.append({"kind": kind, "value": str(value),
                    "issuer": str(raw.get("issuer") or ""),
                    "verified": raw.get("verified")})

    desc = {str(k): v for k, v in (body.descriptors or {}).items()
            if str(k) and v not in (None, "")}

    try:
        node = build_item_node(
            case_id=case_id, title=body.title, item_type=body.item_type,
            identifiers=ids, descriptors=desc,
            lat=body.lat, lng=body.lng, precision=body.precision,
            holder_raw=body.holder_raw,
            acquire_date=body.acquire_date, dispose_date=body.dispose_date)
    except ValueError as e:
        # build_item_node 抛的 ValueError 全属"正兵可修正"的输入问题
        # （未知类型 / 未知种类 / 既无凭证也无特征），必须 400 + 说清原因。
        # 归成 500 会让正兵以为系统坏了，而实际只是他没填凭证。
        raise APIError(ERR_VALIDATION, str(e), 400)

    props = node.get("props") or {}
    return ok({
        "node": node,
        "credentialed": bool(props.get("credentialed")),
        "unidentified": bool(props.get("unidentified")),
        "mappable": bool(props.get("mappable")),
        "sensitive_kinds": props.get("sensitive_kinds") or [],
    })
