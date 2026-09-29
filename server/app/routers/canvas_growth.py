"""研判结果层端点（CAN-19）。

GET /cases/{cid}/canvas/lens-results

为什么需要它
------------
案件级画布**尚无服务端持久化**（节点只在前端画布文档里）。而定向深挖的
观察是**案件级归档**的（artifacts/directed_observations.json，不挂版本）。
本端点把后者还原成前者的元素——结论节点、挂接边、假设节点与推断边——
前端拿到后并入画布文档即可。

这比"跑完镜头由前端自己拼节点"好在哪：拼装口径（精度聚合、假设反查、
分组判重）留在服务端，前端换皮或换画布实现都不会出现第二套口径。

node_id 可选
------------
不传 = 重建整层（画布加载时用，按观察自带 origin.node_id 归组）；
传了 = 只取挂在该节点下的那几组（跑完一次镜头后增量并入时用）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from server.app import canvas_growth
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.store.state_store import StateStore

router = APIRouter(tags=["canvas-growth"])


def _target_labels(ctx: WebContext, case_id: str) -> dict[str, str]:
    """从案件画布持久文档提取 subject 节点 ID→label 映射。

    用于跳过共现人物中的靶心本人（§2/§4），避免同一个人产出两个节点。
    """
    try:
        state = StateStore(case_id,
                           ctx.factory.case_dir(case_id) / "state.sqlite")
        row = state.get_case_canvas(case_id)
        doc = row.get("doc") if isinstance(row, dict) else None
        if not isinstance(doc, dict):
            return {}
        labels: dict[str, str] = {}
        for n in (doc.get("nodes") or []):
            if isinstance(n, dict) and n.get("kind") == "subject":
                nid = str(n.get("id") or "")
                lbl = str(n.get("label") or "")
                if nid and lbl:
                    labels[nid] = lbl
        return labels
    except Exception:
        return {}


@router.get("/cases/{case_id}/canvas/lens-results")
def lens_results(case_id: str,
                 node_id: str | None = Query(
                     None, description="只取挂在该靶心节点下的结论组"),
                 clue_id: str | None = Query(
                     None, description="线索域：只返回该线索发起的观察；"
                                       "不传=案件级全量"),
                 pack: str = Query("default"),
                 p: Principal = Depends(get_principal),
                 ctx: WebContext = Depends(get_ctx)):
    """定向深挖的观察 → 挂到靶心节点下的研判结论层。"""
    _get_owned_case(case_id, p, ctx.cases)
    data = canvas_growth.load_growth_layer(
        case_dir=ctx.factory.case_dir(case_id), case_id=case_id,
        target_node_id=(str(node_id).strip() if node_id else None),
        pack=pack,
        base_dir=ctx.cases.snapshot_ontology_root(case_id),
        target_labels=_target_labels(ctx, case_id),
        clue_id=(str(clue_id).strip() if clue_id else None),
    )
    return ok(data)
