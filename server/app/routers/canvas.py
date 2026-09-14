"""
server/app/routers/canvas.py
线索研判画布端点（PRD 线索研判画布 V1.0.0）。

M1 地基（RC-101/205/207）：
  GET  /cases/{cid}/clues/{clue_id}/canvas   惰性 seed + 读取
  PATCH .../canvas                            防抖自动保存（整文档+版本基准）
M2 溯源（RC-102/103/104）：
  POST .../canvas/expand                      逐层溯源/邻居懒加载
  GET  /cases/{cid}/canvas/rules/{rid}/audit  规则审计视图
M3 编辑（RC-202/203/206）：
  POST   .../canvas/nodes                     人工节点（假设/备注）新增
  PATCH  .../canvas/nodes/{id}                人工节点编辑
  DELETE .../canvas/nodes/{id}                人工节点删除（级联人工边）
  POST   .../canvas/edges                     人工连线（合法性矩阵）
  DELETE .../canvas/edges/{id}                人工连线删除（系统边锁定）
  GET/POST .../canvas/snapshots               快照列表/创建（立即、非防抖）
  POST   .../canvas/snapshots/{id}/rollback   回滚（先存恢复点，主表新版本）
M5 智能（RC-301/302/303）：
  POST   .../canvas/chat                   只读问答（30s）
  POST   .../canvas/chat/suggestion        问答建议转提案（不直接落画布）
M6 报告（RC-304/305/306）：
  POST   .../canvas/reports                生成报告（先快照→202 异步任务）
  GET    .../canvas/reports                版本列表（倒序）
  GET    .../canvas/reports/{rid}          报告详情（sections + citations）
  GET    .../canvas/reports/{rid}/export.md   Markdown 下载
  GET    .../canvas/reports/{rid}/export.docx Word 下载

纪律：
  - seed 是纯函数（server/app/canvas_seed.py），本层只组装 assemble_detail
    产物并落 state.sqlite；不直读 Parquet、不生成自由 SQL；
  - 重复 GET 幂等（INSERT OR IGNORE，第二次回读已存文档，不重新 seed）；
  - PATCH 仅允许系统节点坐标/钉住变动（M1），系统节点 ref/kind/label/props
    篡改与边集合变动一律 400；人工结构变更走 M3 专用端点；版本基准过期
    409（后写覆盖由前端确认重试）；
  - 人工编辑业务校验在 server/app/canvas_edit.py（纯函数）：系统节点/边
    锁定、4 类人工关系矩阵穷举、字段长度；
  - 快照在应用层不可变（无更新/删除通道）；回滚只还原画布表达层并标记
    失效引用，不回滚业务事实；
  - 间类可见性与 clue_detail 同口径 fail-closed（越权统一 404）；
  - 创建/自动保存写审计链（RC-401，version 缺失时 anchor missing 兜底）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime
from urllib.parse import quote as _url_quote

import duckdb
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from core.access import can_see_jian_types
from core.audit import AuditChain
from core.functions import FunctionExecutor
from core.ontology_loader import load_pack
from core.proposal import ProposalStore, ProposalValidationError
from core.verify_machine import PENDING

from server.app import (
    canvas_chat,
    canvas_edit,
    canvas_expand,
    canvas_infer,
    canvas_report,
    canvas_seed,
    clues_view,
    ontology_meta,
)
from server.app.deps import (
    WebContext,
    access_for,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import (
    ERR_CONFLICT,
    ERR_FORBIDDEN,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.evidence_builder import _make_source_ref
from server.app.routers.cases import _get_owned_case
from server.app.routers.clues import _aggregate_cross_rows
from server.app.security import Principal
from server.app.store.state_store import (
    CanvasVersionConflict,
    StateStore,
)
from server.app.verify_provision import render_suggested
from server.app.worker.tasks import TASK_REPORT, TASK_VERIFY, enqueue_task

router = APIRouter(tags=["canvas"])
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _payload(row: dict, *, seeded: bool, semantic_ready: bool) -> dict:
    return {
        "canvas_id": row["canvas_id"],
        "clue_id": row["clue_id"],
        "doc": row["doc"],
        "version": int(row["version"]),
        "created_by": row.get("created_by", ""),
        "created_at": row.get("created_at", ""),
        "updated_by": row.get("updated_by", ""),
        "updated_at": row.get("updated_at", ""),
        "seeded": seeded,
        "semantic_ready": semantic_ready,
    }


def _semantic_ready(ctx: WebContext, case_id: str, version: int | None) -> bool:
    """语义层（obj_* 表）是否已构建：决定画布「实体关联暂不可用」横幅。

    任何失败（无版本/版本文件缺失/查询异常）优雅降级 False，读面不炸。
    M1 seed 本身不依赖语义层（事实/数据行仍生成）。
    """
    if not version:
        return False
    try:
        with ctx.factory.for_case(case_id, mode="read",
                                  version=version) as ro:
            conn = getattr(ro, "read_conn", None) or ro
            n = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name LIKE 'obj_%'").fetchone()[0]
            return int(n) > 0
    except Exception:
        logger.warning("canvas semantic_ready 探测失败 case=%s v=%s",
                       case_id, version, exc_info=True)
        return False


def _audit(state: StateStore, *, case_id: str, version: int | None,
           operator: str, action: str, before: dict | None,
           after: dict | None) -> None:
    """画布写审计（RC-401）：state.sqlite audit_chain，签名链与 Worker 同构。"""
    ver = f"v{version}" if version else "unknown"
    chain = AuditChain(state.conn, case_id=case_id, backend="sqlite",
                       ontology_version=ver)
    chain.append(
        operator=operator, before=before, after=after,
        source_row_ids=[], ontology_version=ver)


@router.get("/cases/{case_id}/clues/{clue_id}/canvas")
def get_canvas(case_id: str, clue_id: str,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """取画布：已存在直接返回（含坐标）；不存在则惰性 seed 落库后返回。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    ready = _semantic_ready(ctx, case_id, version)

    # 画布 seed 必须落库：直接以写模式打开 state.sqlite（与其他纯读面不同，
    # StateStore 构造自带 CREATE TABLE IF NOT EXISTS，不影响既有读路径）。
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        existing = state.get_canvas(clue_id)
        if existing is not None:
            return ok(_payload(existing, seeded=False, semantic_ready=ready),
                      data_version=version)

        # ---- 惰性 seed：复用 assemble_detail（三栏证据/遮蔽/核查项供给
        # 与线索详情完全同源；方案 B 聚合行回填同 clue_detail 口径）----
        cross_rows = _aggregate_cross_rows(
            ctx, case_id, case.pack_id, version, base_dir)
        access = access_for(p, case_id=case_id, purpose="线索研判画布")
        detail = clues_view.assemble_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            clue_id=clue_id, state_map=state.status_map(), decisions=[],
            access=access, pack_id=case.pack_id, base_dir=base_dir,
            state_store=state, cross_rows=cross_rows,
            # M4 RC-105：画布打开不供给手册建议项（未采纳建议仅画布虚节点存在）
            provision_suggested=False)
        if detail is None:
            raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
        if not can_see_jian_types(
                detail.get("jian_types") or [], role=p.role,
                jian_clearances=ontology_meta.jian_clearances(
                    case.pack_id, base_dir)):
            raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)

        doc = canvas_seed.seed_canvas(
            clue_id=clue_id, detail=detail,
            evidence=detail.get("evidence") or [],
            verify_items=state.list_verify_items(clue_id),
            materials=state.list_evidence(clue_id),
            pack_id=case.pack_id, base_dir=base_dir)
        ts = _now()
        row = state.insert_canvas(
            clue_id=clue_id, doc=doc, created_by=p.operator, created_at=ts)
        if row is None:
            # 并发首次 GET：他人已 seed，回读（幂等不覆盖）
            row = state.get_canvas(clue_id)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.create",
               before=None,
               after={"action": "canvas.create", "clue_id": clue_id,
                      "nodes": len(doc["nodes"]), "edges": len(doc["edges"])})
        return ok(_payload(row, seeded=True, semantic_ready=ready),
                  data_version=version)
    finally:
        state.close()


class CanvasPatchIn(BaseModel):
    doc: dict
    version: int | None = None


@router.patch("/cases/{case_id}/clues/{clue_id}/canvas")
def patch_canvas(case_id: str, clue_id: str, body: CanvasPatchIn,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """自动保存（RC-205）：整文档更新 + version 基准；M1 仅坐标/钉住白名单。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = state.get_canvas(clue_id)
        if current is None:
            raise APIError(
                ERR_NOT_FOUND,
                f"画布尚未初始化：{clue_id}（请先 GET 触发惰性创建）", 404)
        errs = canvas_seed.validate_patch(current["doc"], body.doc)
        if errs:
            raise APIError(
                ERR_VALIDATION,
                "画布保存被拒：" + "；".join(errs[:5]), 400)
        try:
            row = state.update_canvas_doc(
                clue_id, body.doc, operator=p.operator, updated_at=_now(),
                expected_version=body.version)
        except CanvasVersionConflict as exc:
            raise APIError(
                ERR_CONFLICT,
                f"画布已被他人更新（你的基准 v{exc.expected}，"
                f"服务端 v{exc.current}），请确认后覆盖重试", 409)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.autosave",
               before={"version": current["version"]},
               after={"action": "canvas.autosave", "clue_id": clue_id,
                      "base_version": body.version,
                      "version": row["version"],
                      "nodes": len(row["doc"]["nodes"]),
                      "edges": len(row["doc"]["edges"])})
        return ok(_payload(row, seeded=False,
                           semantic_ready=_semantic_ready(ctx, case_id,
                                                          version)),
                  data_version=version)
    finally:
        state.close()


# ======================================================================
# RC-103/104：反向逐层溯源 expand（懒加载 + 服务端幂等合并落库）
# ======================================================================
class CanvasExpandIn(BaseModel):
    node_id: str
    # source=向来源方向（fact→object→row→file）；neighbors=对象语义邻居；all
    direction: str = "all"
    version: int | None = None


def _assemble_clue_detail(ctx: WebContext, *, case, state: StateStore,
                          version: int | None, base_dir, p: Principal,
                          clue_id: str, purpose: str,
                          provision_suggested: bool = False):
    """assemble_detail 同源装配（expand 行字典 + 间类 fail-closed 复核）。

    M4 起画布路径默认不供给手册建议项（provision_suggested=False，
    RC-105 AC2：未采纳建议仅画布虚节点存在，不进核查工作台）。
    """
    cross_rows = _aggregate_cross_rows(
        ctx, case.id, case.pack_id, version, base_dir)
    access = access_for(p, case_id=case.id, purpose=purpose)
    detail = clues_view.assemble_detail(
        case_dir=ctx.factory.case_dir(case.id), version=version,
        clue_id=clue_id, state_map=state.status_map(), decisions=[],
        access=access, pack_id=case.pack_id, base_dir=base_dir,
        state_store=state, cross_rows=cross_rows,
        provision_suggested=provision_suggested)
    if detail is None:
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    if not can_see_jian_types(
            detail.get("jian_types") or [], role=p.role,
            jian_clearances=ontology_meta.jian_clearances(
                case.pack_id, base_dir)):
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
    return detail, access


def _clue_row_refs(detail: dict, clue_id: str, *,
                   pack_id: str = "default", base_dir=None) -> dict[str, dict]:
    """本线索 source_row 节点 ref(URI) → 原始视图行 dict（种子同口径）。"""
    out: dict[str, dict] = {}
    view_rows = detail.get("source_rows")
    if not isinstance(view_rows, list):
        return out
    for idx, sr in enumerate(view_rows):
        if not isinstance(sr, dict):
            continue
        ref = _make_source_ref(sr, idx, clue_id, pack_id=pack_id,
                               base_dir=base_dir)
        out[ref["row_uri"]] = sr
    return out


def _registered_sources(ctx: WebContext, case_id: str) -> dict[str, dict]:
    """ingest 登记 dataset 名 → case_sources 行（RC-103 文件元数据一致性）。"""
    out: dict[str, dict] = {}
    for s in ctx.repo.list_sources(case_id):
        if s.get("status") != "imported":
            continue
        ds = s.get("table_name")
        if not ds:
            try:
                mapping = json.loads(s.get("mapping_json") or "{}")
                ds = mapping.get("target_table")
            except (TypeError, ValueError):
                ds = None
        if ds:
            out[str(ds)] = s
    return out


def _current_build(conn, pack_id: str) -> str | None:
    """当前语义 build_id（meta_ontology_state 版本时钟）；任何缺失优雅 None。"""
    try:
        r = conn.execute(
            "SELECT build_id FROM meta_ontology_state "
            "WHERE pack=? AND is_current=true", [pack_id]).fetchone()
        if r:
            return r[0]
        r = conn.execute(
            "SELECT build_id FROM meta_ontology_state "
            "WHERE is_current=true LIMIT 1").fetchone()
        return r[0] if r else None
    except Exception:
        return None


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/expand")
def expand_canvas(case_id: str, clue_id: str, body: CanvasExpandIn,
                  p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    """展开节点下一层（RC-103）：返回新增节点/边 + 抽屉字段负载（RC-104 遮蔽）。

    - 服务端按 id/eid 幂等合并后仅系统通道落库（不经 M1 validate_patch
      节点集冻结；人工 PATCH 通道规则不变）；
    - 重复展开同层无新增 → 不产生新版本；
    - 版本基准过期 → 409（前端刷新后重试）；
    - 语义层缺失/归档缺行 → notices 业务码，不 500、不破坏已渲染层。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    if body.direction not in canvas_expand.VALID_DIRECTIONS:
        raise APIError(ERR_VALIDATION,
                       f"非法 direction：{body.direction}", 400)

    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    # 只读语义库必须在整个 expand 期间保持打开（with 块退出即 close，
    # 会让 expand_layer 拿到已关闭连接而静默降级）；finally 统一释放。
    ro_store = None
    conn = None
    build_id = None
    try:
        current = state.get_canvas(clue_id)
        if current is None:
            raise APIError(
                ERR_NOT_FOUND,
                f"画布尚未初始化：{clue_id}（请先 GET 触发惰性创建）", 404)
        base_version = (body.version if body.version is not None
                        else int(current["version"]))
        if int(base_version) != int(current["version"]):
            raise APIError(
                ERR_CONFLICT,
                f"画布已被他人更新（你的基准 v{base_version}，"
                f"服务端 v{current['version']}），请刷新后重试", 409)

        node = next((n for n in current["doc"].get("nodes", [])
                     if n.get("id") == body.node_id), None)
        if node is None:
            raise APIError(ERR_NOT_FOUND,
                           f"画布节点不存在：{body.node_id}", 404)
        if node.get("kind") not in canvas_expand.EXPANDABLE_KINDS:
            raise APIError(ERR_VALIDATION,
                           f"该节点类型不支持溯源展开：{node.get('kind')}",
                           400)

        detail, access = _assemble_clue_detail(
            ctx, case=case, state=state, version=version, base_dir=base_dir,
            p=p, clue_id=clue_id, purpose="线索画布逐层溯源")
        clue_rows = _clue_row_refs(detail, clue_id, pack_id=case.pack_id,
                                   base_dir=base_dir)
        sources = _registered_sources(ctx, case_id)
        spec = load_pack(case.pack_id, base_dir)
        objects, links = canvas_expand.build_index(spec)

        # 缺失/未 BUILD 时 conn=None 降级（行→文件层仍可用）
        try:
            ro_store = ctx.factory.for_case(case_id, mode="read",
                                            version=version)
            conn = getattr(ro_store, "read_conn", None) or ro_store
            build_id = _current_build(conn, case.pack_id)
        except (FileNotFoundError, OSError):
            logger.info("canvas expand 无语义库，降级只读线索行 case=%s v=%s",
                        case_id, version)
            if ro_store is not None:
                ro_store.close()
            ro_store, conn = None, None

        result = canvas_expand.expand_layer(
            conn=conn, objects=objects, links=links,
            doc=current["doc"], node_id=body.node_id,
            clue_rows=clue_rows, registered_sources=sources,
            access=access, pack_id=case.pack_id, base_dir=base_dir,
            build_id=build_id, direction=body.direction)

        added_nodes, added_edges = [], []
        row = current
        if result["nodes"] or result["edges"]:
            merged = copy.deepcopy(current["doc"])
            added_nodes, added_edges = canvas_expand.merge_expansion(
                merged, result["nodes"], result["edges"])
            shape_errs = canvas_seed.validate_doc_shape(merged)
            if shape_errs:
                # 纯防御：合并产物形状非法不落库（expand 是系统通道，
                # 不应触发；触发即服务 bug，留日志）
                logger.error("canvas expand 合并产物形状非法：%s", shape_errs)
                raise APIError(ERR_VALIDATION,
                               "溯源结果合并失败（文档形状非法）", 500)
            if added_nodes or added_edges:
                try:
                    row = state.update_canvas_doc(
                        clue_id, merged, operator=p.operator,
                        updated_at=_now(), expected_version=base_version)
                except CanvasVersionConflict as exc:
                    raise APIError(
                        ERR_CONFLICT,
                        f"画布已被他人更新（你的基准 v{exc.expected}，"
                        f"服务端 v{exc.current}），请刷新后重试", 409)
                _audit(state, case_id=case_id, version=version,
                       operator=p.operator, action="canvas.expand",
                       before={"version": current["version"]},
                       after={"action": "canvas.expand",
                              "clue_id": clue_id,
                              "node_id": body.node_id,
                              "direction": body.direction,
                              "version": row["version"],
                              "nodes": len(added_nodes),
                              "edges": len(added_edges)})

        payload = _payload(row, seeded=False,
                           semantic_ready=_semantic_ready(ctx, case_id,
                                                          version))
        payload.update({
            "added_nodes": added_nodes,
            "added_edges": added_edges,
            "details": result["details"],
            "notices": result["notices"],
            "truncated": result["truncated"],
            "leaf": result["leaf"],
        })
        return ok(payload, data_version=version)
    finally:
        state.close()
        if ro_store is not None:
            ro_store.close()


# ======================================================================
# RC-102：规则审计视图（只读、按需加载；业务视图不出现技术标识）
# ======================================================================
@router.get("/cases/{case_id}/canvas/rules/{rule_id}/audit")
def rule_audit(case_id: str, rule_id: str,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """规则节点审计视图数据：function/params/命中条件/版本 + 规则工坊外链。

    只读 rules 声明（规则手册第六段），不含任何编辑控件（RC-102-2/3）。
    历史占位规则（无声明）→ 404，前端展示"规则声明缺失"态。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    spec = load_pack(case.pack_id, base_dir)
    rs = spec.rules.get(rule_id) if hasattr(spec, "rules") else None
    if rs is None:
        raise APIError(ERR_NOT_FOUND,
                       f"规则声明缺失或已被移除：{rule_id}", 404)

    ontology_version = ""
    try:
        with ctx.factory.for_case(case_id, mode="read",
                                  version=version) as ro:
            conn = getattr(ro, "read_conn", None) or ro
            r = conn.execute(
                "SELECT ontology_version FROM meta_ontology_state "
                "WHERE pack=? AND is_current=true",
                [case.pack_id]).fetchone()
            if r:
                ontology_version = r[0] or ""
    except Exception:
        ontology_version = ""

    return ok({
        "rule_id": rs.id,
        "title": rs.title,
        "stage": rs.stage,
        "dimension": rs.dimension,
        "function": rs.function,
        "params": dict(rs.params or {}),
        "hit_when": rs.hit_when,
        "jian_types": list(getattr(rs, "jian_types", ()) or ()),
        "assumption": getattr(rs, "assumption", "") or "",
        "rule_text": rs.rule_text,
        "basis_text": getattr(rs, "basis_text", "") or "",
        "ontology_version": ontology_version,
        "pack_id": case.pack_id,
        "rule_workshop_href": "/c/rules",
    }, data_version=version)


# ======================================================================
# M3（RC-202/203/206）：人工节点/边 CRUD + 快照/回滚
# 结构变更走专用端点（立即生效、逐动作审计）；布局坐标仍走 PATCH 防抖通道。
# ======================================================================
def _load_for_edit(state: StateStore, clue_id: str,
                   base_version: int | None) -> dict:
    """取画布并做写入前版本基准校验（404/409 前置，失败不改文档）。"""
    current = state.get_canvas(clue_id)
    if current is None:
        raise APIError(
            ERR_NOT_FOUND,
            f"画布尚未初始化：{clue_id}（请先 GET 触发惰性创建）", 404)
    if base_version is not None \
            and int(base_version) != int(current["version"]):
        raise APIError(
            ERR_CONFLICT,
            f"画布已被他人更新（你的基准 v{base_version}，"
            f"服务端 v{current['version']}），请刷新后重试", 409)
    return current


def _commit_edit(state: StateStore, clue_id: str, doc: dict, *,
                 operator: str, base_version: int | None) -> dict:
    """结构变更落库：形状防御校验 + 版本戳自增（冲突 409）。"""
    shape_errs = canvas_seed.validate_doc_shape(doc)
    if shape_errs:
        logger.error("canvas 人工编辑产物形状非法：%s", shape_errs)
        raise APIError(ERR_VALIDATION, "画布文档结构非法，改动未落库", 400)
    try:
        return state.update_canvas_doc(
            clue_id, doc, operator=operator, updated_at=_now(),
            expected_version=base_version)
    except CanvasVersionConflict as exc:
        raise APIError(
            ERR_CONFLICT,
            f"画布已被他人更新（你的基准 v{exc.expected}，"
            f"服务端 v{exc.current}），请刷新后重试", 409)


def _edit_payload(row: dict, **extra) -> dict:
    payload = _payload(row, seeded=False, semantic_ready=False)
    payload.update(extra)
    return payload


class ManualNodeIn(BaseModel):
    kind: str
    props: dict
    x: float = 0.0
    y: float = 0.0
    version: int | None = None


class ManualNodePatchIn(BaseModel):
    props: dict
    version: int | None = None


class ManualEdgeIn(BaseModel):
    source: str
    target: str
    rel: str
    note: str | None = None
    version: int | None = None


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/nodes")
def create_canvas_node(case_id: str, clue_id: str, body: ManualNodeIn,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """人工节点新增（RC-202）：仅 hypothesis/note；服务端签发 cn_ id。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        try:
            props = canvas_edit.validate_node_props(body.kind, body.props)
            node_id = canvas_edit.manual_node_id()
            doc = copy.deepcopy(current["doc"])
            node = canvas_edit.add_manual_node(
                doc, kind=body.kind, props=props, node_id=node_id,
                x=body.x, y=body.y, operator=p.operator, now=_now())
        except canvas_edit.CanvasEditError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        row = _commit_edit(state, clue_id, doc, operator=p.operator,
                           base_version=body.version)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.node_create",
               before={"version": current["version"]},
               after={"action": "canvas.node_create", "clue_id": clue_id,
                      "node_id": node["id"], "kind": node["kind"],
                      "label": node["label"],
                      "version": row["version"]})
        return ok(_edit_payload(row, node=node), data_version=version)
    finally:
        state.close()


@router.patch("/cases/{case_id}/clues/{clue_id}/canvas/nodes/{node_id}")
def update_canvas_node(case_id: str, clue_id: str, node_id: str,
                       body: ManualNodePatchIn,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """人工节点编辑（RC-202）：标题/内容；系统节点 400。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        try:
            doc = copy.deepcopy(current["doc"])
            node = canvas_edit.update_manual_node(
                doc, node_id=node_id, props=body.props, now=_now())
        except canvas_edit.CanvasEditError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        row = _commit_edit(state, clue_id, doc, operator=p.operator,
                           base_version=body.version)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.node_update",
               before={"version": current["version"]},
               after={"action": "canvas.node_update", "clue_id": clue_id,
                      "node_id": node_id, "kind": node["kind"],
                      "version": row["version"]})
        return ok(_edit_payload(row, node=node), data_version=version)
    finally:
        state.close()


@router.delete("/cases/{case_id}/clues/{clue_id}/canvas/nodes/{node_id}")
def delete_canvas_node(case_id: str, clue_id: str, node_id: str,
                       version: int | None = None,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """人工节点删除（RC-202）：级联删除其人工边，系统边一律保留。"""
    _get_owned_case(case_id, p, ctx.cases)
    case_version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, version)
        try:
            doc = copy.deepcopy(current["doc"])
            removed_edges = canvas_edit.delete_manual_node(
                doc, node_id=node_id)
        except canvas_edit.CanvasEditError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        row = _commit_edit(state, clue_id, doc, operator=p.operator,
                           base_version=version)
        _audit(state, case_id=case_id, version=case_version,
               operator=p.operator, action="canvas.node_delete",
               before={"version": current["version"]},
               after={"action": "canvas.node_delete", "clue_id": clue_id,
                      "node_id": node_id, "removed_edges": removed_edges,
                      "version": row["version"]})
        return ok(_edit_payload(row, removed_node=node_id,
                                removed_edges=removed_edges),
                  data_version=case_version)
    finally:
        state.close()


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/edges")
def create_canvas_edge(case_id: str, clue_id: str, body: ManualEdgeIn,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """人工连线（RC-203）：4 类关系矩阵 + 自连/重复边/禁入目标拒绝。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        # 业务校验（重复/非法矩阵）先于版本基准：陈旧基准提交重复边时先得
        # 400「已存在」，而不是被 409 抢先（前端按 400 抑制重复提交）
        current = _load_for_edit(state, clue_id, None)
        try:
            note = canvas_edit.validate_edge_note(body.note)
            doc = copy.deepcopy(current["doc"])
            edge = canvas_edit.add_manual_edge(
                doc, source=body.source, target=body.target,
                rel=body.rel, note=note, operator=p.operator, now=_now())
        except canvas_edit.CanvasEditError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        row = _commit_edit(state, clue_id, doc, operator=p.operator,
                           base_version=body.version)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.edge_create",
               before={"version": current["version"]},
               after={"action": "canvas.edge_create", "clue_id": clue_id,
                      "edge_id": edge["id"], "source": edge["source"],
                      "target": edge["target"], "rel": edge["rel"],
                      "has_note": bool(edge.get("note")),
                      "version": row["version"]})
        return ok(_edit_payload(row, edge=edge), data_version=version)
    finally:
        state.close()


@router.delete("/cases/{case_id}/clues/{clue_id}/canvas/edges/{edge_id}")
def delete_canvas_edge(case_id: str, clue_id: str, edge_id: str,
                       version: int | None = None,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """人工连线删除（RC-203）：系统边锁定返回 400。"""
    _get_owned_case(case_id, p, ctx.cases)
    case_version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, version)
        try:
            doc = copy.deepcopy(current["doc"])
            edge = canvas_edit.delete_manual_edge(doc, edge_id=edge_id)
        except canvas_edit.CanvasEditError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        row = _commit_edit(state, clue_id, doc, operator=p.operator,
                           base_version=version)
        _audit(state, case_id=case_id, version=case_version,
               operator=p.operator, action="canvas.edge_delete",
               before={"version": current["version"]},
               after={"action": "canvas.edge_delete", "clue_id": clue_id,
                      "edge_id": edge_id,
                      "version": row["version"]})
        return ok(_edit_payload(row, edge=edge), data_version=case_version)
    finally:
        state.close()


# ----------------------------------------------------------------------
# RC-206：快照与回滚
# ----------------------------------------------------------------------
def _snapshot_dto(s: dict) -> dict:
    doc = s.get("doc") or {"nodes": [], "edges": []}
    return {
        "snapshot_id": s["snapshot_id"],
        "clue_id": s["clue_id"],
        "label": s.get("label", ""),
        "origin": s.get("origin", "manual"),
        "report_id": s.get("report_id", ""),
        "created_by": s.get("created_by", ""),
        "created_at": s.get("created_at", ""),
        "node_count": len(doc.get("nodes", [])),
        "edge_count": len(doc.get("edges", [])),
        "doc": doc,
    }


class SnapshotIn(BaseModel):
    label: str
    version: int | None = None


class RollbackIn(BaseModel):
    version: int | None = None


@router.get("/cases/{case_id}/clues/{clue_id}/canvas/snapshots")
def list_canvas_snapshots(case_id: str, clue_id: str,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """快照列表（倒序）；列表项含 doc 供前端只读预览。"""
    _get_owned_case(case_id, p, ctx.cases)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        snaps = state.list_canvas_snapshots(clue_id)
        return ok({"snapshots": [_snapshot_dto(s) for s in snaps]})
    finally:
        state.close()


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/snapshots")
def create_canvas_snapshot(case_id: str, clue_id: str, body: SnapshotIn,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """手动快照（立即创建，非防抖）：备注必填 1..100；内容不可变。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        try:
            label = canvas_edit.validate_snapshot_label(body.label)
        except canvas_edit.CanvasEditError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        snap = state.insert_canvas_snapshot(
            clue_id=clue_id, snapshot_id=snapshot_id,
            doc=copy.deepcopy(current["doc"]), label=label, origin="manual",
            report_id="", created_by=p.operator, created_at=_now())
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.snapshot_create",
               before={"version": current["version"]},
               after={"action": "canvas.snapshot_create", "clue_id": clue_id,
                      "snapshot_id": snapshot_id,
                      "nodes": len(snap["doc"]["nodes"]),
                      "edges": len(snap["doc"]["edges"])})
        # 快照不推进画布 version
        payload = _payload(current, seeded=False, semantic_ready=False)
        return ok(payload | {"snapshot": _snapshot_dto(snap)},
                  data_version=version)
    finally:
        state.close()


@router.post(
    "/cases/{case_id}/clues/{clue_id}/canvas/snapshots/{snapshot_id}/rollback")
def rollback_canvas_snapshot(case_id: str, clue_id: str, snapshot_id: str,
                             body: RollbackIn,
                             p: Principal = Depends(get_principal),
                             ctx: WebContext = Depends(get_ctx)):
    """回滚（RC-206）：当前状态先存自动恢复点 → 主表整体替换为快照内容，
    生成新版本；不删除任何快照，可再滚回。失效业务引用置 stale 灰态。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        snap = state.get_canvas_snapshot(snapshot_id)
        if snap is None or snap.get("clue_id") != clue_id:
            raise APIError(ERR_NOT_FOUND,
                           f"快照不存在：{snapshot_id}", 404)

        # 1) 当前状态自动存恢复点（不可再被覆盖；保证 A→B→A 可往返）
        recovery_id = f"snap_{uuid.uuid4().hex[:16]}"
        recovery_label = f"回滚前自动恢复点（{snapshot_id}）"
        recovery = state.insert_canvas_snapshot(
            clue_id=clue_id, snapshot_id=recovery_id,
            doc=copy.deepcopy(current["doc"]), label=recovery_label,
            origin="manual", report_id="", created_by=p.operator,
            created_at=_now())

        # 2) 表达层整体替换；业务对象已消失的系统节点置 stale
        new_doc = copy.deepcopy(snap["doc"])
        live_items = {str(it.get("item_id") or "")
                      for it in state.list_verify_items(clue_id)}
        live_materials = {str(m.get("material_id") or "")
                          for m in state.list_evidence(clue_id)}
        stale_ids = canvas_edit.mark_stale_refs(
            new_doc, live_item_ids=live_items,
            live_material_ids=live_materials)
        row = _commit_edit(state, clue_id, new_doc, operator=p.operator,
                           base_version=body.version)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.snapshot_rollback",
               before={"version": current["version"],
                       "from_snapshot": snapshot_id},
               after={"action": "canvas.snapshot_rollback",
                      "clue_id": clue_id, "target_snapshot_id": snapshot_id,
                      "recovery_snapshot_id": recovery_id,
                      "stale_node_ids": stale_ids,
                      "version": row["version"]})
        return ok(_edit_payload(
            row, target_snapshot_id=snapshot_id,
            recovery_snapshot_id=recovery_id,
            stale_node_ids=stale_ids,
            recovery_snapshot=_snapshot_dto(recovery)),
            data_version=version)
    finally:
        state.close()


# ======================================================================
# M4 RC-105：手册核实建议（生成虚节点 → 采纳 202 → 终态协调）
# 结构变更全部走本文件系统通道（_commit_edit），不经 PATCH。
# ======================================================================
def _reject_agent_canvas(p: Principal, action: str) -> None:
    """核查写动作红线：agent: 身份不得采纳/转待核实（Worker 同款兜底）。"""
    if p.operator.startswith("agent:"):
        raise APIError(
            ERR_FORBIDDEN,
            f"Agent 身份 {p.operator!r} 不得{action}（核查写操作须由"
            "具名人工触发；LLM 建议须走提案审批）", 403)


class CanvasSuggestIn(BaseModel):
    node_id: str
    version: int | None = None


class CanvasSuggestAdoptIn(BaseModel):
    text: str | None = None
    version: int | None = None


class SuggestSyncTargetIn(BaseModel):
    node_id: str
    text: str | None = None


class SuggestSyncIn(BaseModel):
    targets: list[SuggestSyncTargetIn]
    version: int | None = None


class HypothesisToVerifyIn(BaseModel):
    text: str
    version: int | None = None


def _jian_gate(detail: dict, case, base_dir, p: Principal,
               clue_id: str) -> None:
    if not can_see_jian_types(
            detail.get("jian_types") or [], role=p.role,
            jian_clearances=ontology_meta.jian_clearances(
                case.pack_id, base_dir)):
        raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/suggestions")
def generate_suggestions(case_id: str, clue_id: str, body: CanvasSuggestIn,
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """规则节点「生成手册核实建议」（RC-105）：纯只读 playbook 匹配，
    产物为画布专属 pb: 虚节点（不写 state、不进核查工作台）。

    匹配口径与 verify_provision.render_suggested 完全一致；
    合并线索按所点规则节点的 rule_id 渲染（每规则独立生成）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        rule_node = next((n for n in current["doc"].get("nodes", [])
                          if n.get("id") == body.node_id), None)
        if rule_node is None:
            raise APIError(ERR_NOT_FOUND,
                           f"画布节点不存在：{body.node_id}", 404)
        if rule_node.get("kind") != "rule":
            raise APIError(ERR_VALIDATION,
                           "仅规则节点可生成手册核实建议", 400)

        cross_rows = _aggregate_cross_rows(
            ctx, case_id, case.pack_id, version, base_dir)
        access = access_for(p, case_id=case_id,
                            purpose="画布生成手册核实建议")
        # 只读装配（state_store=None：零 state 写入；auto 项也不供给）
        detail = clues_view.assemble_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            clue_id=clue_id, state_map=state.status_map(), decisions=[],
            access=access, pack_id=case.pack_id, base_dir=base_dir,
            state_store=None, cross_rows=cross_rows)
        if detail is None:
            raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
        _jian_gate(detail, case, base_dir, p, clue_id)

        raw = clues_view.load_clue_raw(
            ctx.factory.case_dir(case_id), version, clue_id)
        if raw is None:
            raise APIError(ERR_NOT_FOUND, f"线索不存在：{clue_id}", 404)
        raw_for_view = clues_view.build_view_raw(
            raw, cross_rows=cross_rows, pack_id=case.pack_id,
            base_dir=base_dir)
        rule_id = str(rule_node.get("ref") or "")
        per_rule = {
            **raw_for_view,
            "detail": {**(raw_for_view.get("detail") or {}),
                       "rule_id": rule_id},
        }
        items, render_skipped = render_suggested(
            per_rule, detail.get("evidence") or [],
            pack_id=case.pack_id, base_dir=base_dir)

        doc = copy.deepcopy(current["doc"])
        merged = canvas_infer.add_suggestion_nodes(
            doc, rule_node_id=body.node_id, items=items, now=_now())
        row = current
        if merged["added_nodes"]:
            row = _commit_edit(state, clue_id, doc, operator=p.operator,
                               base_version=body.version)
            _audit(state, case_id=case_id, version=version,
                   operator=p.operator, action="canvas.suggestions",
                   before={"version": current["version"]},
                   after={"action": "canvas.suggestions",
                          "clue_id": clue_id, "rule_id": rule_id,
                          "node_id": body.node_id,
                          "playbook_ids": [
                              (n.get("props") or {}).get("playbook_id")
                              for n in merged["added_nodes"]],
                          "version": row["version"],
                          "nodes": len(merged["added_nodes"])})
        payload = _edit_payload(
            row, added_nodes=merged["added_nodes"],
            added_edges=merged["added_edges"],
            skipped=merged["skipped"] + [
                {"playbook_id": sk.get("playbook_id", ""),
                 "reason": sk.get("reason", "")}
                for sk in render_skipped])
        return ok(payload, data_version=version)
    finally:
        state.close()


def _verify_enqueue(p: Principal, *, ctx: WebContext, case_id: str,
                    clue_id: str, params: dict, idem: str):
    """TASK_VERIFY 入队（身份快照与既有 verify-items 端点同构）。"""
    params.update({"operator": p.operator, "role": p.role,
                   "clearance": p.clearance})
    return enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
        params=params, idem_key=idem, created_by=p.operator)


@router.post(
    "/cases/{case_id}/clues/{clue_id}/canvas/suggestions/{node_id}/adopt",
    status_code=202)
def adopt_suggestion(case_id: str, clue_id: str, node_id: str,
                     body: CanvasSuggestAdoptIn,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """采纳手册建议（RC-105）：仅入队既有 TASK_VERIFY（add_manual 或
    建议→待核查 transition）→ 202；画布不变更，终态后由 sync 协调。

    终态失败不产生已采纳假象（sync 对 pending 项保持虚节点，前端标红重试）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    _reject_agent_canvas(p, "采纳手册建议")
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        try:
            plan = canvas_infer.plan_adoption(
                current["doc"], node_id, state.list_verify_items(clue_id))
        except canvas_infer.CanvasInferError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)

        override = (body.text or "").strip()
        text = override or str(plan.get("text") or "").strip()
        if not text:
            raise APIError(ERR_VALIDATION, "核查项文本不能为空（text）", 400)

        mode = plan["mode"]
        if mode == "adopted":
            # state 已有同文本已处理项：无需入队，前端直接 sync 即可一致
            return ok({"task": None, "mode": "adopted", "node_id": node_id,
                       "item_id": plan.get("item_id"),
                       "effective_text": text}, data_version=version)

        if mode == "transition":
            item_id = str(plan["item_id"])
            params = {"op": "transition", "clue_id": clue_id,
                      "item_id": item_id, "next_status": PENDING,
                      "conclusion": ""}
            if override:
                params["text"] = override
            idem = f"canvas-adopt:{item_id}:{PENDING}:{_sha1(override)}"
        else:
            params = {"op": "add_manual", "clue_id": clue_id, "text": text}
            for k in ("channel", "ref_function", "falsification",
                      "external"):
                if plan.get(k):
                    params[k] = plan[k]
            idem = f"canvas-add:{clue_id}:{_sha1(text)}"
        task = _verify_enqueue(
            p, ctx=ctx, case_id=case_id, clue_id=clue_id,
            params=params, idem=idem)
        return ok({"task": task_dto(task), "mode": mode,
                   "node_id": node_id, "effective_text": text},
                  data_version=version)
    finally:
        state.close()


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/suggestions/sync")
def sync_suggestions(case_id: str, clue_id: str, body: SuggestSyncIn,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """202 任务终态后的画布协调（RC-105）：pb 虚节点→已采纳 vi 节点、
    人工假设→新增待核实节点；state 未就绪的目标保持 pending（可重试）。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        doc = copy.deepcopy(current["doc"])
        try:
            result = canvas_infer.reconcile(
                doc, state_items=state.list_verify_items(clue_id),
                targets=[{"node_id": t.node_id,
                          "text": t.text if t.text is not None else None}
                         for t in body.targets])
        except canvas_infer.CanvasInferError as exc:
            raise APIError(ERR_VALIDATION, str(exc), 400)
        row = current
        if result["changed"]:
            row = _commit_edit(state, clue_id, doc, operator=p.operator,
                               base_version=body.version)
            _audit(state, case_id=case_id, version=version,
                   operator=p.operator, action="canvas.suggestion_sync",
                   before={"version": current["version"]},
                   after={"action": "canvas.suggestion_sync",
                          "clue_id": clue_id,
                          "targets": len(body.targets),
                          "results": result["results"],
                          "version": row["version"]})
        return ok(_edit_payload(row, results=result["results"]),
                  data_version=version)
    finally:
        state.close()


@router.post(
    "/cases/{case_id}/clues/{clue_id}/canvas/nodes/{node_id}/to-verify",
    status_code=202)
def hypothesis_to_verify(case_id: str, clue_id: str, node_id: str,
                         body: HypothesisToVerifyIn,
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """人工假设「转为待核实」（RC-105）：确认文本（默认带入假设内容）→
    既有 add_manual 202 通道；终态后 sync 生成 verify_item 节点。"""
    _get_owned_case(case_id, p, ctx.cases)
    _reject_agent_canvas(p, "将假设转为待核实")
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = _load_for_edit(state, clue_id, body.version)
        node = next((n for n in current["doc"].get("nodes", [])
                     if n.get("id") == node_id), None)
        if node is None:
            raise APIError(ERR_NOT_FOUND,
                           f"画布节点不存在：{node_id}", 404)
        if node.get("kind") != "hypothesis" or node.get("system"):
            raise APIError(ERR_VALIDATION,
                           "仅人工假设节点可转为待核实", 400)
        text = (body.text or "").strip()
        if not text:
            raise APIError(ERR_VALIDATION, "核查项文本不能为空（text）", 400)
        task = _verify_enqueue(
            p, ctx=ctx, case_id=case_id, clue_id=clue_id,
            params={"op": "add_manual", "clue_id": clue_id, "text": text},
            idem=f"canvas-hyp:{clue_id}:{_sha1(text)}")
        return ok({"task": task_dto(task), "mode": "add_manual",
                   "node_id": node_id, "effective_text": text},
                  data_version=version)
    finally:
        state.close()


# ======================================================================
# M4 RC-204：白名单只读 Function 扩展查询（function_result 节点）
# ======================================================================
class FunctionQueryIn(BaseModel):
    function: str
    params: dict = {}
    source_node_id: str | None = None
    version: int | None = None


@router.get("/cases/{case_id}/clues/{clue_id}/canvas/functions")
def list_canvas_functions(case_id: str, clue_id: str,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """工具箱「扩展查询」白名单目录（业务化表单，不暴露 SQL/impl）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    try:
        spec = load_pack(case.pack_id, base_dir)
    except Exception as exc:  # 案件包装载失败：空目录 + 可读原因，不 500
        logger.warning("canvas functions 案件包装载失败 case=%s：%s",
                       case_id, exc)
        return ok({"functions": [], "available": False})
    return ok({"functions": canvas_infer.function_forms(spec.functions),
               "available": True})


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/function-query")
def canvas_function_query(case_id: str, clue_id: str, body: FunctionQueryIn,
                          p: Principal = Depends(get_principal),
                          ctx: WebContext = Depends(get_ctx)):
    """扩展查询执行（RC-204）：白名单只读 Function → function_result 节点 +
    「查询自」系统边；入参快照随节点 props 与审计链留痕。

    - 非白名单/枚举外参数 → 400（复用模板参数同一校验决策点）；
    - 语义层未接入（版本文件缺失）/执行降级 → executed=false，不产生空节点；
    - 只经 read 版本库 + ReadOnlyStore 护栏，不写业务库。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    fname = (body.function or "").strip()
    if fname not in canvas_infer.CANVAS_FUNCTION_WHITELIST:
        raise APIError(
            ERR_VALIDATION,
            f"函数 {fname!r} 不在画布扩展查询白名单"
            f"（允许：{', '.join(sorted(canvas_infer.CANVAS_FUNCTION_WHITELIST))}）",
            400)
    try:
        spec = load_pack(case.pack_id, base_dir).functions.get(fname)
    except Exception as exc:
        logger.warning("canvas function-query 装载失败 case=%s：%s",
                       case_id, exc)
        spec = None
    if spec is None:
        raise APIError(ERR_VALIDATION,
                       f"案件包未声明该函数：{fname}", 400)
    # 参数校验在开库前（string enum 外值/类型错误快速失败，无需语义库）
    try:
        merged = canvas_infer.merge_query_params(spec, body.params or {})
    except canvas_infer.CanvasInferError as exc:
        raise APIError(ERR_VALIDATION, str(exc), 400)

    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    ro_store = None
    try:
        current = _load_for_edit(state, clue_id, body.version)
        source_id = body.source_node_id
        if source_id:
            if not any(n.get("id") == source_id
                       for n in current["doc"].get("nodes", [])):
                raise APIError(ERR_NOT_FOUND,
                               f"画布节点不存在：{source_id}", 404)

        # ---- 只读执行（语义库缺失 → 业务化降级，不产生节点）----
        try:
            ro_store = ctx.factory.for_case(case_id, mode="read",
                                            version=version)
        except (FileNotFoundError, OSError) as exc:
            logger.info("canvas function-query 数据源未接入 case=%s v=%s：%s",
                        case_id, version, exc)
            return ok({"executed": False, "code": "DATASOURCE_UNAVAILABLE",
                       "message": "数据源未接入，请先完成数据构建（BUILD）",
                       "function": fname, "params": merged},
                      data_version=version)
        access = access_for(p, case_id=case_id, purpose="画布扩展查询")
        executor = FunctionExecutor(
            ro_store, pack=case.pack_id, access=access, base_dir=base_dir)
        try:
            out = executor.invoke(fname, merged)
        except PermissionError as exc:
            raise APIError(ERR_FORBIDDEN,
                           f"无权调用该查询（间类策略）：{exc}", 403)
        except FileNotFoundError as exc:
            # 版本库句柄惰性打开：invoke 期才暴露数据源缺失，统一回
            # executed=false 业务信封（不落节点、不涨版本、不 500）
            logger.info("canvas function-query 数据源缺失 case=%s v=%s：%s",
                        case_id, version, exc)
            return ok({"executed": False, "code": "DATASOURCE_UNAVAILABLE",
                       "message": "数据源未接入，请先完成数据构建（BUILD）",
                       "function": fname, "params": merged},
                      data_version=version)
        except Exception as exc:  # noqa: BLE001 —— 边界统一转信封
            logger.error("canvas function-query 执行失败 case=%s fn=%s",
                         case_id, fname, exc_info=True)
            raise APIError(ERR_INTERNAL,
                           f"查询执行失败：{type(exc).__name__}: {exc}",
                           500)
        params_used = out.get("params_used", merged)
        if out.get("degraded"):
            # 语义表缺失/schema 不符：零命中降级，不落空节点
            return ok({"executed": False, "code": "DEGRADED",
                       "message": "查询依赖的语义表未接入或结构不符",
                       "reason": out.get("degraded_reason") or "",
                       "function": fname, "params": params_used},
                      data_version=version)

        # ---- 成功：系统通道落 function_result 节点 ----
        result_id = f"fr_{uuid.uuid4().hex[:12]}"
        now = _now()
        doc = copy.deepcopy(current["doc"])
        built = canvas_infer.build_function_result(
            doc, spec=spec, out=out, params_used=params_used,
            source_node_id=source_id, operator=p.operator, now=now,
            result_id=result_id)
        row = _commit_edit(state, clue_id, doc, operator=p.operator,
                           base_version=body.version)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.function_query",
               before={"version": current["version"]},
               after={"action": "canvas.function_query",
                      "clue_id": clue_id, "function": fname,
                      "params": params_used, "node_id": built["node"]["id"],
                      "source_node_id": source_id,
                      "executed_at": now,
                      "row_count": built["summary"].get("row_count"),
                      "version": row["version"]})
        return ok(_edit_payload(row, executed=True,
                                node=built["node"], edge=built["edge"],
                                result=built["summary"],
                                params=params_used),
                  data_version=version)
    finally:
        state.close()
        if ro_store is not None:
            ro_store.close()


# ----------------------------------------------------------------------
# M5 RC-301：画布只读问答（RC-302 引用校验串联）
# ----------------------------------------------------------------------
class CanvasChatIn(BaseModel):
    question: str


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/chat")
def canvas_chat_endpoint(case_id: str, clue_id: str, body: CanvasChatIn,
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """画布只读问答（RC-301）：就当刻画布提问，返回带引用回答。

    - 上下文为画布业务视图（脱敏后出网）；
    - 回答过 RC-302 引用校验：有据句入 facts、无据句入 pending、假引用剔除；
    - 问答不写画布（只读）；消息留痕 clue_canvas_chat；
    - llm_enabled=false / network=isolated → 业务错误码，非 500。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    version = ctx.repo.current_version(case_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)

    q = (body.question or "").strip()
    if not q:
        raise APIError(ERR_VALIDATION, "请输入问题", 400)
    if len(q) > 500:
        raise APIError(ERR_VALIDATION, "问题不超过 500 字", 400)

    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        current = state.get_canvas(clue_id)
        if current is None:
            raise APIError(ERR_NOT_FOUND,
                           f"画布不存在：{clue_id}（请先进入研判画布 Tab）", 404)
        doc = current["doc"]

        access = access_for(p, case_id=case_id, purpose="画布问答")
        result = canvas_chat.chat_on_canvas(
            conn=state.conn, ctx=access, doc=doc, question=q,
            pack_id=case.pack_id, base_dir=base_dir)

        # 消息留痕（user + assistant）
        now = _now()
        state.insert_canvas_chat(
            message_id=canvas_chat.new_message_id(), clue_id=clue_id,
            role="user", content=q, citations=[], warnings=[],
            created_by=p.operator, created_at=now)
        if result["ok"]:
            state.insert_canvas_chat(
                message_id=canvas_chat.new_message_id(), clue_id=clue_id,
                role="assistant", content=result["answer"],
                citations=result["citations"], warnings=result["warnings"],
                created_by="system", created_at=now)

        # 审计（RC-401）：问答是只读动作，记 operator/问题长度/模型，不记内容
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.chat",
               before=None,
               after={"action": "canvas.chat", "clue_id": clue_id,
                      "question_len": len(q), "model": result.get("model"),
                      "ok": result["ok"], "error": result.get("error")})

        if not result["ok"]:
            # 业务错误（llm_disabled / blocked / error），非 500
            raise APIError(
                ERR_FORBIDDEN if result.get("error") in (
                    "llm_disabled", "deployment_off", "llm_blocked")
                else ERR_INTERNAL,
                result["warnings"][0] if result["warnings"]
                else "智能问答不可用",
                403 if result.get("error") in (
                    "llm_disabled", "deployment_off", "llm_blocked")
                else 500)

        return ok({
            "answer": result["answer"],
            "facts": result["facts"],
            "pending": result["pending"],
            "warnings": result["warnings"],
            "citations": result["citations"],
            "model": result["model"],
        }, data_version=version)
    finally:
        state.close()


# ----------------------------------------------------------------------
# M5 RC-303：问答建议转审批（proposal 桥接，不直接落画布/核查项）
# ----------------------------------------------------------------------
class CanvasSuggestionIn(BaseModel):
    text: str


@router.post("/cases/{case_id}/clues/{clue_id}/canvas/chat/suggestion")
def canvas_chat_suggestion(case_id: str, clue_id: str,
                           body: CanvasSuggestionIn,
                           p: Principal = Depends(get_principal),
                           ctx: WebContext = Depends(get_ctx)):
    """把问答产出的核实建议提交为提案（RC-303）。

    - 不直接落画布/核查项，仅创建 verify_item 提案（draft 态）；
    - 审批走既有 /proposals/{id}/decide；approve 后由既有桥接生成
      TASK_VERIFY，终态后画布 sync 出 verify_item 节点；
    - operator 以 agent: 开头直接拒绝（沿用既有硬约束）。
    """
    _get_owned_case(case_id, p, ctx.cases)
    if p.operator.startswith("agent:"):
        raise APIError(ERR_FORBIDDEN,
                       "agent 会话不得提交核实建议（须自然人确认）", 403)

    text = (body.text or "").strip()
    if not text:
        raise APIError(ERR_VALIDATION, "建议内容不能为空", 400)
    if len(text) > 500:
        raise APIError(ERR_VALIDATION, "建议内容不超过 500 字", 400)

    # 打开全局提案库（与 MCP review.submit_proposal 同库）
    pconn = duckdb.connect(ctx.proposals_db)
    try:
        store = ProposalStore(pconn, pack="default")
        proposal_id = "pp_" + uuid.uuid4().hex[:16]
        proposal = {
            "proposal_id": proposal_id,
            "kind": "verify_item",
            "case_id": case_id,
            "author": p.operator,
            "candidate": {
                "clue_id": clue_id,
                "text": text,
                "kind": "画布问答建议",
            },
            "input": {
                "origin": "canvas_chat",
                "clue_id": clue_id,
            },
        }
        try:
            store.submit(proposal, actor=f"web:{p.operator}")
        except ProposalValidationError as e:
            raise APIError(ERR_VALIDATION,
                           f"提案校验失败：{'; '.join(e.errors)}", 400)
    finally:
        pconn.close()

    # 审计（RC-401）
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        version = ctx.repo.current_version(case_id)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.chat_suggestion",
               before=None,
               after={"action": "canvas.chat_suggestion",
                      "clue_id": clue_id, "proposal_id": proposal_id,
                      "text_len": len(text)})
    finally:
        state.close()

    return ok({
        "proposal_id": proposal_id,
        "status": "draft",
        "message": "已提交为核实建议，待审批后生效",
    }, data_version=ctx.repo.current_version(case_id))


# ======================================================================
# M6 RC-304/305/306：研判报告（生成/列表/详情/导出）
# ======================================================================

def _report_dto(r: dict) -> dict:
    """报告 DTO（列表与详情共用；详情额外带 sections/citations/warnings）。"""
    return {
        "report_id": r["report_id"],
        "clue_id": r["clue_id"],
        "version_no": int(r["version_no"]),
        "snapshot_id": r["snapshot_id"],
        "status": r["status"],
        "model": r.get("model", ""),
        "error": r.get("error", ""),
        "task_id": r.get("task_id", ""),
        "extra_request": r.get("extra_request", ""),
        "content_md": r.get("content_md", ""),
        "sections": r.get("sections", {}),
        "citations": r.get("citations", []),
        "warnings": r.get("warnings", []),
        "created_by": r.get("created_by", ""),
        "created_at": r.get("created_at", ""),
    }


def _report_list_dto(r: dict) -> dict:
    """列表项精简版（不含 content_md/sections 全文）。"""
    d = _report_dto(r)
    d.pop("content_md", None)
    d.pop("sections", None)
    d["warning_count"] = len(d.pop("warnings", []) or [])
    d["citation_count"] = len(d.pop("citations", []) or [])
    return d


class ReportGenerateIn(BaseModel):
    extra_request: str = ""
    version: int | None = None


@router.post(
    "/cases/{case_id}/clues/{clue_id}/canvas/reports",
    status_code=202)
def generate_report(case_id: str, clue_id: str, body: ReportGenerateIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """生成研判报告（RC-304）：先冻结不可变快照 → 入队 TASK_REPORT。

    - 每次点击产生 version_no 递增的不可变版本；
    - 报告与快照一一关联（origin=report, report_id=新报告 ID）；
    - 202 异步任务（沿用任务池/终态轮询）；
    - llm_enabled=false 时按钮应禁用；服务端仍兜底拒绝。
    """
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        # 1. 读当前画布
        current = _load_for_edit(state, clue_id, body.version)
        # 2. 冻结快照（origin=report）
        report_id = f"rpt_{uuid.uuid4().hex[:16]}"
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        now = _now()
        snap = state.insert_canvas_snapshot(
            clue_id=clue_id, snapshot_id=snapshot_id,
            doc=copy.deepcopy(current["doc"]), label=f"报告 {report_id}",
            origin="report", report_id=report_id,
            created_by=p.operator, created_at=now)
        # 3. 计算 version_no = 已有最大版本 + 1
        existing = state.list_canvas_reports(clue_id)
        max_vn = max((int(r["version_no"]) for r in existing), default=0)
        version_no = max_vn + 1
        # 4. 插入 report 行（status=generating）
        report = state.insert_canvas_report(
            report_id=report_id, clue_id=clue_id,
            version_no=version_no, snapshot_id=snapshot_id,
            created_by=p.operator, created_at=now)
        # 5. 入队 TASK_REPORT
        case = ctx.repo.get_case(case_id)
        extra = (body.extra_request or "").strip()
        if len(extra) > 300:
            raise APIError(ERR_VALIDATION,
                           "补充要求不超过 300 字", 400)
        task = enqueue_task(
            ctx.repo, case_id=case_id, task_type=TASK_REPORT,
            params={"report_id": report_id, "clue_id": clue_id,
                    "operator": p.operator, "role": p.role,
                    "clearance": p.clearance,
                    "extra_request": extra},
            idem_key=f"report:{clue_id}:{report_id}",
            created_by=p.operator)
        # 6. 回写 task_id
        state.update_canvas_report_status(
            report_id, status="generating", task_id=task.id)
        # 7. 审计
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.report_generate",
               before=None,
               after={"action": "canvas.report_generate",
                      "clue_id": clue_id, "report_id": report_id,
                      "snapshot_id": snapshot_id,
                      "version_no": version_no,
                      "task_id": task.id})
        return ok({
            "report_id": report_id,
            "version_no": version_no,
            "snapshot_id": snapshot_id,
            "task_id": task.id,
            "status": "generating",
        }, data_version=version)
    finally:
        state.close()


@router.get("/cases/{case_id}/clues/{clue_id}/canvas/reports")
def list_reports(case_id: str, clue_id: str,
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """报告版本列表（倒序）。"""
    _get_owned_case(case_id, p, ctx.cases)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        reports = state.list_canvas_reports(clue_id)
        return ok({"reports": [_report_list_dto(r) for r in reports]},
                  data_version=ctx.repo.current_version(case_id))
    finally:
        state.close()


@router.get(
    "/cases/{case_id}/clues/{clue_id}/canvas/reports/{report_id}")
def get_report(case_id: str, clue_id: str, report_id: str,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """报告详情（sections + citations + warnings + content_md）。"""
    _get_owned_case(case_id, p, ctx.cases)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        r = state.get_canvas_report(report_id)
        if r is None or r["clue_id"] != clue_id:
            raise APIError(ERR_NOT_FOUND, f"报告不存在：{report_id}", 404)
        return ok({"report": _report_dto(r)},
                  data_version=ctx.repo.current_version(case_id))
    finally:
        state.close()


@router.get(
    "/cases/{case_id}/clues/{clue_id}/canvas/reports/{report_id}/export.md")
def export_report_md(case_id: str, clue_id: str, report_id: str,
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """Markdown 导出（RC-306）：含附录引用索引。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        r = state.get_canvas_report(report_id)
        if r is None or r["clue_id"] != clue_id:
            raise APIError(ERR_NOT_FOUND, f"报告不存在：{report_id}", 404)
        if r["status"] != "ready":
            raise APIError(ERR_CONFLICT,
                           f"报告尚未就绪（当前状态：{r['status']}）", 409)
        md = canvas_report.render_markdown(
            r.get("sections", {}), r.get("citations", []))
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.report_export_md",
               before=None,
               after={"action": "canvas.report_export_md",
                      "clue_id": clue_id, "report_id": report_id,
                      "version_no": r["version_no"]})
        filename = f"研判报告_{clue_id}_v{r['version_no']}_{r['created_at'][:10]}.md"
        return Response(
            content=md,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f"attachment; filename=\"report_v{r['version_no']}.md\"; "
                     f"filename*=UTF-8''{_url_quote(filename)}"})
    finally:
        state.close()


@router.get(
    "/cases/{case_id}/clues/{clue_id}/canvas/reports/{report_id}/export.docx")
def export_report_docx(case_id: str, clue_id: str, report_id: str,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """Word 导出（RC-306）：python-docx 生成，含引用附录表格。"""
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    state = StateStore(case_id, ctx.factory.case_dir(case_id) / "state.sqlite")
    try:
        r = state.get_canvas_report(report_id)
        if r is None or r["clue_id"] != clue_id:
            raise APIError(ERR_NOT_FOUND, f"报告不存在：{report_id}", 404)
        if r["status"] != "ready":
            raise APIError(ERR_CONFLICT,
                           f"报告尚未就绪（当前状态：{r['status']}）", 409)
        try:
            docx_bytes = canvas_report.render_docx(
                r.get("sections", {}), r.get("citations", []))
        except ImportError:
            raise APIError(
                ERR_INTERNAL,
                "Word 导出依赖 python-docx 未安装，请改用 Markdown 复制",
                500)
        _audit(state, case_id=case_id, version=version,
               operator=p.operator, action="canvas.report_export_docx",
               before=None,
               after={"action": "canvas.report_export_docx",
                      "clue_id": clue_id, "report_id": report_id,
                      "version_no": r["version_no"]})
        filename = f"研判报告_{clue_id}_v{r['version_no']}_{r['created_at'][:10]}.docx"
        return Response(
            content=docx_bytes,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"),
            headers={"Content-Disposition":
                     f"attachment; filename=\"report_v{r['version_no']}.docx\"; "
                     f"filename*=UTF-8''{_url_quote(filename)}"})
    finally:
        state.close()
