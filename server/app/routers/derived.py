"""
server/app/routers/derived.py
R8 派生属性端点（REQ-R8）：

    GET /cases/{cid}/objects/{obj_type}/{obj_id}/derived/{prop}

只读、按需、查询时计算；不塞进线索详情避免膨胀。
未 BUILD（语义层缺失）返回 available:false，不 500。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.policy import PolicyDeniedError
from server.app import derived_view
from server.app.deps import WebContext, access_for, get_ctx, get_principal
from server.app.envelope import ERR_FORBIDDEN, ERR_NOT_FOUND, APIError, ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["derived"])


@router.get("/cases/{case_id}/objects/{obj_type}/{obj_id}/derived/{prop}")
def get_derived_property(case_id: str, obj_type: str, obj_id: str,
                         prop: str,
                         p: Principal = Depends(get_principal),
                         ctx: WebContext = Depends(get_ctx)):
    """单对象单派生属性（声明驱动；响应带 cache=hit/miss 调试字段）。"""
    _get_owned_case(case_id, p, ctx.cases)
    case = ctx.repo.get_case(case_id)
    access = access_for(p, case_id=case_id, purpose="派生属性查询")
    store = None
    try:
        # for_case 本身不开库（read_conn 惰性打开），FileNotFoundError 可能在取数时才抛
        store = ctx.factory.for_case(case_id, mode="read")
        try:
            data = derived_view.assemble_derived(
                store=store, pack=case.pack_id,
                base_dir=ctx.cases.snapshot_ontology_root(case_id),
                obj_type=obj_type, obj_id=obj_id, prop=prop,
                access=access)
        except FileNotFoundError:
            data = {"available": False,
                    "object_type": obj_type, "object_id": obj_id,
                    "property": prop,
                    "note": "语义层未构建（先导入数据并 BUILD）"}
        except derived_view.DerivedNotFound as e:
            raise APIError(ERR_NOT_FOUND, str(e), 404)
        except PolicyDeniedError as e:
            raise APIError(ERR_FORBIDDEN, f"无权访问该派生属性：{e}", 403)
    finally:
        if store is not None:
            store.close()
    return ok(data, data_version=ctx.repo.current_version(case_id))
