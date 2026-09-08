"""
server/app/routers/disposal.py
W-P-001 五泳道处置看板（只读）。

- 跨租户案件一律 404；未产出线索返回 available:false 空泳道（不 500）；
- 状态真值读 state.sqlite（不存在则缺省待查）；
- 卡片状态迁移走已有 POST /clues/{clue_id}/actions（本端点不写）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from server.app import disposal_board
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["disposal"])


def _state_map(ctx: WebContext, case_id: str) -> dict[str, dict]:
    path = ctx.factory.case_dir(case_id) / "state.sqlite"
    if not path.exists():
        return {}
    from server.app.store.state_store import StateStore
    st = StateStore(case_id, path)
    try:
        return st.status_map()
    finally:
        st.close()


@router.get("/cases/{case_id}/disposal/board")
def get_disposal_board(case_id: str,
                       p: Principal = Depends(get_principal),
                       ctx: WebContext = Depends(get_ctx)):
    """五泳道看板：卡片/计数/stay_days/overdue（内间线索按秩级过滤）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    data = disposal_board.assemble_board(
        case_dir=ctx.factory.case_dir(case_id),
        state_map=_state_map(ctx, case_id),
        role=p.role,
        snapshot_base=ctx.cases.snapshot_ontology_root(case_id),
        pack=case.pack_id)
    return ok(data, data_version=ctx.repo.current_version(case_id))
