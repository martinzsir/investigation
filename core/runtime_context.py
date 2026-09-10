"""
core/runtime_context.py
RuntimeContext：py Function 运行时上下文 + ReadOnlyStore 只读护栏。

解决问题：
  - core/functions.py invoke() 只传 store/params，py 函数拿不到 pack/access/policy/health
  - store 可写（store.execute 可执行 DDL），py 路径无只读护栏
  - py 函数硬编码 obj_*/lnk_* 表名，换包直接崩

设计：
  - ReadOnlyStore.__getattr__ 屏蔽 execute/conn；query() 内部调 _assert_readonly
  - RuntimeContext.table(name)/link(name) 从 ontology 声明派生表名，未声明抛 ValueError
  - RuntimeContext.check(table) 显式权限校验（复用 policy.check_object/check_link）
  - RuntimeContext.derived(obj_type, prop, **params) 调 core.derived.compute()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 禁止 py 函数直接访问的 store 属性（写操作/连接泄漏）
_FORBIDDEN_STORE_ATTRS = frozenset({"execute", "conn", "_conn", "l1"})


class ReadOnlyStore:
    """Store 的只读代理：屏蔽 execute/conn，query 强制只读白名单。

    py 函数通过此代理访问数据，与 SQL 函数共享 _assert_readonly 护栏。
    """

    def __init__(self, store):
        object.__setattr__(self, "_store", store)

    def query(self, sql: str, params: tuple = (), **kwargs) -> list[dict]:
        # 复用 SQL 路径的只读白名单校验（同一决策点）
        from core.functions import _assert_readonly
        _assert_readonly(sql, "py-function")
        return self._store.query(sql, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name in _FORBIDDEN_STORE_ATTRS:
            raise AttributeError(
                f"py 函数禁止访问 store.{name}（只读护栏；如需写操作走 ActionExecutor）")
        return getattr(self._store, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_store":
            object.__setattr__(self, name, value)
            return
        # 禁止 py 函数向 store 写属性
        raise AttributeError(
            f"py 函数禁止向 store 写属性 '{name}'（只读护栏）")


@dataclass
class RuntimeContext:
    """py Function 运行时上下文：提供 pack 元数据/只读 store/权限/策略/健康度。

    用法（py 函数新签名）：
        def my_fn(store, params, ctx):
            tbl = ctx.table("call")            # → "obj_call"
            rows = store.query(f"SELECT * FROM {tbl}")
            ...
    """
    store: ReadOnlyStore
    pack: str
    access: Any = None          # AccessContext（None → system 旁路）
    policy: Any = None          # PolicyEngine
    health: Any = None          # RunHealth
    base_dir: Any = None        # 案件快照基目录
    _objects: dict = field(default_factory=dict, repr=False)   # 声明的对象名集合
    _links: dict = field(default_factory=dict, repr=False)     # 声明的链接名集合

    def _ensure_meta(self) -> None:
        """惰性加载 ontology 声明（只加载一次）。"""
        if self._objects:
            return
        from core.ontology_loader import load_pack
        pack_obj = load_pack(self.pack, base_dir=self.base_dir)
        self._objects = {o.name: o for o in pack_obj.objects}
        self._links = {l.name: l for l in pack_obj.links}

    def table(self, obj_name: str) -> str:
        """从 ontology 声明派生对象表名 obj_{name}；未声明抛 ValueError。"""
        self._ensure_meta()
        if obj_name not in self._objects:
            raise ValueError(
                f"RuntimeContext.table: 对象 '{obj_name}' 未在 ontology 声明，"
                f"可用 {sorted(self._objects)}")
        return f"obj_{obj_name}"

    def link(self, link_name: str) -> str:
        """从 ontology 声明派生链接表名 lnk_{name}；未声明抛 ValueError。"""
        self._ensure_meta()
        if link_name not in self._links:
            raise ValueError(
                f"RuntimeContext.link: 链接 '{link_name}' 未在 ontology 声明，"
                f"可用 {sorted(self._links)}")
        return f"lnk_{link_name}"

    def check(self, table: str) -> None:
        """显式权限校验：obj_* 走 check_object，lnk_* 走 check_link。"""
        if self.access is None or self.policy is None:
            return  # system 旁路
        if table.startswith("obj_"):
            self.policy.check_object(self.access, table[4:])
        elif table.startswith("lnk_"):
            self.policy.check_link(self.access, table[4:])

    def declared_objects(self) -> list[str]:
        """返回已声明的对象名列表。"""
        self._ensure_meta()
        return list(self._objects.keys())

    def declared_links(self) -> list[str]:
        """返回已声明的链接名列表。"""
        self._ensure_meta()
        return list(self._links.keys())

    def derived(self, obj_type: str, prop: str, obj_pks=None,
                params: dict | None = None) -> dict:
        """查询时派生属性计算（接入 core.derived.compute）。"""
        from core.derived import compute
        return compute(self._store, obj_type, prop, obj_pks=obj_pks,
                       params=params, pack=self.pack, health=self.health,
                       base_dir=self.base_dir, access=self.access)
