"""三维交汇端点。

GET /cases/{cid}/convergence              交汇列表（主体/维度/日期/歧义筛选）
GET /cases/{cid}/convergence/{conv_key}   单条详情（三维命中 + 支撑观察）

定位
----
交汇是**观察档案的归集视图**，不是新的事实源，也不是线索：
  - 不走处置流程（无 status 流转）——与 /observations 同；
  - 不带假设链——它回答"这个锚点上有几个维度的事实"，
    不回答"要验证什么"；要变成命题须经 /observations/{id}/promote 提升。

为什么单独开端点而不是塞进 /observations
----------------------------------------
观察是"单个镜头看到了什么"，交汇是"多个镜头在同一个锚点上是否同时看到"。
两者筛选维度不同（观察按镜头/主体，交汇按维度命中数/精度档），
混在一个列表里会让"哪些是单点发现、哪些是多维印证"重新变模糊。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from server.app import convergence_view
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_NOT_FOUND, ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["convergence"])

_VALID_DIMS = ("space", "time", "relation")


def _close_quietly(store: object | None) -> None:
    try:
        if store is not None and hasattr(store, "close"):
            store.close()
    except Exception:
        pass


def _open_store(ctx: WebContext, case_id: str) -> tuple[object | None,
                                                        object | None,
                                                        str | None]:
    """返回 (store, conn, conn_error)。store 由调用方 finally 关闭。"""
    try:
        store = ctx.factory.for_case(case_id, mode="read")
    except Exception as e:
        return None, None, f"无法打开案件库：{type(e).__name__}: {e}"
    try:
        return store, store.read_conn, None
    except Exception as e:
        return store, None, f"无法获取只读连接：{type(e).__name__}: {e}"


@router.get("/cases/{case_id}/convergence")
def list_convergence(case_id: str,
                     person: str | None = None,
                     dim: str | None = None,
                     date_from: str | None = None,
                     date_to: str | None = None,
                     ambiguous: bool | None = None,
                     min_dims: int = Query(2, ge=1, le=3),
                     page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=200),
                     p: Principal = Depends(get_principal),
                     ctx: WebContext = Depends(get_ctx)):
    """交汇列表。

    min_dims 默认 2——**单维不是交汇**：只有一个维度命中的锚点，
    其观察在原档案里已经能看到，进交汇清单只会稀释真正需要对比看的那些。

    排序由 core 层给定（先按命中维数、再按分数），**不在这里重排**：
    分数已经含精度加权，重排会破坏"3 次时刻级重于 15 次日期级"这条口径。
    """
    _get_owned_case(case_id, p, ctx.cases)
    if dim and dim not in _VALID_DIMS:
        raise APIError(ERR_VALIDATION,
                       f"非法维度：{dim!r}（允许 {_VALID_DIMS}）", 400)
    version = ctx.repo.current_version(case_id)
    store, conn, conn_error = _open_store(ctx, case_id)
    try:
        data = convergence_view.assemble_convergence(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            conn=conn, conn_error=conn_error,
            min_dims=min_dims, dim=dim or None, person=person or None,
            date_from=date_from or None, date_to=date_to or None,
            ambiguous=ambiguous, page=page, page_size=page_size)
    finally:
        _close_quietly(store)
    return ok(data, data_version=version)


@router.get("/cases/{case_id}/convergence/{conv_key}")
def convergence_detail(case_id: str, conv_key: str,
                       fact_limit: int = Query(20, ge=1, le=200),
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """单条交汇详情：三维各自命中了什么 + 支撑观察的判据与事实行。

    conv_key 形如 `person_xxx|2020-03-24`（含 `|`），前端须
    encodeURIComponent 后再拼路径。
    """
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    store, conn, conn_error = _open_store(ctx, case_id)
    try:
        data = convergence_view.assemble_convergence_detail(
            case_dir=ctx.factory.case_dir(case_id), version=version,
            conv_key=conv_key, conn=conn, conn_error=conn_error,
            fact_limit=fact_limit)
    finally:
        _close_quietly(store)
    if data is None:
        raise APIError(ERR_NOT_FOUND, f"交汇不存在：{conv_key}", 404)
    return ok(data, data_version=version)
