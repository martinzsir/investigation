"""
core/gateway.py
OntologyReadGateway —— 语义层唯一读入口（REQ-002）。

架构约束（把"不要自己写业务 SQL"从纪律变为机制）：
  - 只接受 objects.json / links.json 中已声明的名字；中文业务表名
    （银行流水/通话记录/...）一律 UnknownObjectError 硬失败；
  - materialization_state == STALE 时禁止悄悄返回旧值（StaleOntologyError），
    除非显式 allow_stale（调试用，留痕由调用方负责）；
  - 不提供任何自由 SQL 入口（无 query 方法，AC5 以 dir() 断言）；
  - 每次读取经策略链，explain() 可审计：plan/versions/applied_policies。

检测器/图库/MCP 读语义层一律走本网关；写操作唯一入口仍是 ActionExecutor。
"""
from __future__ import annotations

from core.access import AccessContext, system_context
from core.ontology_loader import load_pack
from core.ontology_version import current_version, freshness
from core.policy import PolicyEngine


class UnknownObjectError(KeyError):
    """请求了未在 objects.json/links.json 声明的名字（含中文业务表名）。"""


class StaleOntologyError(RuntimeError):
    """语义层落后于源端（STALE），禁止返回旧值；须先增量重建。"""


# 读策略链（explain 的 applied_policies，审计用）
POLICY_DECLARED_NAMES_ONLY = "declared_names_only"
POLICY_STALE_BLOCK = "stale_block"
POLICY_NO_RAW_SQL = "no_raw_sql"
POLICY_OBJECT_ACCESS = "object_access_policy"
POLICY_PROPERTY_MASK = "property_mask"
APPLIED_POLICIES = (
    POLICY_DECLARED_NAMES_ONLY,
    POLICY_STALE_BLOCK,
    POLICY_NO_RAW_SQL,
    POLICY_OBJECT_ACCESS,
    POLICY_PROPERTY_MASK,
)


class OntologyReadGateway:
    """语义层只读网关。用法：
        gw = OntologyReadGateway(store.conn)
        rows = gw.objects("person")      # 仅声明名
        rows = gw.links("transfers")
        gw.explain()                     # 版本与策略审计
    """

    def __init__(self, conn, pack: str = "default", *,
                 access: AccessContext | None = None,
                 allow_stale: bool = False):
        self._conn = conn
        self._pack = pack
        self._allow_stale = allow_stale
        self._access = access if access is not None else system_context()
        self._policy = PolicyEngine(pack)
        spec = load_pack(pack)
        self._object_names = {o.name for o in spec.objects}
        self._link_names = {l.name for l in spec.links}

    # ---- 状态 ----
    def materialization_state(self) -> str:
        """FRESH | STALE | UNBUILT。"""
        return freshness(self._conn, self._pack).state

    def explain(self) -> dict:
        """返回 plan + versions + policies（AC3 三键齐备）。"""
        ver = current_version(self._conn, self._pack)
        fr = freshness(self._conn, self._pack)
        return {
            "pack": self._pack,
            "plan": {
                "declared_objects": sorted(self._object_names),
                "declared_links": sorted(self._link_names),
            },
            "state": fr.state,
            "ontology_version": ver.ontology_version if ver else None,
            "build_id": ver.build_id if ver else None,
            "built_at": ver.built_at if ver else None,
            "source_watermark": fr.source_watermark,
            "ontology_watermark": fr.ontology_watermark,
            "affected_objects": fr.affected_objects,
            "applied_policies": list(APPLIED_POLICIES),
            "allow_stale": self._allow_stale,
        }

    # ---- 读取 ----
    def objects(self, name: str) -> list[dict]:
        """读取对象类型全量行；name 必须是已声明名，且当前 access 有权
        （对象级策略 fail-closed，属性级敏感列按策略遮蔽）。"""
        if name not in self._object_names:
            raise UnknownObjectError(
                f"未声明的对象类型：{name!r}（语义层只接受 objects.json 声明名，"
                f"可用 {sorted(self._object_names)}；中文业务表请走数据接入，禁止直查）")
        self._policy.check_object(self._access, name)
        self._guard_fresh()
        rows = self._read_table(f"obj_{name}")
        return self._policy.apply_row_masks(self._access, name, rows)

    def links(self, name: str) -> list[dict]:
        """读取链接类型全量行；name 必须是 links.json 已声明名。
        链接沿用对象级策略（link_policies，未声明即拒）。"""
        if name not in self._link_names:
            raise UnknownObjectError(
                f"未声明的链接类型：{name!r}（可用 {sorted(self._link_names)}）")
        self._policy.check_link(self._access, name)
        self._guard_fresh()
        return self._read_table(f"lnk_{name}")

    def count(self, kind: str, name: str) -> int:
        """聚合读取：行数（检测器常用，避免把明细搬进上下文——禁令 2）。"""
        table = self._resolve(kind, name)
        self._policy.check_object(self._access, name)
        self._guard_fresh()
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- 内部 ----
    def _resolve(self, kind: str, name: str) -> str:
        if kind == "object":
            if name not in self._object_names:
                raise UnknownObjectError(f"未声明的对象类型：{name!r}")
            return f"obj_{name}"
        if kind == "link":
            if name not in self._link_names:
                raise UnknownObjectError(f"未声明的链接类型：{name!r}")
            return f"lnk_{name}"
        raise ValueError(f"kind 必须是 object|link，收到 {kind!r}")

    def _guard_fresh(self) -> None:
        state = self.materialization_state()
        if state == "STALE" and not self._allow_stale:
            raise StaleOntologyError(
                "语义层已落后于源端（STALE）：禁止读取旧值。"
                "请先跑增量重建（scripts.incremental / rebuild_from_partition），"
                "或显式构造 OntologyReadGateway(..., allow_stale=True) 并留痕。")

    def _read_table(self, table: str) -> list[dict]:
        cur = self._conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
