"""
core/ontology_loader.py
Ontology 案件包装载器（schema_version=2）：把 ontology/<pack>/*.json 声明校验后编译为内存 dataclass。

v2 分层（类型层 vs 管道层）：
  objects.json    类型层：ObjectType（name/pk/kind/name_property/properties{属性:值类型}/runtime）
  links.json      类型层：LinkType（from_obj/to_obj/properties{边属性:值类型}/runtime），不含 SQL
  bindings.json   管道层：object_bindings（source/source_sql/clean/optional）+
                          link_bindings（build_sql）
  actions.json    Action Types（受控写回）
  functions.json  Function Types（只读计算，可选）

设计：
  - 声明是数据（JSON），实现是代码（清洗规则/Function py 实现/Action 副作用按名注册）；
  - 加载时强校验：schema_version、必填字段、值类型、交叉引用（binding 必须指向已声明类型、
    非 runtime 类型必须有 binding、结构化源别名必须是已声明属性、链接边属性物化后对账、
    function/action 引用存在性），任何未知名/结构错误硬失败（不准带病编译）；
  - 零第三方依赖（stdlib json/dataclasses/pathlib），与 MCP server 风格一致。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.ontology import (
    ObjectType, ObjectBinding, LinkType, LinkBinding,
    ActionSpec, ParamSpec, FunctionSpec, RuleSpec,
    TYPE_SQL, TYPE_NAMES, OBJECT_KINDS,
    CLEAN_RULE_NAMES, reverse_reach,
)
from core.registry import ClueStatus

SCHEMA_VERSION = 2
PACK_ROOT = Path(__file__).resolve().parent.parent / "ontology"

ALLOWED_ROLES = {"any", "human"}
ALLOWED_SIDE_EFFECTS = {"set_clue_status", "create_decision"}
ALLOWED_IMPL_KINDS = {"sql", "py"}
ALLOWED_OUTPUT_TYPES = {"rows", "scalar", "report"}
ALLOWED_RULE_STAGES = {"xu_shi", "qi_zheng", "yong_jian"}
ALLOWED_DIMENSIONS = {"资金", "通讯", "行为", "关系", "时间"}
ALLOWED_JIAN = {"因间", "内间", "反间", "死间", "生间"}
ALLOWED_HIT_WHEN = {"rows_nonempty", "result_hit"}
RULE_TEXT_MIN = 30
_DERIVE_RULES = {"reverse_reach"}


@dataclass
class OntologyPack:
    name: str
    objects: list[ObjectType]
    links: list[LinkType]
    object_bindings: dict[str, ObjectBinding]
    link_bindings: dict[str, LinkBinding]
    actions: dict[str, ActionSpec]
    functions: dict[str, FunctionSpec]
    rules: dict[str, RuleSpec]


# ----------------------------------------------------------------------
# 装载入口
# ----------------------------------------------------------------------
def load_pack(pack: str = "default", base_dir: Path | None = None) -> OntologyPack:
    root = (base_dir or PACK_ROOT) / pack
    if not root.is_dir():
        raise FileNotFoundError(f"ontology 案件包不存在：{root}")

    objects = _load_objects(root / "objects.json")
    links = _load_links(root / "links.json", objects)
    object_bindings, link_bindings = _load_bindings(
        root / "bindings.json", objects, links)
    actions = _load_actions(root / "actions.json", objects)
    functions = _load_functions(root / "functions.json", objects, links,
                                required=False)
    rules = _load_rules(root / "rules.json", functions, required=False)
    return OntologyPack(name=pack, objects=objects, links=links,
                        object_bindings=object_bindings,
                        link_bindings=link_bindings,
                        actions=actions, functions=functions, rules=rules)


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
            f"本内核支持 {SCHEMA_VERSION}（v2：类型层 objects/links + 管道层 bindings）")
    return data


def _require(d: dict, keys: tuple, ctx: str) -> None:
    for k in keys:
        if k not in d or d[k] in (None, ""):
            raise ValueError(f"ontology 声明 {ctx} 缺必填字段：{k}")


# ----------------------------------------------------------------------
# objects（类型层）
# ----------------------------------------------------------------------
def _load_objects(path: Path) -> list[ObjectType]:
    data = _read_json(path)
    out: list[ObjectType] = []
    seen: set[str] = set()
    for i, o in enumerate(data.get("objects", [])):
        ctx = f"objects[{i}]"
        _require(o, ("name", "pk", "name_property"), ctx)
        name = o["name"]
        if name in seen:
            raise ValueError(f"{ctx} 对象名重复：{name}")
        seen.add(name)

        kind = o.get("kind", "entity")
        if kind not in OBJECT_KINDS:
            raise ValueError(f"{ctx}（{name}）kind='{kind}' 非法，允许 {OBJECT_KINDS}")
        props = o.get("properties", {})
        if not isinstance(props, dict):
            raise ValueError(f"{ctx}（{name}）properties 必须是映射 {{属性名: 值类型}}")
        bad = {p: t for p, t in props.items() if t not in TYPE_NAMES}
        if bad:
            raise ValueError(f"{ctx}（{name}）属性值类型非法：{bad}，允许 {TYPE_NAMES}")
        if o["pk"] in props:
            raise ValueError(f"{ctx}（{name}）pk '{o['pk']}' 不得出现在 properties 中")
        name_prop = o["name_property"]
        if name_prop != o["pk"] and name_prop not in props:
            raise ValueError(
                f"{ctx}（{name}）name_property='{name_prop}' 必须是已声明属性，"
                f"或等于 pk（自引用）")

        out.append(ObjectType(
            name=name, title=o.get("title", name), pk=o["pk"], kind=kind,
            name_property=name_prop, properties=dict(props),
            runtime=bool(o.get("runtime", False)),
        ))
    return out


# ----------------------------------------------------------------------
# links（类型层）
# ----------------------------------------------------------------------
def _load_links(path: Path, objects: list[ObjectType]) -> list[LinkType]:
    data = _read_json(path)
    obj_names = {o.name for o in objects}
    out: list[LinkType] = []
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
        if "build_sql" in l:
            raise ValueError(
                f"{ctx}（{name}）build_sql 属于管道层，请移至 bindings.json 的 link_bindings")
        props = l.get("properties", {})
        if not isinstance(props, dict):
            raise ValueError(f"{ctx}（{name}）properties 必须是映射 {{边属性: 值类型}}")
        bad = {p: t for p, t in props.items() if t not in TYPE_NAMES}
        if bad:
            raise ValueError(f"{ctx}（{name}）边属性类型非法：{bad}，允许 {TYPE_NAMES}")
        out.append(LinkType(
            name=name, title=l.get("title", name),
            from_obj=l["from_obj"], to_obj=l["to_obj"],
            properties=dict(props),
            runtime=bool(l.get("runtime", False)),
        ))
    return out


# ----------------------------------------------------------------------
# bindings（管道层）
# ----------------------------------------------------------------------
def _compile_structured_source(src: dict, otype: ObjectType,
                               ctx: str) -> tuple[str, str]:
    """
    结构化源 → (source_sql, source_table)。
    类型感知：非 string 属性编译期 CAST（与 TYPE_SQL 物化列类型同口径）。
    """
    _require(src, ("table", "columns"), f"{ctx}.source")
    table, columns = src["table"], src["columns"]
    if not isinstance(columns, dict) or not columns:
        raise ValueError(f"{ctx}.source.columns 必须是非空映射 {{别名: 源列}}")
    parts: list[str] = []
    for alias, raw in columns.items():
        t = otype.properties.get(alias, "string")
        if t == "string":
            parts.append(f'"{raw}" AS {alias}')
        else:
            parts.append(f'CAST("{raw}" AS {TYPE_SQL[t]}) AS {alias}')
    return f"SELECT {', '.join(parts)} FROM {table}", table


def _load_bindings(path: Path, objects: list[ObjectType],
                   links: list[LinkType]) -> tuple[dict, dict]:
    data = _read_json(path)
    obj_map = {o.name: o for o in objects}
    link_map = {l.name: l for l in links}
    obj_out: dict[str, ObjectBinding] = {}
    link_out: dict[str, LinkBinding] = {}

    # ---- object_bindings ----
    for i, b in enumerate(data.get("object_bindings", [])):
        ctx = f"object_bindings[{i}]"
        _require(b, ("object",), ctx)
        name = b["object"]
        if name not in obj_map:
            raise ValueError(f"{ctx} 绑定了未声明对象 '{name}'（先在 objects.json 声明类型）")
        if name in obj_out:
            raise ValueError(f"{ctx} 对象 '{name}' 重复绑定")
        otype = obj_map[name]
        if otype.runtime:
            raise ValueError(f"{ctx} runtime 对象 '{name}' 不得有 binding（由 Action 副作用创建）")

        source_sql, source_table = b.get("source_sql", ""), b.get("source_table", "")
        if "source" in b:
            sql, table = _compile_structured_source(b["source"], otype, ctx)
            source_sql, source_table = sql, table
        if not source_sql:
            raise ValueError(f"{ctx}（{name}）必须声明 source 或 source_sql")
        if "source" in b:
            aliases = set(b["source"]["columns"])
            # 合法输出列 = 属性集 ∪ {pk}（name_property 等于 pk 的自引用对象，如 clue）
            allowed = set(otype.properties) | {otype.pk}
            unknown = aliases - allowed
            if unknown:
                raise ValueError(
                    f"{ctx}（{name}）源列别名 {sorted(unknown)} 不在属性声明 "
                    f"{sorted(otype.properties)} 内（类型层与管道层不一致）")
            if otype.name_property not in aliases:
                raise ValueError(
                    f"{ctx}（{name}）结构化源缺少 name_property 列 '{otype.name_property}'")

        clean = tuple(b.get("clean", ()))
        unknown_rules = set(clean) - CLEAN_RULE_NAMES
        if unknown_rules:
            raise ValueError(f"{ctx}（{name}）引用未注册清洗规则：{sorted(unknown_rules)}，"
                             f"可用 {sorted(CLEAN_RULE_NAMES)}")

        obj_out[name] = ObjectBinding(
            object=name, source_sql=source_sql, source_table=source_table,
            clean=clean, optional=bool(b.get("optional", False)))

    missing_obj = [o.name for o in objects if not o.runtime and o.name not in obj_out]
    if missing_obj:
        raise ValueError(f"非 runtime 对象缺少 binding 声明：{missing_obj}")

    # ---- link_bindings ----
    for i, b in enumerate(data.get("link_bindings", [])):
        ctx = f"link_bindings[{i}]"
        _require(b, ("link", "build_sql"), ctx)
        name = b["link"]
        if name not in link_map:
            raise ValueError(f"{ctx} 绑定了未声明链接 '{name}'（先在 links.json 声明类型）")
        if name in link_out:
            raise ValueError(f"{ctx} 链接 '{name}' 重复绑定")
        if link_map[name].runtime:
            raise ValueError(f"{ctx} runtime 链接 '{name}' 不得有 binding（由 Action 副作用写入）")
        link_out[name] = LinkBinding(link=name, build_sql=b["build_sql"])

    missing_link = [l.name for l in links if not l.runtime and l.name not in link_out]
    if missing_link:
        raise ValueError(f"非 runtime 链接缺少 binding 声明：{missing_link}")

    # runtime 链接端点列约定：<from_obj>_id / <to_obj>_id（ensure_runtime_tables 据此建表）
    for l in links:
        if not l.runtime:
            continue
        for end in (l.from_obj, l.to_obj):
            epk = obj_map[end].pk
            if epk != f"{end}_id":
                raise ValueError(
                    f"runtime 链接 {l.name} 端点对象 '{end}' 的 pk 必须是 '{end}_id' "
                    f"（当前 '{epk}'），否则副作用建表列名无法约定")

    return obj_out, link_out


# ----------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------
def _load_actions(path: Path, objects: list[ObjectType]) -> dict[str, ActionSpec]:
    data = _read_json(path)
    obj_names = {o.name for o in objects}
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
        if "create_decision" in effects and "decision" not in obj_names:
            raise ValueError(f"{ctx}（{name}）副作用 create_decision 要求在 objects.json "
                             f"声明 runtime 对象 'decision'")
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
def _load_functions(path: Path, objects: list[ObjectType], links: list[LinkType],
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
        params = dict(f.get("parameters", {}))
        _validate_function_params(params, sql if kind == "sql" else None, ctx, name)
        out[name] = FunctionSpec(
            name=name, title=f.get("title", name),
            inputs=tuple(f.get("inputs", [])),
            output_type=f["output_type"], impl=kind,
            parameters=params,
            impl_ref=impl_ref, sql=sql,
            description=f.get("description", ""),
        )
    return out


def _validate_function_params(params: dict, sql: str | None, ctx: str, name: str) -> None:
    """参数声明校验：类型合法、string 必带 enum、默认值类型合法；
    SQL 实现的占位符与 parameters 双向核对、且每个参数必须有默认值（无参调用可跑）。"""
    from core import functions as fn_mod

    for pname, pspec in params.items():
        pctx = f"{ctx}（{name}）.parameters.{pname}"
        if not isinstance(pspec, dict):
            raise ValueError(f"{pctx} 必须是对象 {{type, default, ...}}")
        ptype = pspec.get("type", "string")
        if ptype not in fn_mod.PARAM_TYPES:
            raise ValueError(f"{pctx} type='{ptype}' 非法，允许 {sorted(fn_mod.PARAM_TYPES)}")
        if ptype == "string" and not pspec.get("enum"):
            raise ValueError(f"{pctx} string 类型必须声明 enum 白名单（防注入）")
        if "default" in pspec:
            fn_mod.check_param_value(pname, pspec, pspec["default"], pctx)
        elif sql is not None:
            raise ValueError(f"{pctx} SQL 函数参数必须声明 default（保证无参调用可跑）")
    if sql is not None:
        placeholders = fn_mod.sql_placeholders(sql)
        missing = placeholders - set(params)
        if missing:
            raise ValueError(f"{ctx}（{name}）SQL 占位符未在 parameters 声明：{sorted(missing)}")
        unused = set(params) - placeholders
        if unused:
            raise ValueError(f"{ctx}（{name}）parameters 已声明但 SQL 未使用：{sorted(unused)}")


# ----------------------------------------------------------------------
# rules（自然语言规则手册，第六段）
# ----------------------------------------------------------------------

def _known_hypothesis_ids() -> set[str]:
    """从 MiaoSuan.FINDING_PATTERNS 提取静态假设 ID（延迟导入避免循环依赖）。

    返回空集表示假设库不可导入（测试隔离场景），调用方应跳过校验。
    """
    try:
        from core.hypotheses import MiaoSuan
        return {p["hypothesis"].id for p in MiaoSuan.FINDING_PATTERNS
                if isinstance(p, dict) and "hypothesis" in p}
    except Exception:
        return set()


def _validate_assumption(assumption: str, ctx: str, rid: str) -> str:
    """校验 assumption 引用：空串合法（无假设驱动）；非空须在已知假设 ID 集合内。"""
    if not assumption:
        return ""
    known = _known_hypothesis_ids()
    if known and assumption not in known:
        raise ValueError(
            f"{ctx}（{rid}）assumption='{assumption}' 未在 core.hypotheses "
            f"MiaoSuan.FINDING_PATTERNS 中声明，可用 {sorted(known)}；"
            f"空字符串表示无假设驱动")
    return assumption


def _load_rules(path: Path, functions: dict[str, FunctionSpec],
                required: bool) -> dict[str, RuleSpec]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"ontology 声明文件缺失：{path}")
        return {}
    data = _read_json(path)
    from core import functions as fn_mod

    out: dict[str, RuleSpec] = {}
    for i, r in enumerate(data.get("rules", [])):
        ctx = f"rules[{i}]"
        _require(r, ("id", "stage", "title", "rule_text", "function", "hit_when"), ctx)
        rid = r["id"]
        if rid in out:
            raise ValueError(f"{ctx} 规则 id 重复：{rid}")
        stage = r["stage"]
        if stage not in ALLOWED_RULE_STAGES:
            raise ValueError(f"{ctx}（{rid}）stage='{stage}' 非法，允许 {sorted(ALLOWED_RULE_STAGES)}")
        dimension = r.get("dimension", "")
        if dimension and dimension not in ALLOWED_DIMENSIONS:
            raise ValueError(f"{ctx}（{rid}）dimension='{dimension}' 非法，允许 {sorted(ALLOWED_DIMENSIONS)}")
        jian = tuple(r.get("jian_types", []))
        bad_jian = set(jian) - ALLOWED_JIAN
        if bad_jian:
            raise ValueError(f"{ctx}（{rid}）jian_types 非法：{sorted(bad_jian)}，允许 {sorted(ALLOWED_JIAN)}")
        hit_when = r["hit_when"]
        if hit_when not in ALLOWED_HIT_WHEN:
            raise ValueError(f"{ctx}（{rid}）hit_when='{hit_when}' 非法，允许 {sorted(ALLOWED_HIT_WHEN)}")
        rule_text = (r.get("rule_text") or "").strip()
        if len(rule_text) < RULE_TEXT_MIN:
            raise ValueError(f"{ctx}（{rid}）rule_text 过短（<{RULE_TEXT_MIN} 字）："
                             f"自然语言判据必须写明模式/反常理由/边界排除")
        fname = r["function"]
        if fname not in functions:
            raise ValueError(f"{ctx}（{rid}）绑定 function '{fname}' 未在 functions.json 声明，"
                             f"可用 {sorted(functions)}")
        fspec = functions[fname]
        params = r.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"{ctx}（{rid}）params 必须是对象")
        unknown = set(params) - set(fspec.parameters)
        if unknown:
            raise ValueError(f"{ctx}（{rid}）params 含函数 '{fname}' 未声明的参数：{sorted(unknown)}，"
                             f"可用 {sorted(fspec.parameters)}")
        for pname, pval in params.items():
            fn_mod.check_param_value(
                pname, fspec.parameters[pname], pval, f"{ctx}（{rid}）.params")
        out[rid] = RuleSpec(
            id=rid, stage=stage, title=r["title"], rule_text=rule_text,
            function=fname, params=params, hit_when=hit_when,
            dimension=dimension, jian_types=jian,
            assumption=_validate_assumption(r.get("assumption", ""), ctx, rid),
            basis_text=r.get("basis_text", r["title"]),
        )
    return out
