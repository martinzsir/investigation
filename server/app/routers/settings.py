"""
server/app/routers/settings.py
W-P-013 系统设置（queue/resources/health）/ W-P-017 配置中心
（policies-thresholds/snapshots/features）。

纪律：
  - 平台级配置存 meta.settings_kv，**不作案件快照回灌**（新建案件取平台
    默认，生效值以案件快照为准——响应明示）；
  - 写仅 admin + **reason 必填** + record_platform_event 审计；
  - 键白名单 + 区间/enum 校验，白名单外 400；llm_enabled 等红线/安全键
    永不进入白名单（FE-T-011：前端 🔒 锁定项后端同样拒绝）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, ERR_VALIDATION, APIError, ok
from server.app.security import Principal

router = APIRouter(prefix="/settings", tags=["settings"])

_BACKEND_VERSION = "M6"
_DEFAULT_MAX_WORKERS = 2
_DEFAULT_POLL_MS = 100

# ---- 白名单（键 → 校验器）；红线键（llm_enabled 等）不在此即拒绝 ----
_QUEUE_KEYS = {
    "max_workers": lambda v: isinstance(v, int) and 1 <= v <= 16,
    "poll_interval_ms": lambda v: isinstance(v, int) and v >= 10,
}
_RESOURCE_KEYS = {
    "max_rows_default": lambda v: isinstance(v, int) and 1 <= v <= 100000,
    "query_timeout_ms": lambda v: isinstance(v, int) and 100 <= v <= 600000,
    # storage_root 为部署路径：GET 透出、永不可经 API 改写
}
_FEATURE_KEYS = {
    "ui_density": lambda v: v in ("compact", "comfortable"),
}
_THRESHOLD_KEYS = {
    # 五间交叉升格/降级的平台默认阈值（红线下限：单源不升格/最少线索数）
    "cross_level_min_sources": lambda v: isinstance(v, int) and v >= 1,
    "cross_level_min_clues": lambda v: isinstance(v, int) and v >= 2,
    "stale_days": lambda v: isinstance(v, int) and 1 <= v <= 3650,
}

_DEFAULTS = {
    "queue": {"max_workers": _DEFAULT_MAX_WORKERS,
              "poll_interval_ms": _DEFAULT_POLL_MS},
    "resources": {"max_rows_default": 1000, "query_timeout_ms": 30000},
    "features": {"ui_density": "comfortable"},
    "policies_thresholds": {
        "cross_level_min_sources": 1, "cross_level_min_clues": 2,
        "stale_days": 14},
}


class SettingsIn(BaseModel):
    reason: str = ""
    values: dict


def _require_admin(p: Principal, ctx: WebContext, endpoint: str):
    user = ctx.repo.get_user(p.operator)
    if not ((user is not None and user.is_admin == 1) or p.role == "system"):
        ctx.repo.record_platform_event(
            "authz_failure", operator=p.operator, tenant_id=p.tenant_id,
            detail={"endpoint": endpoint, "reason": "非管理员访问平台设置"})
        raise APIError(ERR_FORBIDDEN, "平台设置仅管理员可操作", 403)


def _merge_defaults(scope: str, stored: dict) -> dict:
    return {**_DEFAULTS[scope], **(stored or {})}


def _get_scope(ctx: WebContext, scope: str) -> dict:
    return _merge_defaults(scope, ctx.repo.get_setting(scope, {}) or {})


def _put_scope(endpoint: str, scope: str, allowed: dict,
               body: SettingsIn, p: Principal, ctx: WebContext,
               *, note: str = ""):
    _require_admin(p, ctx, endpoint)
    reason = body.reason.strip()
    if not reason:
        raise APIError(ERR_VALIDATION, "写平台设置必须填写 reason（审计留痕）",
                       400)
    values = body.values or {}
    unknown = [k for k in values if k not in allowed]
    if unknown:
        raise APIError(ERR_VALIDATION,
                       f"不可配置/未知键：{unknown}（白名单 {sorted(allowed)}）",
                       400)
    bad = [k for k, v in values.items() if not allowed[k](v)]
    if bad:
        raise APIError(ERR_VALIDATION, f"键值超出允许区间/枚举：{bad}", 400)
    merged = {**_get_scope(ctx, scope), **values}
    ctx.repo.set_setting(scope, merged, by=p.operator, reason=reason)
    ctx.repo.record_platform_event(
        "settings_change", operator=p.operator, tenant_id=p.tenant_id,
        detail={"scope": scope, "changed": sorted(values), "reason": reason})
    out = {scope: _merge_defaults(scope, merged)}
    if note:
        out["note"] = note
    return ok(out)


# ----------------------------------------------------------------------
# W-P-013 系统设置
# ----------------------------------------------------------------------
@router.get("/queue")
def get_queue(p: Principal = Depends(get_principal),
              ctx: WebContext = Depends(get_ctx)):
    _require_admin(p, ctx, "/settings/queue")
    return ok(_get_scope(ctx, "queue"))


@router.put("/queue")
def put_queue(body: SettingsIn, p: Principal = Depends(get_principal),
              ctx: WebContext = Depends(get_ctx)):
    return _put_scope("/settings/queue", "queue", _QUEUE_KEYS, body, p, ctx)


@router.get("/resources")
def get_resources(p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    _require_admin(p, ctx, "/settings/resources")
    data = _get_scope(ctx, "resources")
    data["storage_root"] = str(ctx.factory.cases_root)
    return ok(data)


@router.put("/resources")
def put_resources(body: SettingsIn, p: Principal = Depends(get_principal),
                  ctx: WebContext = Depends(get_ctx)):
    return _put_scope("/settings/resources", "resources", _RESOURCE_KEYS,
                      body, p, ctx)


@router.get("/health")
def settings_health(p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """健康探针（登录可读）：meta/队列/Worker 缺省配置/版本。"""
    try:
        pending = len(ctx.repo.list_tasks(status="PENDING", limit=500))
        running = len(ctx.repo.list_tasks(status="RUNNING", limit=500))
        meta_ok = True
    except Exception:
        pending = running = 0
        meta_ok = False
    queue_cfg = _get_scope(ctx, "queue")
    return ok({
        "meta_ok": meta_ok,
        "queue": {"pending": pending, "running": running},
        # API 进程不持有 Worker 池（池为独立部署/测试驱动）：存活探测以
        # 队列积压为信号，配置值透传平台设置。
        "worker": {"pool_alive": False,
                   "max_workers": queue_cfg["max_workers"],
                   "poll_interval_ms": queue_cfg["poll_interval_ms"],
                   "note": "API 进程不内嵌 Worker；池存活由部署侧监控"},
        "versions": {"backend": _BACKEND_VERSION,
                     "ontology_default": "default"},
    })


# ----------------------------------------------------------------------
# W-P-017 配置中心
# ----------------------------------------------------------------------
@router.get("/policies-thresholds")
def get_thresholds(p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    _require_admin(p, ctx, "/settings/policies-thresholds")
    return ok({
        "thresholds": _get_scope(ctx, "policies_thresholds"),
        "note": "平台值仅用于新建案件默认；案件生效阈值以案件快照 "
                "thresholds.json 为准（D-M6-10）",
    })


@router.put("/policies-thresholds")
def put_thresholds(body: SettingsIn, p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    return _put_scope(
        "/settings/policies-thresholds", "policies_thresholds",
        _THRESHOLD_KEYS, body, p, ctx,
        note="平台值仅用于新建案件默认；案件生效阈值以案件快照为准")


@router.get("/snapshots")
def list_snapshots(p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    """跨案件本体快照列表（admin）。"""
    _require_admin(p, ctx, "/settings/snapshots")
    rows = ctx.repo.list_pack_snapshots()
    return ok({"items": rows, "total": len(rows)})


@router.get("/features")
def get_features(p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    _require_admin(p, ctx, "/settings/features")
    return ok(_get_scope(ctx, "features"))


@router.put("/features")
def put_features(body: SettingsIn, p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    return _put_scope("/settings/features", "features", _FEATURE_KEYS, body,
                      p, ctx)
