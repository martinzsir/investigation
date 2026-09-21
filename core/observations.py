"""观察档案端点。

镜头产出 = **观察**，不是线索：摆出数据的某种结构，不下"这是异常"的判断
（镜头无常态基线，规则才有）。故单列一套端点，与 /clues 平行但**不共享**
处置语义——观察没有 status 流转，只有正兵的认领/归档。

GET  /cases/{cid}/observations              列表（镜头/主体/处置态筛选）
GET  /cases/{cid}/observations/{obs_id}     详情（判据 + 证伪 + 事实明细）

提升为线索（第三期）走独立端点，强制带 hypothesis——不指定假设的线索
无法处置，等于没解决问题。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.app import observations_view
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_NOT_FOUND, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["observations"])


def _read_dispositions(ctx: WebContext, case_id: str) -> dict[str, dict]:
    """正兵对观察的处置动作（state.sqlite，跨版本持久）。

    读失败 → 空 dict：观察浏览本身不依赖处置记录，不该因为状态库
    打不开就整页不可用（全部回落"未认领"）。
    """
    try:
        from server.app.routers.clues import _read_state
        state, _map = _read_state(ctx.factory, case_id)
    except Exception:
        return {}
    try:
        if state is None:
            return {}
        return {d["observation_id"]: d
                for d in state.list_observation_dispositions(case_id)}
    except Exception:
        return {}
    finally:
        try:
            if state is not None:
                state.close()
        except Exception:
            pass


@router.get("/cases/{case_id}/observations")
def list_observations(case_id: str,
                      skill: str | None = None,
                      subject: str | None = None,
                      disposition: str | None = None,
                      page: int = Query(1, ge=1),
                      page_size: int = Query(50, ge=1, le=200),
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """观察档案列表：镜头/主体/处置态筛选 + 分页 + 统计。

    与线索列表**物理分离**：观察不进处置清单，也不占看板计数。
    """
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    data = observations_view.assemble_observations(
        case_dir=ctx.factory.case_dir(case_id), version=version,
        dispositions=_read_dispositions(ctx, case_id),
        skill=skill or None, subject=subject or None,
        disposition=disposition or None,
        page=page, page_size=page_size)
    return ok(data, data_version=version)


@router.get("/cases/{case_id}/observations/{observation_id}")
def observation_detail(case_id: str, observation_id: str,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """观察详情：判据 + 证伪条件 + 事实明细 + 证据引用。

    事实明细分页在前端（默认 50 条）；完整序列由画布下钻承载——
    详情页不铺开上百行。
    """
    _get_owned_case(case_id, p, ctx.cases)
    version = ctx.repo.current_version(case_id)
    data = observations_view.assemble_observation_detail(
        case_dir=ctx.factory.case_dir(case_id), version=version,
        observation_id=observation_id,
        dispositions=_read_dispositions(ctx, case_id))
    if data is None:
        raise APIError(ERR_NOT_FOUND,
                       f"观察不存在：{observation_id}", 404)
    return ok(data, data_version=version)
