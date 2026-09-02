"""
core/ontology_loader.py
Ontology 案件包装载器：把 ontology/<pack>/*.json 声明校验后编译为内存 dataclass。

设计：
  - 声明是数据（JSON），实现是代码（清洗规则/Function py 实现/Action 副作用按名注册）；
  - 加载时强校验：schema_version、必填字段、引用名（清洗规则/function 实现/
    对象链接引用/副作用名/角色名）存在性，任何未知名/结构错误硬失败（不准带病编译）；
  - 零第三方依赖（stdlib json/dataclasses/pathlib），与 MCP server 风格一致。

案件包结构：
  ontology/<pack>/objects.json   schema_version + objects[]
  ontology/<pack>/links.json     schema_version + links[]
  ontology/<pack>/actions.json   schema_version + actions[]
  ontology/<pack>/functions.json schema_version + functions[]（可选）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.ontology import (
    ObjectSpec, LinkSpec, ActionSpec, ParamSpec, FunctionSpec,
    CLEAN_RULE_NAMES, reverse_reach,
)
from core.registry import ClueStatus

SCHEMA_VERSION = 1
PACK_ROOT = Path(__file__).resolve().parent.parent / "ontology"

ALLOWED_ROLES = {"any", "human"}
ALLOWED_SIDE_EFFECTS = {"set_clue_status", "create_decision"}
ALLOWED_IMPL_KINDS = {"sql", "py"}
ALLOWED_OUTPUT_TYPES = {"rows", "scalar", "report"}
_DERIVE_RULES = {"reverse_reach"}


@dataclass
class OntologyPack:
    name: str
    objects: list[ObjectSpec]
    links: list[LinkSpec]
    actions: dict[str, ActionSpec]
    functions: dict[str, FunctionSpec]


# ----------------------------------------------------------------------
# 装载入口
# ----------------------------------------------------------------------
def load_pack(pack: str = "default", base_dir: Path | None = None) -> OntologyPack:
    root = (base_dir or PACK_ROOT) / pack
    if not root.is_dir():
        raise FileNotFoundError(f"ontology 案件包不存在：{root}")

    objects = _load_objects(root / "objects.json")
    links = _load_links(root / "links.json", objects)
    actions = _load_actions(root / "actions.json")
    functions = _load_functions(root / "functions.json", objects, links,
                                required=False)
    return OntologyPack(name=pack, objects=objects, links=links,
                        actions=actions, functions=functions)


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"ontology 声明文件缺失：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"ontology 声明 JSON 非法（{path.name}）：{e}") from e
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version={data.get('schema_version')}，"
            f"本内核支持 {SCHEMA_VERSION}")
    return data


def _require(d: dict, keys: tuple, ctx: str) -> None:
    for k in keys:
        if k not in d or d[k] in (None, ""):
            raise ValueError(f"ontology 声明 {ctx} 缺必填字段：{k}")


# ----------------------------------------------------------------------
# objects
# ----------------------------------------------------------------------
def _compile_structured_source(src: dict, ctx: str) -> tuple[str, str]:
    """结构化源 → (source_sql, source_table)。"""
    _require(src, ("table", "columns"), f"{ctx}.source")
    table, columns = src["table"], src["columns"]
    if not isinstance(columns, dict) or not columns:
        raise ValueError(f"{ctx}.source.columns 必须是非空映射 {{别名: 源列}}")
    select = ", ".join(f'"{raw}" AS {alias}' for alias, raw in columns.items())
    return f"SELECT {select} FROM {table}", table


def _load_objects(path: Path) -> list[ObjectSpec]:
    data = _read_json(path)
    out: list[ObjectSpec] = []
    seen: set[str] = set()
    for i, o in enumerate(data.get("objects", [])):
        ctx = f"objects[{i}]"
        _require(o, ("name", "pk", "name_col"), ctx)
        name = o["name"]
        if name in seen:
            raise ValueError(f"{ctx} 对象名重复：{name}")
        seen.add(name)

        runtime = bool(o.get("runtime", False))
        source_sql, source_table = o.get("source_sql", ""), o.get("source_table", "")
        if not runtime:
            if "source" in o:
                sql, table = _compile_structured_source(o["source"], ctx)
                source_sql, source_table = sql, table
            if not source_sql:
                raise ValueError(f"{ctx}（{name}）必须声明 source 或 source_sql")
            # name_col 必须是 source 产出列之一（结构化源可静态校验）
            if "source" in o:
                aliases = set(o["source"]["columns"].keys())
                if o["name_col"] not in aliases:
                    raise ValueError(
                        f"{ctx} name_col='{o['name_col']}' 不在 source.columns 别名 {sorted(aliases)} 内")

        clean = tuple(o.get("clean", ()))
        unknown = set(clean) - CLEAN_RULE_NAMES
        if unknown:
            raise ValueError(f"{ctx}（{name}）引用未注册清洗规则：{sorted(unknown)}，"
                             f"可用 {sorted(CLEAN_RULE_NAMES)}")

        out.append(ObjectSpec(
            name=name, title=o.get("title", name), pk=o["pk"],
            source_sql=source_sql, name_col=o["name_col"],
            properties=dict(o.get("properties", {})),
            clean=clean,
            optional_table=bool(o.get("optional", False)),
            row_key=bool(o.get("row_key", False)),
            source_table=source_table,
            runtime=runtime,
        ))
    return out


# ----------------------------------------------------------------------
# links
# ----------------------------------------------------------------------
def _load_links(path: Path, objects: list[ObjectSpec]) -> list[LinkSpec]:
    data = _read_json(path)
    obj_names = {o.name for o in objects}
    out: list[LinkSpec] = []
    seen: set[str] = set()
    for i, l in enumerate(data.get("links", [])):
        ctx = f"links[{i}]"
        _require(l, ("name", "from_obj", "to_obj"), ctx)
        name = l["name"]
        if name in seen:
            raise ValueError(f"{ctx} 链接名重复：{name}")
        seen.add(name)
        for end in ("from_obj", "to_obj"):
            if l[end] not in obj_names:
                raise ValueError(f"{ctx}（{name}）{end}='{l[end]}' 未在 objects 声明")
        runtime = bool(l.get("runtime", False))
        build_sql = l.get("build_sql", "")
        if not runtime and not build_sql:
            raise ValueError(f"{ctx}（{name}）非 runtime 链接必须声明 build_sql")
        out.append(LinkSpec(
            name=name, title=l.get("title", name),
            from_obj=l["from_obj"], to_obj=l["to_obj"],
            build_sql=build_sql,
            properties=dict(l.get("properties", {})),
            runtime=runtime,
        ))
    return out


# ----------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------
def _load_actions(path: Path) -> dict[str, ActionSpec]:
    data = _read_json(path)
    out: dict[str, ActionSpec] = {}
    for i, a in enumerate(data.get("actions", [])):
        ctx = f"actions[{i}]"
        _require(a, ("name", "target_status"), ctx)
        name = a["name"]
        if name in out:
            raise ValueError(f"{ctx} 动作名重复：{name}")
        target = a["target_status"]
        if target not in ClueStatus.ALLOWED:
            raise ValueError(f"{ctx}（{name}）target_status='{target}' 非法，"
                             f"允许 {sorted(ClueStatus.ALLOWED)}")
        derive = a.get("derive", "reverse_reach")
        if derive not in _DERIVE_RULES:
            raise ValueError(f"{ctx}（{name}）未知 derive 规则：{derive}")
        role = a.get("requires_role", "any")
        if role not in ALLOWED_ROLES:
            raise ValueError(f"{ctx}（{name}）requires_role='{role}' 非法，"
                             f"允许 {sorted(ALLOWED_ROLES)}")
        effects = tuple(a.get("side_effects", ()))
        unknown_fx = set(effects) - ALLOWED_SIDE_EFFECTS
        if unknown_fx:
            raise ValueError(f"{ctx}（{name}）引用未注册副作用：{sorted(unknown_fx)}，"
                             f"可用 {sorted(ALLOWED_SIDE_EFFECTS)}")
        params = []
        for p in a.get("parameters", []):
            _require(p, ("name",), f"{ctx}.parameters")
            params.append(ParamSpec(
                name=p["name"], type=p.get("type", "string"),
                required=bool(p.get("required", False)),
                description=p.get("description", ""),
            ))
        out[name] = ActionSpec(
            name=name, target_status=target,
            allowed_from=reverse_reach(target),
            parameters=tuple(params),
            requires_role=role,
            side_effects=effects,
            terminal=bool(a.get("terminal", False)),
            description=a.get("description", ""),
        )
    return out


# ----------------------------------------------------------------------
# functions
# ----------------------------------------------------------------------
def _load_functions(path: Path, objects: list[ObjectSpec], links: list[LinkSpec],
                    required: bool) -> dict[str, FunctionSpec]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"ontology 声明文件缺失：{path}")
        return {}
    data = _read_json(path)
    valid_tables = ({f"obj_{o.name}" for o in objects}
                    | {f"lnk_{l.name}" for l in links})
    # py 实现注册表（延迟导入避免循环依赖）
    try:
        from core import functions as fn_mod
        py_impls = set(getattr(fn_mod, "FUNCTION_IMPLS", {}).keys())
    except ImportError:
        py_impls = set()

    out: dict[str, FunctionSpec] = {}
    for i, f in enumerate(data.get("functions", [])):
        ctx = f"functions[{i}]"
        _require(f, ("name", "output_type", "impl"), ctx)
        name = f["name"]
        if name in out:
            raise ValueError(f"{ctx} 函数名重复：{name}")
        if f["output_type"] not in ALLOWED_OUTPUT_TYPES:
            raise ValueError(f"{ctx}（{name}）output_type='{f['output_type']}' 非法，"
                             f"允许 {sorted(ALLOWED_OUTPUT_TYPES)}")
        kind = f["impl"]
        if kind not in ALLOWED_IMPL_KINDS:
            raise ValueError(f"{ctx}（{name}）impl='{kind}' 非法，允许 {sorted(ALLOWED_IMPL_KINDS)}")
        sql, impl_ref = f.get("sql", ""), f.get("impl_ref", "")
        if kind == "sql" and not sql:
            raise ValueError(f"{ctx}（{name}）impl=sql 必须声明 sql")
        if kind == "py":
            if not impl_ref:
                raise ValueError(f"{ctx}（{name}）impl=py 必须声明 impl_ref")
            if impl_ref not in py_impls:
                raise ValueError(f"{ctx}（{name}）py 实现未注册：'{impl_ref}'，"
                                 f"core.functions.FUNCTION_IMPLS 可用 {sorted(py_impls)}")
        for t in f.get("inputs", []):
            if t not in valid_tables:
                raise ValueError(f"{ctx}（{name}）inputs 引用未声明语义表：{t}，"
                                 f"可用 {sorted(valid_tables)}")
        out[name] = FunctionSpec(
            name=name, title=f.get("title", name),
            inputs=tuple(f.get("inputs", [])),
            output_type=f["output_type"], impl=kind,
            parameters=dict(f.get("parameters", {})),
            impl_ref=impl_ref, sql=sql,
            description=f.get("description", ""),
        )
    return out
