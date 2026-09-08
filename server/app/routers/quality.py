"""
server/app/routers/quality.py
W-P-008 质量检查（POST/latest，阶段 D 接线）/ W-P-009 隔离区 /
W-P-010 清洗留痕。

读端点纯消费语义层（build_quarantine / run_diagnostic），未 BUILD 案件
返回空结构不 500；样本只出脱敏值。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.app import quality_view
from server.app.deps import (
    WebContext,
    get_ctx,
    get_principal,
    task_dto,
)
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.meta.models import TASK_PENDING, TASK_RUNNING
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TASK_QUALITY, enqueue_task

router = APIRouter(tags=["quality"])

_QUARANTINE_REASONS = ("cast_error", "null_value", "dedup", "other")


@router.get("/cases/{case_id}/quarantine")
def list_quarantine(case_id: str,
                    reason: str | None = None,
                    page: int = Query(1, ge=1),
                    page_size: int = Query(50, ge=1, le=50),
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """隔离区：CAST 失败整行 + 空值/去重剔除留痕（零隔离给 empty_message）。"""
    _get_owned_case(case_id, p, ctx.cases)
    if reason is not None and reason not in _QUARANTINE_REASONS:
        raise APIError(ERR_VALIDATION,
                       f"reason 非法：{reason}（可用 {_QUARANTINE_REASONS}）",
                       400)
    data = {"items": [], "total": 0,
            "stats": {k: 0 for k in _QUARANTINE_REASONS},
            "page": page, "page_size": page_size,
            "empty_message": quality_view.EMPTY_MESSAGE}
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            data = quality_view.list_quarantine(
                store.read_conn, reason=reason, page=page,
                page_size=page_size)
        except FileNotFoundError:
            pass
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/clean-trace")
def list_clean_trace(case_id: str,
                     object: str | None = None,
                     page: int = Query(1, ge=1),
                     page_size: int = Query(50, ge=1, le=50),
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """清洗留痕：按 (object,property) 聚合剔除/去重/降级诊断。"""
    _get_owned_case(case_id, p, ctx.cases)
    data = {"items": [], "total": 0, "page": page, "page_size": page_size}
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            data = quality_view.list_clean_trace(
                store.read_conn, obj=object, page=page, page_size=page_size)
        except FileNotFoundError:
            pass
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))


# ----------------------------------------------------------------------
# W-P-008：数据质量检查（短频写；落 state.sqlite 不产版本文件）
# ----------------------------------------------------------------------
class QualityCheckIn(BaseModel):
    note: str = ""


def _inflight(ctx: WebContext, case_id: str, task_type: str):
    """同案件进行中（PENDING/RUNNING）的指定类型任务。"""
    rows = (ctx.repo.list_tasks(case_id=case_id, status=TASK_PENDING)
            + ctx.repo.list_tasks(case_id=case_id, status=TASK_RUNNING))
    return next((t for t in rows if t.task_type == task_type), None)


@router.post("/cases/{case_id}/quality-checks", status_code=202)
def run_quality_check(case_id: str,
                      body: QualityCheckIn = QualityCheckIn(),
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """触发数据质量检查（合规/新鲜度/敏感列/单位四扫描；进行中幂等回跳）。"""
    _get_owned_case(case_id, p, ctx.cases)
    inflight = _inflight(ctx, case_id, TASK_QUALITY)
    if inflight is not None:
        return ok(task_dto(inflight),
                  data_version=ctx.repo.current_version(case_id))
    if ctx.repo.current_version(case_id) < 1:
        raise APIError(ERR_VALIDATION, "案件尚未 BUILD，无数据可质检", 400)
    task = enqueue_task(
        ctx.repo, case_id=case_id, task_type=TASK_QUALITY,
        params={"operator": p.operator, "role": p.role,
                "clearance": p.clearance, "note": body.note},
        idem_key=f"quality:{uuid.uuid4().hex[:12]}",
        created_by=p.operator)
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/quality-checks/latest")
def latest_quality_check(case_id: str,
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """最近一次质检报告（未跑过 → available:false，不 500）。"""
    _get_owned_case(case_id, p, ctx.cases)
    data: dict = {"available": False}
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    if path.exists():
        st = StateStore(case_id, path)
        try:
            row = st.latest_quality_check()
            if row is not None:
                data = {"available": True, **row}
        finally:
            st.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))
