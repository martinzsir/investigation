"""
server/app/routers/ontology_config.py
W-P-018 Ontology 配置下发（六项解耦共性基建）。

GET /cases/{cid}/ontology-config —— 一份下发兵法五间/侦查五维/交叉等级/
状态机/动作/计分声明，前端全局消费，不再硬编码领域词汇。
只读声明文件，不查 DuckDB；未 BUILD 案件也可取（配置随案件快照目录存在）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from server.app import ontology_config_view
from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["ontology-config"])


@router.get("/cases/{case_id}/ontology-config")
def get_ontology_config(case_id: str,
                        p: Principal = Depends(get_principal),
                        ctx: WebContext = Depends(get_ctx)):
    """案件包 ontology 声明（配置下发；名称全量返回，命中数据仍按角色过滤）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    data = ontology_config_view.assemble_ontology_config(
        pack=case.pack_id,
        base_dir=ctx.cases.snapshot_ontology_root(case_id))
    return ok(data, data_version=ctx.repo.current_version(case_id))
