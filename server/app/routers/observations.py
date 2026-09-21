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
from server.app.deps import WebContext, get_ctx, get_principal, task_dto
from server.app.envelope import ERR_NOT_FOUND, ERR_VALIDATION, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.worker.tasks import (
    TASK_OBS_PROMOTE,
    TASK_OBS_DISPOSITION,
    enqueue_task,
)

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


# ----------------------------------------------------------------------
# 提升为线索（人工认领，强制指定假设）
# ----------------------------------------------------------------------
class PromoteIn(BaseModel):
    """提升入参。

    hypothesis **必填**：线索是待证明的命题，不指定假设就无法处置
    （没有证伪目标）。这是提升动作的核心门槛，不是可选项。
    """
    hypothesis: str = ""
    note: str = ""
    parent_clue_id: str = ""   # 可选：挂到某条主线索下（该线索的证据支撑）
    idem_key: str = ""


@router.post("/cases/{case_id}/observations/{observation_id}/promote",
             status_code=202)
def promote_observation(case_id: str, observation_id: str, body: PromoteIn,
                        p: Principal = Depends(get_principal),
                        ctx: WebContext = Depends(get_ctx)):
    """把观察提升为线索 → 202 + task_id。

    提升 = 正兵断言"这批观察构成疑点，我要验证的是某个假设"。
    那一刻观察才成为命题（assumption_chain 非空），才进处置流程。

    hypothesis 未填 → 400（不派任务，省一次往返）：不指定假设的线索
    仍然无法处置，等于没解决问题。
    """
    _get_owned_case(case_id, p, ctx.cases)
    hypothesis = (body.hypothesis or "").strip()
    if not hypothesis:
        raise APIError(ERR_VALIDATION,
                       "提升为线索必须指定待验证的假设（hypothesis）", 400)
    params = {
        "observation_id": observation_id,
        "hypothesis": hypothesis,
        "note": (body.note or "").strip(),
        "parent_clue_id": (body.parent_clue_id or "").strip(),
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    idem = (body.idem_key or "").strip() or \
        f"obs-promote:{observation_id}:{hypothesis}"
    task = enqueue_task(ctx.repo, case_id=case_id,
                        task_type=TASK_OBS_PROMOTE, params=params,
                        idem_key=idem, created_by=p.operator)
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))


class DispositionIn(BaseModel):
    """观察处置入参（认领/归档/取消认领）。

    注意：**提升不走这里**——提升必须带假设且产线索，走 /promote。
    """
    disposition: str = ""
    note: str = ""


@router.post("/cases/{case_id}/observations/{observation_id}/disposition",
             status_code=202)
def set_disposition(case_id: str, observation_id: str, body: DispositionIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """认领/归档观察 → 202 + task_id。

    处置态落 state.sqlite、跨版本持久：正兵"看过/归过了"不能因为重扫
    就重置。提升为线索是另一个动作（/promote），必须带假设。
    """
    _get_owned_case(case_id, p, ctx.cases)
    disp = (body.disposition or "").strip()
    if disp not in ("已认领", "已归档", "未认领"):
        raise APIError(
            ERR_VALIDATION,
            f"非法处置态：{disp!r}（允许 已认领/已归档/未认领；"
            f"提升为线索请用 /promote）", 400)
    params = {
        "observation_id": observation_id,
        "disposition": disp,
        "note": (body.note or "").strip(),
        "operator": p.operator,
        "role": p.role,
    }
    idem = f"obs-disp:{observation_id}:{disp}"
    task = enqueue_task(ctx.repo, case_id=case_id,
                        task_type=TASK_OBS_DISPOSITION, params=params,
                        idem_key=idem, created_by=p.operator)
    return ok(task_dto(task), data_version=ctx.repo.current_version(case_id))


@router.get("/cases/{case_id}/observations/hypothesis-choices")
def hypothesis_choices(case_id: str,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """提升时的假设下拉选项（本体已声明，换本体自动跟随）。

    不硬编码 H1..Hn：假设是本体声明的领域知识，换领域即换集合。
    """
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    from server.app import observation_promote as promote_mod
    choices = promote_mod.hypothesis_choices(
        pack=case.pack_id,
        base_dir=ctx.cases.snapshot_ontology_root(case_id))
    return ok({"hypotheses": choices},
              data_version=ctx.repo.current_version(case_id))
