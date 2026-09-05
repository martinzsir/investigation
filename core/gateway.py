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

REQ-046 Object Views（按角色投影）：
  - 视图 v_<name> 物化为 DuckDB VIEW，SELECT 只引用 obj_<base> 的列子集；
  - 读视图唯一入口是本网关 view(name) 方法——直查 v_* 不在 Store.query 安全名单
    内，等同绕过网关的违规行为（AC4）；
  - 视图读时仍经 PolicyEngine.check_object + apply_row_masks（AC2：权限不复制）；
  - 视图按角色授权：roles 名单外被拒（AC1）。
"""
from __future__ import annotations

from core.access import AccessContext, system_context
from core.ontology_loader import load_pack
from core.ontology_version import current_version, freshness
from core.policy import PolicyEngine
from core.views import (
    ViewAccessDenied, ViewNotFoundError, all_views,
)


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
        self._spec = spec
        self._object_names = {o.name for o in spec.objects}
        self._link_names = {l.name for l in spec.links}
        # REQ-046：合并显式声明视图 + 标准视图（标准视图不覆盖显式声明）
        self._views = all_views(pack)

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
                "declared_views": sorted(self._views),
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

    def view(self, name: str) -> list[dict]:
        """REQ-046 AC1/AC2/AC4：按角色读取视图投影。

        - 视图必须已声明（load_views + build_standard_views）——未声明 raise
          ViewNotFoundError；
        - 角色必须在 view.roles 内（AC1：按角色投影；system 旁路）；
        - 视图读经 base_object 的对象级策略（AC2：权限不复制，读时执行）；
        - 视图读唯一入口，不绕过 REQ-002 网关（AC4：v_* 不在 Store.query
          安全名单内，直查 v_* 视同违规）；
        - 属性级遮蔽仍按 base_object 执行（如 tipoff 视图读到 content_raw
          仍会被 mask）。
        """
        if name not in self._views:
            raise ViewNotFoundError(
                f"未声明的视图：{name!r}（可用 {sorted(self._views)}）")
        view = self._views[name]
        # 角色授权（system 旁路）
        if not self._access.is_system and self._access.role not in view.roles:
            raise ViewAccessDenied(
                f"角色 {self._access.role!r} 无权访问视图 {name!r}"
                f"（需 roles={sorted(view.roles)}）"
                f"——operator={self._access.operator}")
        # AC2：权限仍由 base_object 策略执行（不复制到视图）
        self._policy.check_object(self._access, view.base_object)
        self._guard_fresh()
        rows = self._read_table(f"v_{name}")
        # 属性级遮蔽仍按 base_object 执行（apply_row_masks 只遮蔽 base_object
        # 已声明的敏感属性——视图投影列子集时不影响其他列）
        return self._policy.apply_row_masks(self._access, view.base_object, rows)

    def view_spec(self, name: str):
        """返回视图声明（不含数据；调试/审计用）。"""
        if name not in self._views:
            raise ViewNotFoundError(
                f"未声明的视图：{name!r}（可用 {sorted(self._views)}）")
        return self._views[name]

    def list_views(self) -> list[str]:
        """列出所有可用视图名（显式声明 + 标准）。"""
        return sorted(self._views)

    def count(self, kind: str, name: str) -> int:
        """聚合读取：行数（检测器常用，避免把明细搬进上下文——禁令 2）。"""
        table = self._resolve(kind, name)
        self._policy.check_object(self._access, name)
        self._guard_fresh()
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- REQ-P M3 画像方法（聚合/元数据读取，不取回全量——禁令 2） ----
    def materialized_objects(self) -> list[str]:
        """已物化对象：declared ∩ information_schema 实表。

        纯 schema 内省（非数据读取），不做 STALE 拦截——对象已物化与否
        不随源端推进变化。"""
        self._check_declared_pack()
        rows = self._conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'obj_%'").fetchall()
        tables = {r[0] for r in rows}
        return sorted(n for n in self._object_names if f"obj_{n}" in tables)

    def materialized_props(self, props: list[str] | None = None) -> dict[str, bool]:
        """属性级列存在性：{"obj.prop": bool}，不按值类型过滤（画像缺陷 3 正解——
        decimal/date 属性同样被覆盖）。

        缺列 ≠ 未物化：对象已物化但表中无此列 → False（schema 演进场景，
        画像缺陷 4 的判据来源；M4 profiler 据此报 profile_missing_column）。
        props 传 "obj.prop" 键列表过滤；未声明键硬失败。
        """
        self._check_declared_pack()
        want = self._validate_prop_keys(props) if props is not None else None
        cols_by_table: dict[str, set[str]] = {}
        for t, c in self._conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name LIKE 'obj_%'").fetchall():
            cols_by_table.setdefault(t, set()).add(c)
        out: dict[str, bool] = {}
        for o in self._spec.objects:
            for p in o.properties:
                key = f"{o.name}.{p}"
                if want is not None and key not in want:
                    continue
                cols = cols_by_table.get(f"obj_{o.name}")
                out[key] = bool(cols) and p in cols
        return out

    def value_profile(self, obj: str, prop: str) -> dict:
        """单 SQL 聚合画像：行数/非空/空值率/基数/MIN/MAX/样例（≤5 行）。

        敏感属性（policies.json 声明无权）样例与 MIN/MAX 按策略遮蔽。
        未声明对象/属性、对象未物化、列缺失 → 明确报错（fail-loud，
        调用方先用 materialized_props 判定，防画像缺陷 4 的 BinderException）。
        """
        table = self._require_prop(obj, prop)
        self._policy.check_object(self._access, obj)
        self._guard_fresh()
        row = self._conn.execute(
            f'SELECT COUNT(*), COUNT("{prop}"), COUNT(DISTINCT "{prop}"), '
            f'MIN("{prop}"), MAX("{prop}") FROM {table}').fetchone()
        samples = [r[0] for r in self._conn.execute(
            f'SELECT DISTINCT "{prop}" FROM {table} '
            f'WHERE "{prop}" IS NOT NULL LIMIT 5').fetchall()]
        total, non_null, distinct, lo, hi = row
        readable = self._policy.can_read_property(self._access, obj, prop)
        if not readable:
            mask = lambda v: self._policy.mask_value(self._access, obj, prop, v)
            samples = [mask(v) for v in samples]
            lo, hi = mask(lo), mask(hi)
        return {
            "obj": obj, "prop": prop,
            "row_count": total, "non_null": non_null,
            "null_rate": (total - non_null) / total if total else 0.0,
            "distinct": distinct, "min": lo, "max": hi,
            "samples": samples,
        }

    def value_overlap(self, obj_a: str, prop_a: str,
                      obj_b: str, prop_b: str) -> dict:
        """精确交集/包含率（INTERSECT 子查询，不采样）——画像 → 探测的依据。"""
        ta = self._require_prop(obj_a, prop_a)
        tb = self._require_prop(obj_b, prop_b)
        self._policy.check_object(self._access, obj_a)
        self._policy.check_object(self._access, obj_b)
        self._guard_fresh()
        da = self._conn.execute(
            f'SELECT COUNT(DISTINCT "{prop_a}") FROM {ta} '
            f'WHERE "{prop_a}" IS NOT NULL').fetchone()[0]
        db_ = self._conn.execute(
            f'SELECT COUNT(DISTINCT "{prop_b}") FROM {tb} '
            f'WHERE "{prop_b}" IS NOT NULL').fetchone()[0]
        inter = self._conn.execute(
            f'SELECT COUNT(*) FROM ('
            f'SELECT DISTINCT "{prop_a}" AS v FROM {ta} WHERE "{prop_a}" IS NOT NULL '
            f'INTERSECT '
            f'SELECT DISTINCT "{prop_b}" AS v FROM {tb} WHERE "{prop_b}" IS NOT NULL)'
        ).fetchone()[0]
        return {
            "a": f"{obj_a}.{prop_a}", "b": f"{obj_b}.{prop_b}",
            "distinct_a": da, "distinct_b": db_,
            "intersection": inter,
            "a_in_b_ratio": round(inter / da, 4) if da else 0.0,
            "b_in_a_ratio": round(inter / db_, 4) if db_ else 0.0,
        }

    def distinct_values(self, obj: str, prop: str, limit: int = 1000) -> list:
        """去重值清单（变体探测/候选关联用）；敏感属性按策略遮蔽。"""
        table = self._require_prop(obj, prop)
        self._policy.check_object(self._access, obj)
        self._guard_fresh()
        vals = [r[0] for r in self._conn.execute(
            f'SELECT DISTINCT "{prop}" FROM {table} '
            f'WHERE "{prop}" IS NOT NULL LIMIT ?', [int(limit)]).fetchall()]
        if not self._policy.can_read_property(self._access, obj, prop):
            vals = [self._policy.mask_value(self._access, obj, prop, v)
                    for v in vals]
        return vals

    def _check_declared_pack(self) -> None:
        """画像元数据读取同样只认声明名集合（pack 一致性由构造保证）。"""
        if not self._object_names:
            raise UnknownObjectError("案件包未声明任何对象类型")

    def _validate_prop_keys(self, props: list[str]) -> set[str]:
        """过滤键必须是已声明的 "obj.prop"；未声明硬失败。"""
        declared = {f"{o.name}.{p}"
                    for o in self._spec.objects for p in o.properties}
        unknown = [k for k in props if k not in declared]
        if unknown:
            raise ValueError(
                f"未声明的属性键：{sorted(unknown)}（可用声明键见 objects.json）")
        return set(props)

    def _require_prop(self, obj: str, prop: str) -> str:
        """画像取数前置：声明对象 + 声明属性 + 表/列存在性（缺陷 4 fail-loud）。"""
        if obj not in self._object_names:
            raise UnknownObjectError(f"未声明的对象类型：{obj!r}")
        o = next(o for o in self._spec.objects if o.name == obj)
        if prop not in o.properties:
            raise ValueError(
                f"对象 {obj} 未声明属性 {prop!r}"
                f"（画像只接受 objects.json 声明属性：{sorted(o.properties)}）")
        table = f"obj_{obj}"
        exists = self._conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table]).fetchone()[0]
        if not exists:
            raise ValueError(
                f"对象 {obj} 未物化（{table} 不存在）——"
                "请先构建语义层，或用 materialized_objects() 判定后再画像")
        cols = {r[0] for r in self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", [table]).fetchall()}
        if prop not in cols:
            raise ValueError(
                f"对象 {obj} 已物化，但表中无此列：{prop!r}"
                "（schema 演进——请重建语义层；调用方应先用 materialized_props 判定）")
        return table

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
