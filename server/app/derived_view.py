"""
server/app/derived_view.py
R8 派生属性读取面（REQ-R8 / REQ-028）。

- 声明来源：ontology/<pack>/derived_properties.json（经 loader 校验 AC3/AC5/cache_policy）；
- 注册表（core.derived）是进程级全局：首次查询幂等注册，已注册同名属性不重复注册；
- 五出口纪律：目标对象 + 绑定 Function 的输入对象/链接全部过 PolicyEngine
  （access=None 时 system 旁路，仅测试/本机）；
- 不在线索详情内膨胀：由独立端点按对象/属性按需取数。
"""
from __future__ import annotations

from typing import Any

from core import derived as derived_reg
from core.ontology_loader import load_derived_properties, load_pack
from core.policy import PolicyEngine
from server.app.ontology_meta import resolve_base


class DerivedNotFound(LookupError):
    """对象类型 / 对象实例 / 派生属性未声明或不存在（路由统一 404）。"""


class _StoreAdapter:
    """把 web CaseStore 适配成 core 侧约定（.query + .conn 版本锚点）。"""

    def __init__(self, case_store: Any) -> None:
        self._s = case_store
        self.conn = case_store.read_conn

    def query(self, sql: str, params: tuple | list = (), **kwargs) -> list[dict]:
        return self._s.query(sql, params, **kwargs)


def _ensure_registered(pack: str, base) -> list[dict]:
    decls = load_derived_properties(pack, base)
    for d in decls:
        if derived_reg.get(d["name"]) is None:
            # register_from_decl 内部再过一次 AC3/AC5/cache_policy 校验
            derived_reg.register_from_decl([d], pack=pack)
    return decls


def assemble_derived(*, store: Any, pack: str, base_dir: str | None,
                     obj_type: str, obj_id: str, prop: str,
                     access: Any = None) -> dict:
    """组装单个派生属性响应。

    返回体含 cache=hit/miss 调试字段；未声明/对象不存在抛 DerivedNotFound。
    """
    base = resolve_base(pack, base_dir)
    decls = _ensure_registered(pack, base)
    full = f"{obj_type}.{prop}"
    decl = next((d for d in decls if d["name"] == full), None)
    if decl is None:
        raise DerivedNotFound(f"派生属性未声明：{full}")

    pack_obj = load_pack(pack, base)
    obj_spec = next((o for o in pack_obj.objects if o.name == obj_type), None)
    if obj_spec is None:
        raise DerivedNotFound(f"对象类型未声明：{obj_type}")

    # 权限：目标对象 + Function 输入对象/链接（缓存命中也先鉴权，再取值）
    pe = PolicyEngine(pack)
    if access is not None:
        pe.check_object(access, obj_type)
        fn_spec = pack_obj.functions.get(decl["function"])
        if fn_spec is None:
            raise DerivedNotFound(
                f"绑定 Function 未声明：{decl['function']}")
        for tbl in fn_spec.inputs:
            if tbl.startswith("obj_"):
                pe.check_object(access, tbl[4:])
            elif tbl.startswith("lnk_"):
                pe.check_link(access, tbl[4:])

    # 对象实例解析：entity 按 name_property 值定位代理键；列名均为声明内受控标识符。
    # 语义表未物化（数据源未接入）视同对象不存在 → 404，不 500。
    name_col = obj_spec.name_property or obj_spec.pk
    adapter = _StoreAdapter(store)
    try:
        rows = adapter.query(
            f'SELECT "{obj_spec.pk}" AS pk FROM "obj_{obj_type}" '
            f'WHERE "{name_col}" = ? LIMIT 1',
            [obj_id])
    except Exception as e:  # duckdb.CatalogException：obj_* 表缺失
        if e.__class__.__name__ in ("CatalogException", "BinderException"):
            raise DerivedNotFound(
                f"对象语义表未物化或对象不存在：{obj_type}:{obj_id}") from e
        raise
    if not rows:
        raise DerivedNotFound(f"对象不存在：{obj_type}:{obj_id}")
    obj_pk = rows[0]["pk"]

    result = derived_reg.compute(
        adapter, obj_type, prop, obj_pks=[obj_pk],
        pack=pack, base_dir=base, access=access)

    return {
        "available": True,
        "object_type": obj_type,
        "object_id": obj_id,
        "object_pk": obj_pk,
        "property": prop,
        "function": decl["function"],
        "cache_policy": decl["cache_policy"],
        "value": result.get("value"),
        "computed_at": result.get("computed_at"),
        "source_version_set": result.get("source_version_set"),
        "params_hash": result.get("params_hash"),
        "cache": result.get("cache"),
    }
