"""
server/app/routers/proposals.py
REQ-V-014 Agent 提案接入：提案审批 → 核查任务的单向桥接。

提案由 Agent 经 MCP review.submit_proposal 提交（唯一写通道，永不自动生效，
pp- 前缀，落全局提案库 proposal 表）；本路由是审批侧唯一入口：

POST /cases/{cid}/proposals/{pid}/decide 🔒 —— approve/reject + 理由：
  - approve：ProposalStore.decide（core 状态机 draft→approved + 全局审计链）
    后，按 kind 桥接生成 TASK_VERIFY 任务——operator/role/clearance 取
    审批人会话快照（operator=审批人，绝不透传 agent:*）；
  - reject：仅落审批态，不产生任何任务；
  - 提案不存在/跨案件一律 404（不泄露存在性）；重复 decide 409。

红线不回退：action.status 的 pp- 前缀只读路由保持 ProposalStore.get() 查询；
agent 会话（operator 以 agent: 开头）不得审批——审批是人的判断。
"""
from __future__ import annotations

import duckdb
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.proposal import ProposalStore, ProposalValidationError

from server.app.deps import WebContext, get_ctx, get_principal, task_dto
from server.app.envelope import (
    ERR_CONFLICT,
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.backend import open_local_conn
from server.app.worker.tasks import TASK_VERIFY, enqueue_task

router = APIRouter(tags=["proposals"])


class ProposalDecisionIn(BaseModel):
    decision: str  # approve / reject
    reason: str | None = None


def _open_proposals(ctx: WebContext) -> tuple[ProposalStore, duckdb]:
    """打开全局提案库（与 MCP 提交侧 core 层提案库同库）。"""
    conn = open_local_conn(ctx.proposals_db)
    return ProposalStore(conn), conn


def _verify_task_params(kind: str, candidate: dict, p: Principal,
                        rec_input: dict | None = None) -> dict:
    """提案候选 → TASK_VERIFY 任务参数（对齐 clues.py 人工通道同款形状）。

    operator 强制取审批人快照：agent:* 只出现在提案 author/proposed_by，
    绝不进任务 operator（REQ-V-014 改动点 3）。
    """
    params: dict = {
        "operator": p.operator,
        "role": p.role,
        "clearance": p.clearance,
    }
    if kind == "verify_item":
        params.update({
            "op": "add_manual",
            "clue_id": str(candidate.get("clue_id") or "").strip(),
            "text": str(candidate.get("text") or "").strip(),
        })
        # REQ-V-019：AI 核查方向草案提案（input.origin='ai_draft'）审批通过
        # 时，把 LLM 候选的结构化路由字段带入任务（channel 白名单与 function
        # 名在草案侧已守门；此处透传，Worker 落库时复核 origin 枚举）。
        # 人工/普通提案不携带 → Worker 落 origin='manual'，行为不变。
        inp = rec_input if rec_input is not None else {}
        if str(inp.get("origin") or "") == "ai_draft":
            params["origin"] = "ai_draft"
            params["channel"] = str(inp.get("channel") or "")
            params["ref_function"] = str(inp.get("ref_function") or "")
            if isinstance(inp.get("external"), dict):
                params["external"] = inp["external"]
            fals = str(inp.get("falsification") or "")
            if fals:
                params["falsification"] = fals
        return params
    # verify_request → op=add_request（调取登记；AC8 已保证必填形状）
    params.update({
        "op": "add_request",
        "clue_id": str(candidate.get("clue_id") or "").strip(),
        "target": str(candidate.get("target") or "").strip(),
        "material": str(candidate.get("material") or "").strip(),
        "legal_instrument": str(candidate.get("legal_instrument") or "").strip(),
        "handler": str(candidate.get("handler") or "").strip(),
        "due_date": str(candidate.get("due_date") or "").strip(),
        "note": str(candidate.get("note") or "").strip(),
    })
    item_id = str(candidate.get("item_id") or "").strip()
    if item_id:
        params["item_id"] = item_id
    return params


@router.post("/cases/{case_id}/proposals/{proposal_id}/decide")
def decide_proposal(case_id: str, proposal_id: str,
                    body: ProposalDecisionIn,
                    p: Principal = Depends(get_principal),
                    ctx: WebContext = Depends(get_ctx)):
    """审批提案：approve 桥接 TASK_VERIFY（operator=审批人）；reject 无任务。"""
    _get_owned_case(case_id, p, ctx.cases)
    if p.operator.startswith("agent:"):
        raise APIError(
            ERR_FORBIDDEN,
            f"Agent 身份 {p.operator!r} 不得审批提案"
            "（审批是人的判断，REQ-V-014 红线）", 403)
    decision = (body.decision or "").strip()
    if decision not in ("approve", "reject"):
        raise APIError(ERR_VALIDATION,
                       "decision 仅可选 approve/reject", 400)
    reason = (body.reason or "").strip()
    if decision == "reject" and not reason:
        raise APIError(ERR_VALIDATION, "驳回必须给出理由", 400)

    ps, conn = _open_proposals(ctx)
    try:
        rec = ps.get(proposal_id)
        if rec is None or rec["case_id"] != case_id:
            raise APIError(ERR_NOT_FOUND,
                           f"提案不存在：{proposal_id}", 404)
        # image_draft approve 必须经专用人验端点（写 image_evidence），
        # 通用桥接会把图像草案误当核查任务；reject 仍允许（人直接驳回）。
        if decision == "approve" and rec["kind"] == "image_draft":
            raise APIError(
                ERR_VALIDATION,
                "image_draft 核验请走 "
                f"/cases/{case_id}/vlm/drafts/{proposal_id}/verify"
                "（人比对原件后入图入报告）", 400)
        try:
            rec = ps.decide(proposal_id, decision, operator=p.operator,
                            reason=reason)
        except ProposalValidationError as e:
            # 重复 decide / 已过期（状态机单向）→ 409
            raise APIError(ERR_CONFLICT, str(e), 409)
        except PermissionError as e:
            raise APIError(ERR_FORBIDDEN, str(e), 403)

        task = None
        if decision == "approve":
            params = _verify_task_params(
                rec["kind"], rec["payload"].get("candidate") or {}, p,
                rec_input=rec["payload"].get("input"))
            task = enqueue_task(
                ctx.repo, case_id=case_id, task_type=TASK_VERIFY,
                params=params,
                idem_key=f"verify-proposal:{proposal_id}:{rec['kind']}",
                created_by=p.operator)
        return ok({
            "proposal_id": rec["proposal_id"],
            "kind": rec["kind"],
            "status": rec["status"],
            "decided_by": rec["decided_by"],
            "decided_at": rec["decided_at"],
            "decision_reason": rec["decision_reason"],
            "task": task_dto(task) if task is not None else None,
        }, data_version=ctx.repo.current_version(case_id))
    finally:
        conn.close()
