"""
core/views.py
REQ-046 Object Views（按角色投影）。

设计：
  - View 声明在 ontology/<pack>/views.json：name / base_object / properties / roles / description；
  - ViewMaterializer 把声明编译为 DuckDB VIEW（v_<name>），SELECT 只引用 obj_<base_object>
    的列子集——视图是 SQL 投影，不复制数据、不复制权限事实（AC2）；
  - 读视图唯一入口是 OntologyReadGateway.view(name)（AC4：不绕过 REQ-002 网关，
    Store.query 的安全表白名单不含 v_*，直查 v_* 会被视为可疑行为）；
  - 标准视图自动生成：build_standard_views() 为每个对象生成 v_<obj>_basic
    （pk + name_property）与 v_<obj>_full（全部属性），均全角色可见
    （细粒度由 PolicyEngine 在读时执行，不在视图层重复声明，AC2/AC3）；
  - 视图定义纳入 schema 校验：load_views 强校验 schema_version、base_object 引用存在性、
    properties ⊆ 对象属性集、roles ⊆ ROLE_RANK（AC5）；任何不一致硬失败。

权限边界（与 AGENTS.md 三条禁令一致）：
  - 视图只是 SQL 投影，不引入新数据来源；
  - 视图读时仍经 PolicyEngine.check_object / apply_row_masks（对象级 + 属性级），
    视图本身不存任何"谁能看什么"的事实；
  - 视图 v_* 不入 Store.query 安全名单，直查 v_* 视同绕过网关的违规行为。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.access import ROLE_RANK
from core.ontology_loader import PACK_ROOT, load_pack


# ----------------------------------------------------------------------
# 异常
# ----------------------------------------------------------------------
class ViewDefinitionError(ValueError):
    """视图声明非法（schema/引用/属性子集/角色任一不一致，装载期硬失败）。"""


class ViewNotFoundError(KeyError):
    """请求的视图未在 views.json 声明（也未由 build_standard_views 自动生成）。"""


class ViewAccessDenied(PermissionError):
    """角色无权访问该视图（AC1：按角色投影，roles 名单外被拒）。"""


# ----------------------------------------------------------------------
# ViewSpec
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ViewSpec:
    """视图声明（不可变）。"""
    name: str                          # 视图名（物化为 v_<name>）
    base_object: str                   # 基对象（必须是 objects.json 已声明对象）
    properties: tuple[str, ...]        # 投影属性子集（⊆ base_object 的属性集 ∪ {pk}）
    roles: frozenset[str]              # 允许访问的角色集（⊆ ROLE_RANK）
    description: str = ""
    standard: bool = False             # True=build_standard_views 自动生成


# ----------------------------------------------------------------------
# 装载入口
# ----------------------------------------------------------------------
def load_views(pack: str = "default",
               base_dir: Path | None = None) -> dict[str, ViewSpec]:
    """装载 ontology/<pack>/views.json。

    文件缺失 → 返回空字典（视图是可选扩展，不强制每个包都声明）；
    文件存在则强校验（AC5）：
      - schema_version 必须 == 2；
      - 每条 view 必须有 name/base_object/properties/roles 四字段；
      - base_object 必须在 objects.json 声明；
      - properties 必须是该对象属性集 ∪ {pk, name_property} 的子集；
      - roles 必须与 core.access.ROLE_RANK 同源；
      - 视图名不得重复（含 standard 视图合成后）。
    任何不一致硬失败（不准带病编译）。
    """
    root = (base_dir or PACK_ROOT) / pack
    path = root / "views.json"
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ViewDefinitionError(f"views.json 解析失败：{e}") from e

    if data.get("schema_version") != 2:
        raise ViewDefinitionError(
            f"views.json schema_version={data.get('schema_version')}，"
            f"本内核支持 2（与 objects.json/links.json 同口径）")

    # 取已声明对象及其属性集（含 pk / name_property 作为合法输出列）
    pack_spec = load_pack(pack, base_dir=base_dir)
    obj_props: dict[str, set[str]] = {}
    for o in pack_spec.objects:
        obj_props[o.name] = set(o.properties) | {o.pk, o.name_property}

    out: dict[str, ViewSpec] = {}
    for i, v in enumerate(data.get("views", [])):
        ctx = f"views[{i}]"
        for k in ("name", "base_object", "properties", "roles"):
            if k not in v or v[k] in (None, ""):
                raise ViewDefinitionError(f"{ctx} 缺必填字段 {k!r}")
        name = v["name"]
        if not isinstance(name, str) or not name.isidentifier():
            raise ViewDefinitionError(
                f"{ctx} 视图名 {name!r} 非合法标识符（v_<name> 物化需要）")
        if name in out:
            raise ViewDefinitionError(f"{ctx} 视图名 {name!r} 重复声明")
        base = v["base_object"]
        if base not in obj_props:
            raise ViewDefinitionError(
                f"{ctx} base_object {base!r} 未在 objects.json 声明")
        props = tuple(v["properties"])
        if not props:
            raise ViewDefinitionError(f"{ctx} properties 不能为空")
        unknown = set(props) - obj_props[base]
        if unknown:
            raise ViewDefinitionError(
                f"{ctx} 属性 {sorted(unknown)} 不在 {base!r} 声明属性 "
                f"{sorted(obj_props[base])} 内")
        if len(set(props)) != len(props):
            dups = [p for p in props if props.count(p) > 1]
            raise ViewDefinitionError(f"{ctx} 属性 {sorted(set(dups))} 重复")
        roles = frozenset(v["roles"])
        if not roles:
            raise ViewDefinitionError(f"{ctx} roles 不能为空")
        invalid = roles - set(ROLE_RANK)
        if invalid:
            raise ViewDefinitionError(
                f"{ctx} roles {sorted(invalid)} 不在 ROLE_RANK "
                f"{sorted(ROLE_RANK)} 内")
        out[name] = ViewSpec(
            name=name, base_object=base, properties=props,
            roles=roles, description=str(v.get("description", "")))
    return out


def build_standard_views(pack: str = "default",
                         base_dir: Path | None = None) -> dict[str, ViewSpec]:
    """AC3：为每个对象自动生成标准视图。

    每个对象两个标准视图（与对象同生命周期，schema 变更自动跟随）：
      <obj>_basic：pk + name_property（最简投影，全角色可见）；
      <obj>_full：全部声明属性（与对象本身同门槛，细粒度由 PolicyEngine 在读时执行）。

    runtime 对象（如 decision）也生成标准视图——但 v_decision_* 物化时
    依赖 obj_decision 已由 Action 副作用建表；build_ontology 阶段若表不存在
    则按 optional 跳过（与 lnk_* 编译失败的 optional 跳过同口径）。
    """
    pack_spec = load_pack(pack, base_dir=base_dir)
    all_roles = frozenset(ROLE_RANK)
    out: dict[str, ViewSpec] = {}
    for o in pack_spec.objects:
        # basic：去重保留序，确保 pk 与 name_property 都在
        basic_cols = []
        for p in (o.pk, o.name_property):
            if p not in basic_cols:
                basic_cols.append(p)
        out[f"{o.name}_basic"] = ViewSpec(
            name=f"{o.name}_basic", base_object=o.name,
            properties=tuple(basic_cols), roles=all_roles,
            description=f"标准基础视图：{o.name} 的 pk 与 name_property",
            standard=True)
        # full：全部属性 + pk（pk 已在 properties 中也保序加入）
        full_cols = list(o.properties)
        if o.pk not in full_cols:
            full_cols.insert(0, o.pk)
        out[f"{o.name}_full"] = ViewSpec(
            name=f"{o.name}_full", base_object=o.name,
            properties=tuple(full_cols), roles=all_roles,
            description=f"标准全量视图：{o.name} 的全部声明属性",
            standard=True)
    return out


def all_views(pack: str = "default",
              base_dir: Path | None = None) -> dict[str, ViewSpec]:
    """合并显式声明视图 + 标准视图（标准视图不覆盖显式声明）。"""
    explicit = load_views(pack, base_dir=base_dir)
    standard = build_standard_views(pack, base_dir=base_dir)
    out = dict(standard)  # 标准视图打底
    out.update(explicit)  # 显式声明优先（同名覆盖标准视图）
    return out


# ----------------------------------------------------------------------
# 物化器
# ----------------------------------------------------------------------
class ViewMaterializer:
    """AC1：把 ViewSpec 编译为 DuckDB VIEW（v_<name>）。

    CREATE OR REPLACE VIEW v_<name> AS SELECT <properties> FROM obj_<base>
    —— 纯 SELECT，不复制数据、不引入新来源（AC2：权限事实不在视图里）。
    物化幂等：重跑 build_ontology 时 OR REPLACE 覆盖旧定义。
    """

    def __init__(self, conn):
        self._conn = conn

    def materialize(self, view: ViewSpec) -> str:
        if not view.properties:
            raise ViewDefinitionError(f"视图 {view.name!r} 无属性")
        cols = ", ".join(f'"{p}"' for p in view.properties)
        sql = (f'CREATE OR REPLACE VIEW v_{view.name} AS '
               f'SELECT {cols} FROM obj_{view.base_object}')
        try:
            self._conn.execute(sql)
        except Exception as e:
            # 基表不存在（runtime 对象未建表 / optional 跳过场景）
            # → 跳过该视图（与 build_ontology lnk_* 编译失败跳过同口径）
            raise ViewMaterializeError(
                f"物化 v_{view.name} 失败（基表 obj_{view.base_object} 不存在？）：{e}"
            ) from e
        return f"v_{view.name}"

    def materialize_all(self, views: dict[str, ViewSpec],
                        *, skip_missing_base: bool = True) -> dict[str, str]:
        """批量物化。返回 {view_name: status}。

        skip_missing_base=True：基表不存在时跳过该视图（与 optional 跳过同口径）；
        False：基表不存在则抛 ViewMaterializeError（schema 不一致的硬失败）。
        """
        out: dict[str, str] = {}
        for name, v in views.items():
            try:
                out[name] = self.materialize(v)
            except ViewMaterializeError:
                if not skip_missing_base:
                    raise
                out[name] = "_skipped"
        return out

    def drop(self, name: str) -> None:
        """删除视图（如重建前清理）。"""
        self._conn.execute(f'DROP VIEW IF EXISTS v_{name}')

    def drop_all(self, names: list[str]) -> None:
        for n in names:
            self.drop(n)


class ViewMaterializeError(RuntimeError):
    """视图物化失败（基表不存在等）。"""


# ----------------------------------------------------------------------
# 视图表名约定
# ----------------------------------------------------------------------
def view_table_name(view_name: str) -> str:
    """视图 → DuckDB 表名（v_<view_name>）。"""
    return f"v_{view_name}"
