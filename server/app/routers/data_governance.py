"""
server/app/routers/data_governance.py
W-011 可选属性缺列降级（M4 阶段 B，数据治理页）。

读：GET /cases/{cid}/governance/missing-columns —— 聚合 run_diagnostic 中
    source_column_missing（可选源列缺失→类型化 NULL）与
    source_value_cast_failed（脏值 TRY_CAST 降级）两类警告，按对象/属性
    分组展示。纯读，复用 M2 diagnostics 端点数据源，不重复存储。
AC：缺列降级为警告不崩溃；必填缺列仍硬失败（不产生诊断，BUILD 直接失败）。
权限：登录即可。
"""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends

from server.app import dashboard
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["data-governance"])

_MISSING_KINDS = ("source_column_missing", "source_value_cast_failed")


@router.get("/cases/{case_id}/governance/missing-columns")
def missing_columns(case_id: str,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    items: list[dict] = []
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            for kind in _MISSING_KINDS:
                items.extend(dashboard.list_diagnostics(
                    store.read_conn, kind=kind, limit=1000))
        except FileNotFoundError:
            pass  # 未 BUILD → 空
    finally:
        if store is not None:
            store.close()

    # 按对象/属性分组计数
    groups: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "kinds": set(), "samples": []})
    for it in items:
        det = it.get("detail") if isinstance(it.get("detail"), dict) else {}
        obj = det.get("object") or it.get("object") or ""
        prop = det.get("property") or it.get("property") or ""
        key = f"{obj}.{prop}"
        g = groups[key]
        g["count"] += 1
        g["kinds"].add(it.get("kind"))
        if len(g["samples"]) < 3:
            g["samples"].append(it.get("reason") or det.get("reason") or "")
    result = []
    for key, g in groups.items():
        obj, _, prop = key.partition(".")
        result.append({
            "object": obj, "property": prop,
            "count": g["count"],
            "kinds": sorted(g["kinds"]),
            "samples": g["samples"],
        })
    result.sort(key=lambda x: -x["count"])
    return ok({"items": result, "total_warnings": len(items)},
              data_version=ctx.repo.current_version(case_id))
