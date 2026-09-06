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
import re
from dataclasses import dataclass
from pathlib import Path

from core.ontology import (
    ObjectType, ObjectBinding, LinkType, LinkBinding,
    ActionSpec, ParamSpec, FunctionSpec, RuleSpec,
    TYPE_SQL, TYPE_NAMES, OBJECT_KINDS,
    CLEAN_RULE_NAMES, reverse_reach, _render_projection,
)
from core.registry import ClueStatus
from core.data_elements import CHECKSUM_ALGOS
from core import clean_ops as _clean_ops

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
ALLOWED_OVERLAP_RESOLUTION = {None, "drop_if_primary_hit"}


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
    clean_rules: dict | None = None


# ----------------------------------------------------------------------
# 装载入口
# ----------------------------------------------------------------------
def load_pack(pack: str = "default", base_dir: Path | None = None) -> OntologyPack:
    root = (base_dir or PACK_ROOT) / pack
    if not root.is_dir():
        raise FileNotFoundError(f"ontology 案件包不存在：{root}")

    # REQ-D-001/016：数据元标准先于对象装载——属性 data_element 引用需校验 ID 已注册
    elements = load_data_elements(pack, base_dir)
    # REQ-D-002 AC-4/AD-5：sensitive 数据元属性必须已在 policies.json 声明遮蔽
    mask_set = _load_property_mask_set(root)
    objects = _load_objects(root / "objects.json", elements, mask_set)
    links = _load_links(root / "links.json", objects)
    object_bindings, link_bindings = _load_bindings(
        root / "bindings.json", objects, links)
    actions = _load_actions(root / "actions.json", objects)
    functions = _load_functions(root / "functions.json", objects, links,
                                required=False)
    # REQ-G-011：维度声明先于规则装载——规则的 dimension 必须是 dimensions.json
    # 已声明的 name（缺省回落内置 5 维）；新增维度在声明文件加一项即被规则引用。
    dim_names = load_dimensions(pack, base_dir)
    rules = _load_rules(root / "rules.json", functions, required=False,
                        allowed_dimensions=set(dim_names))
    # REQ-G-012：枚举空间声明化——存在即校验版本与结构（缺失回落内置默认）。
    load_enum_space(pack, base_dir)
    # REQ-D-007：案件级清洗词表（clean_rules.json，缺失回落内置基线词表）。
    clean_rules = _load_clean_rules(root)
    return OntologyPack(name=pack, objects=objects, links=links,
                        object_bindings=object_bindings,
                        link_bindings=link_bindings,
                        actions=actions, functions=functions, rules=rules,
                        clean_rules=clean_rules)


# REQ-G-011：维度缺省内置集（dimensions.json 缺失时回落，保证旧案件包/精简测试包兼容）
DEFAULT_DIMENSIONS = ["资金", "通讯", "行为", "关系", "时间"]


def _as_dim_list(v) -> list[str]:
    """规则/假设的 dimension 可能是字符串或列表，归一为列表。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    return [x for x in v if x]


def load_dimensions(pack: str = "default", base_dir: Path | None = None) -> list[str]:
    """返回维度名有序列表。dimensions.json 缺失 → 内置默认 5 维；存在 → 校验版本/非空。"""
    root = (base_dir or PACK_ROOT) / pack
    p = root / "dimensions.json"
    if not p.exists():
        return list(DEFAULT_DIMENSIONS)
    data = _read_json(p)
    dims = data.get("dimensions", [])
    names: list[str] = []
    seen: set[str] = set()
    for i, d in enumerate(dims):
        name = d.get("name") if isinstance(d, dict) else None
        if not name:
            raise ValueError(f"dimensions.json dimensions[{i}] 缺 name 字段")
        if name in seen:
            raise ValueError(f"dimensions.json 维度名重复：{name}")
        seen.add(name)
        names.append(name)
    if not names:
        raise ValueError("dimensions.json 声明为空：至少需要一个维度（REQ-G-011）")
    return names


def load_enum_space(pack: str = "default", base_dir: Path | None = None) -> dict | None:
    """返回枚举空间 dict；enum_space.json 缺失 → None（调用方回落内置默认）。"""
    root = (base_dir or PACK_ROOT) / pack
    p = root / "enum_space.json"
    if not p.exists():
        return None
    data = _read_json(p)
    space = data.get("space", {})
    if not isinstance(space, dict) or not space:
        raise ValueError("enum_space.json 的 space 必须是非空对象（REQ-G-012）")
    for k, vals in space.items():
        if not isinstance(vals, list) or not vals:
            raise ValueError(f"enum_space.json space.{k} 必须是非空数组")
        # REQ-D-003 AC-3：枚举值必须是非空字符串（非法枚举装载期硬失败）
        if not all(isinstance(x, str) and x.strip() for x in vals):
            raise ValueError(
                f"enum_space.json space.{k} 枚举值必须是非空字符串（REQ-D-003 AC-3）")
    return space


def derive_code_tables(elements: dict) -> dict:
    """REQ-D-003 AC-1：从数据元 enum 值域派生标准代码表 {维度名: [枚举值...]}。

    维度名取数据元 ``enum_space_dim`` 声明，缺省用数据元 name；同一维度多个数据元
    的值域并集去重保序。代码表承载性别/币种/证件类型/案件类别等标准值域，
    天然不含具体人名/地名（人名属实体数据，由 obj_* 承载）。
    """
    tables: dict[str, list[str]] = {}
    for eid in sorted(elements):
        spec = elements[eid]
        if not isinstance(spec, dict):
            continue
        vals = spec.get("enum")
        if not vals:
            continue
        dim = spec.get("enum_space_dim") or spec.get("name") or eid
        bucket = tables.setdefault(str(dim), [])
        for v in vals:
            sv = str(v)
            if sv not in bucket:
                bucket.append(sv)
    return tables


def load_code_tables(pack: str = "default", base_dir: Path | None = None) -> dict:
    """REQ-D-003 AC-1/AC-4：标准代码表（数据元 enum 派生）+ 案件级追加合并。

    案件包可在 enum_space.json 的 ``code_tables`` 段追加自定义枚举值
    （{维度: [追加值...]}）；标准值永远排在前面、不被覆盖或删除（追加并集，
    标准值优先，AC-4）。返回 {维度: [标准值..., 案件追加值...]}。
    """
    elements = load_data_elements(pack, base_dir)
    tables = derive_code_tables(elements)
    root = (base_dir or PACK_ROOT) / pack
    p = root / "enum_space.json"
    if p.exists():
        data = _read_json(p)
        extra = data.get("code_tables", {})
        if extra:
            if not isinstance(extra, dict):
                raise ValueError(
                    "enum_space.json code_tables 必须是 {维度: [追加值...]} 映射（REQ-D-003 AC-4）")
            for dim, vals in extra.items():
                if not isinstance(vals, list) or \
                   not all(isinstance(x, str) and x.strip() for x in vals):
                    raise ValueError(
                        f"enum_space.json code_tables.{dim} 必须是非空字符串数组（REQ-D-003 AC-3）")
                bucket = tables.setdefault(str(dim), [])
                for v in vals:
                    if v not in bucket:
                        bucket.append(v)   # 仅追加，不改写/删除标准值
    return tables


def _load_clean_rules(root: Path) -> dict | None:
    """REQ-D-007：读 clean_rules.json 案件级清洗词表；文件缺失 → None（回落内置基线）。

    结构：{"schema_version": 2, "mode": "merge"(默认)|"replace",
           "org_keywords": [str...], "summary_tokens": [str...]}
    校验：schema_version 一致；mode 合法；两词表必须是字符串数组（空数组合法——
    merge 下该表 no-op 回落基线，replace 下该表为空集）；非字符串元素硬失败。
    合并/替换语义由 clean_ops.build_clean_context 执行。
    """
    p = root / "clean_rules.json"
    if not p.exists():
        return None
    data = _read_json(p)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"clean_rules.json schema_version={data.get('schema_version')} 与内核 "
            f"{SCHEMA_VERSION} 不符（REQ-D-002 版本锚定）")
    mode = data.get("mode", "merge")
    if mode not in ("merge", "replace"):
        raise ValueError(f"clean_rules.json mode='{mode}' 非法，允许 merge | replace（REQ-D-007）")
    out: dict = {"mode": mode, "org_keywords": [], "summary_tokens": []}
    for key in ("org_keywords", "summary_tokens"):
        vals = data.get(key, [])
        if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
            raise ValueError(
                f"clean_rules.json {key} 必须是字符串数组（REQ-D-007；空数组=no-op，"
                f"非字符串元素硬失败）")
        out[key] = [x.strip() for x in vals if x.strip()]
    return out


# ----------------------------------------------------------------------
# data_elements（REQ-D-001 数据元标准，第 14 声明文件）
# ----------------------------------------------------------------------
class _DuplicateKeyError(ValueError):
    pass


def _no_dup_pairs(pairs):
    """object_pairs_hook：JSON 对象内重复键硬失败（dict 形态的元素 ID 重复检测，AC-5）。"""
    d = {}
    for k, v in pairs:
        if k in d:
            raise _DuplicateKeyError(k)
        d[k] = v
    return d


def load_data_elements(pack: str = "default", base_dir: Path | None = None) -> dict:
    """返回 {元素 ID: 元素声明}；data_elements.json 缺失 → {}（向后兼容）。

    校验（REQ-D-001）：schema_version 一致（AC-3）；必填字段缺失硬失败（AC-1）；
    type 值类型合法；未知 checksum 算法硬失败（AC-2 fail-closed）；元素 ID 重复
    硬失败（AC-5）；clean_rule 必须已在 op 注册表 clean 层（与 binding→clean 同口径）。
    """
    root = (base_dir or PACK_ROOT) / pack
    p = root / "data_elements.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"),
                          object_pairs_hook=_no_dup_pairs)
    except _DuplicateKeyError as e:
        raise ValueError(
            f"data_elements.json 数据元 ID 重复注册：'{e.args[0]}'（REQ-D-001 AC-5）") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"ontology 声明 JSON 非法（data_elements.json）：{e}") from e
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"data_elements.json schema_version={data.get('schema_version')}，"
            f"本内核支持 {SCHEMA_VERSION}（REQ-D-001 AC-3：与其余声明文件一致）")
    elements = data.get("elements", {})
    if not isinstance(elements, dict) or not elements:
        raise ValueError("data_elements.json elements 必须是非空映射 {元素ID: 声明}")
    for eid, spec in elements.items():
        if not isinstance(spec, dict):
            raise ValueError(f"data_elements['{eid}'] 声明必须是对象")
        _require(spec, ("name", "type"), f"data_elements['{eid}']")
        if spec["type"] not in TYPE_NAMES:
            raise ValueError(
                f"data_elements['{eid}'] type='{spec['type']}' 非法，允许 {TYPE_NAMES}")
        checksum = spec.get("checksum")
        if checksum is not None and checksum not in CHECKSUM_ALGOS:
            raise ValueError(
                f"data_elements['{eid}'] 未知 checksum 算法：'{checksum}'，"
                f"已注册 {sorted(CHECKSUM_ALGOS)}（REQ-D-001 AC-2 fail-closed）")
        clean_rule = spec.get("clean_rule")
        if clean_rule is not None:
            if not isinstance(clean_rule, str):
                raise ValueError(
                    f"data_elements['{eid}'] clean_rule 必须是 op 名字符串（可带白名单参数）")
            try:
                _clean_ops.validate_op(clean_rule, "clean")
            except ValueError as e:
                raise ValueError(
                    f"data_elements['{eid}'] clean_rule='{clean_rule}' 非法：{e}，"
                    f"可用 {sorted(CLEAN_RULE_NAMES)}")
        # REQ-D-016：合规扫描相关字段装载期校验（fail-closed，扫描期不做容错）
        fmt = spec.get("format")
        if fmt is not None:
            if not isinstance(fmt, str) or not fmt.strip():
                raise ValueError(f"data_elements['{eid}'] format 必须是非空正则字符串")
            try:
                re.compile(fmt)
            except re.error as e:
                raise ValueError(f"data_elements['{eid}'] format 正则非法：{e}") from e
        rng = spec.get("range")
        if rng is not None:
            if (not isinstance(rng, dict) or not rng
                    or any(k not in ("min", "max") for k in rng)):
                raise ValueError(
                    f"data_elements['{eid}'] range 必须是 {{min?, max?}} 非空映射")
        enum_vals = spec.get("enum")
        if enum_vals is not None:
            if (not isinstance(enum_vals, list) or not enum_vals
                    or any(not isinstance(x, (str, int, float)) for x in enum_vals)):
                raise ValueError(
                    f"data_elements['{eid}'] enum 必须是非空数组（代码表）")
    return elements


# REQ-D-016：合规检查项（AC-6 可经 data_elements.json 顶层 compliance_checks 启停）
COMPLIANCE_CHECK_NAMES = ("format", "checksum", "range", "enum")


def load_compliance_checks(pack: str = "default",
                           base_dir: Path | None = None) -> dict:
    """返回合规检查项启停声明（REQ-D-016 AC-6）：data_elements.json 顶层
    compliance_checks（{检查项: bool}）；缺失 → {}（调用方回落全开）。
    键非法或值非 bool 硬失败（fail-closed）。"""
    root = (base_dir or PACK_ROOT) / pack
    p = root / "data_elements.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    decl = data.get("compliance_checks")
    if decl is None:
        return {}
    if not isinstance(decl, dict):
        raise ValueError("data_elements.json compliance_checks 必须是映射 {检查项: bool}")
    for k, v in decl.items():
        if k not in COMPLIANCE_CHECK_NAMES:
            raise ValueError(
                f"data_elements.json compliance_checks 未知检查项 '{k}'，"
                f"允许 {COMPLIANCE_CHECK_NAMES}")
        if not isinstance(v, bool):
            raise ValueError(
                f"data_elements.json compliance_checks['{k}'] 值必须是 bool")
    return dict(decl)


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


def _parse_jian(d: dict, ctx: str) -> tuple[str, str]:
    """REQ-G-013：读取可选 jian/jian_source；jian 非空时必须在五间枚举内（非法硬失败）。"""
    jian = (d.get("jian") or "").strip()
    if jian and jian not in ALLOWED_JIAN:
        raise ValueError(f"{ctx} jian='{jian}' 非法，允许 {sorted(ALLOWED_JIAN)}（REQ-G-013）")
    return jian, (d.get("jian_source") or "").strip()


# ----------------------------------------------------------------------
# objects（类型层）
# ----------------------------------------------------------------------
def _load_property_mask_set(root: Path) -> set[tuple[str, str]]:
    """轻量读 policies.json property_policies → {(object, property)} 遮蔽集合。

    REQ-D-002 AC-4/AD-5：引用 sensitive:true 数据元的属性必须已声明遮蔽。
    policies.json 缺失 → 空集合（敏感引用将因此硬失败，fail-closed 不放宽）；
    此处只取遮蔽键集合，不重复 PolicyEngine 的角色/版本校验（运行时仍由它把关）。
    """
    p = root / "policies.json"
    if not p.exists():
        return set()
    data = _read_json(p)
    out: set[tuple[str, str]] = set()
    for x in data.get("property_policies", []):
        if isinstance(x, dict) and x.get("object") and x.get("property"):
            out.add((x["object"], x["property"]))
    return out


def _load_objects(path: Path,
                  elements: dict | None = None,
                  mask_set: set | None = None) -> list[ObjectType]:
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
        # REQ-D-013：属性值可为 string 或映射 {"type": ..., "composite": true}
        # REQ-D-016/002：映射还允许 {"data_element": "DE_X"} 引用数据元
        # REQ-D-002：引用数据元时 type 自动继承（AC-1）、本地冲突硬失败（AC-2）、
        #   sensitive 必须已声明遮蔽（AC-4/AD-5）、clean_rule 自动挂接（AC-3）
        elements = elements or {}
        composite: list[str] = []
        prop_de: dict[str, str] = {}
        prop_de_clean: dict[str, str] = {}
        norm_props: dict[str, str] = {}
        bad: dict = {}
        for p, t in props.items():
            if isinstance(t, dict):
                unknown = {k for k in t
                           if k not in ("type", "composite", "data_element")}
                if unknown:
                    raise ValueError(
                        f"{ctx}（{name}）属性 '{p}' 声明映射含未知键 {sorted(unknown)}"
                        f"（fail-closed；允许键：type/composite/data_element，"
                        f"REQ-D-013/REQ-D-016）")
                base = t.get("type")
                de = t.get("data_element")
                if de is not None:
                    if not isinstance(de, str) or not de.strip():
                        raise ValueError(
                            f"{ctx}（{name}）属性 '{p}' data_element 必须是非空元素 ID")
                    if de not in elements:
                        raise ValueError(
                            f"{ctx}（{name}）属性 '{p}' 引用未注册数据元：'{de}'"
                            f"（REQ-D-002 AC-6：未知 ID 硬失败；"
                            f"已注册 {sorted(elements)}）")
                    spec = elements[de]
                    de_type = spec.get("type")
                    if base is None:
                        base = de_type              # AC-1：类型自动继承
                    elif base != de_type:
                        raise ValueError(             # AC-2：本地与标准冲突硬失败
                            f"{ctx}（{name}）属性 '{p}' 本地声明 type='{base}' "
                            f"与数据元 '{de}' type='{de_type}' 冲突"
                            f"（REQ-D-002 AC-2：本地不得静默覆盖标准；"
                            f"请省略本地 type 由数据元继承，或改为一致类型）")
                    # AC-4/AD-5：sensitive 数据元 → 属性必须已在 policies 声明遮蔽
                    if spec.get("sensitive") and (name, p) not in (mask_set or set()):
                        raise ValueError(
                            f"{ctx}（{name}）属性 '{p}' 引用敏感数据元 '{de}'"
                            f"（sensitive:true）但未在 policies.json property_policies "
                            f"声明遮蔽（REQ-D-002 AC-4/AD-5 fail-closed："
                            f"敏感属性必须先声明 {name}.{p} 的遮蔽策略）")
                    cr = spec.get("clean_rule")      # AC-3：清洗规则自动挂接
                    if cr:
                        prop_de_clean[p] = cr
                    prop_de[p] = de
                if base not in TYPE_NAMES:
                    bad[p] = base
                    continue
                if t.get("composite"):
                    if base != "string":
                        raise ValueError(
                            f"{ctx}（{name}）属性 '{p}' composite 降级仅支持 string 类型"
                            f"（当前 {base}；复合列整列保留，不参与 CAST，REQ-D-013）")
                    composite.append(p)
                norm_props[p] = base
            elif t in TYPE_NAMES:
                norm_props[p] = t
            else:
                bad[p] = t
        props = norm_props
        if bad:
            raise ValueError(f"{ctx}（{name}）属性值类型非法：{bad}，允许 {TYPE_NAMES}")
        if o["pk"] in props:
            raise ValueError(f"{ctx}（{name}）pk '{o['pk']}' 不得出现在 properties 中")
        name_prop = o["name_property"]
        if name_prop != o["pk"] and name_prop not in props:
            raise ValueError(
                f"{ctx}（{name}）name_property='{name_prop}' 必须是已声明属性，"
                f"或等于 pk（自引用）")
        if name_prop in composite:
            raise ValueError(
                f"{ctx}（{name}）name_property '{name_prop}' 不得声明 composite"
                f"（REQ-D-013 AC-5：身份列不参与复合降级）")

        # REQ-041 AC2: enum 属性必须有 enum_values 白名单，装载期校验
        enum_values: dict[str, list[str]] = {}
        raw_enum = o.get("enum_values", {})
        if raw_enum:
            if not isinstance(raw_enum, dict):
                raise ValueError(f"{ctx}（{name}）enum_values 必须是映射 {{属性名: [允许值]}}")
            for prop_name, allowed in raw_enum.items():
                if prop_name not in props or props[prop_name] != "enum":
                    raise ValueError(
                        f"{ctx}（{name}）enum_values 中的 '{prop_name}' "
                        f"不是 enum 类型属性（REQ-041 AC2）")
                if not isinstance(allowed, list) or not allowed:
                    raise ValueError(
                        f"{ctx}（{name}）enum_values['{prop_name}'] "
                        f"必须是非空列表（REQ-041 AC2）")
                enum_values[prop_name] = list(allowed)
        # enum 类型属性必须有 enum_values 声明
        for p, t in props.items():
            if t == "enum" and p not in enum_values:
                raise ValueError(
                    f"{ctx}（{name}）属性 '{p}' 声明为 enum 但未声明 enum_values"
                    f"（REQ-041 AC2：enum 属性必须有白名单）")

        jian, jian_source = _parse_jian(o, f"{ctx}（{name}）")
        # REQ-P-034：元数据/内容属性排除声明（不参与实体连接与画像）
        md = o.get("metadata_props", [])
        if not isinstance(md, list) or any(
                not isinstance(x, str) or not x.strip() for x in md):
            raise ValueError(
                f"{ctx}（{name}）metadata_props 必须是非空字符串列表（REQ-P-034）")
        if len(set(md)) != len(md):
            raise ValueError(f"{ctx}（{name}）metadata_props 存在重复项（REQ-P-034）")
        unknown_md = [x for x in md if x not in props]
        if unknown_md:
            raise ValueError(
                f"{ctx}（{name}）metadata_props 引用未声明属性：{unknown_md}"
                f"（REQ-P-034：只允许排除已声明属性）")
        out.append(ObjectType(
            name=name, title=o.get("title", name), pk=o["pk"], kind=kind,
            name_property=name_prop, properties=dict(props),
            runtime=bool(o.get("runtime", False)),
            enum_values=enum_values,
            jian=jian, jian_source=jian_source,
            metadata_props=tuple(md),
            composite_props=tuple(composite),
            prop_data_elements=prop_de,
            prop_de_clean=prop_de_clean,
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
        jian, jian_source = _parse_jian(l, f"{ctx}（{name}）")
        endpoints = _parse_endpoints(l, f"{ctx}（{name}）", obj_names)
        out.append(LinkType(
            name=name, title=l.get("title", name),
            from_obj=l["from_obj"], to_obj=l["to_obj"],
            properties=dict(props),
            runtime=bool(l.get("runtime", False)),
            jian=jian, jian_source=jian_source,
            endpoints=endpoints,
        ))
    return out


def _parse_endpoints(l: dict, ctx: str, obj_names: set[str]) -> dict:
    """REQ-G-015：解析图导出端点声明（可选）。

    形态：{"from": {"col": "<lnk 列>", "ref": {"object","key","name"}?},
           "to":   {...}, "extra": ["直传边属性列", ...]}
    col 为 lnk_<link> 中直接可读的端点列；ref 存在表示该列是代理键，需 JOIN
    obj_<object> 按 key 取 name 列（导出边端点名）。非法结构装载期硬失败。
    """
    ep = l.get("endpoints")
    if ep is None:
        return {}
    if not isinstance(ep, dict):
        raise ValueError(f"{ctx} endpoints 必须是对象")
    out: dict = {}
    for side in ("from", "to"):
        e = ep.get(side)
        if not isinstance(e, dict) or not e.get("col"):
            raise ValueError(f"{ctx} endpoints.{side} 必须含非空 'col'")
        ref = e.get("ref")
        ref_out = None
        if ref is not None:
            if not isinstance(ref, dict):
                raise ValueError(f"{ctx} endpoints.{side}.ref 必须是对象")
            for k in ("object", "key", "name"):
                if not ref.get(k):
                    raise ValueError(f"{ctx} endpoints.{side}.ref 缺 '{k}'")
            if ref["object"] not in obj_names:
                raise ValueError(
                    f"{ctx} endpoints.{side}.ref.object='{ref['object']}' 未在 objects 声明")
            ref_out = {"object": ref["object"], "key": ref["key"],
                       "name": ref["name"]}
        out[side] = {"col": e["col"], "ref": ref_out}
    extra = ep.get("extra", [])
    if extra is not None:
        if not isinstance(extra, list) or not all(isinstance(x, str) and x for x in extra):
            raise ValueError(f"{ctx} endpoints.extra 必须是非空字符串数组")
        out["extra"] = list(extra)
    return out


# ----------------------------------------------------------------------
# bindings（管道层）
# ----------------------------------------------------------------------
def _parse_on_cast_error_map(raw, otype: ObjectType, ctx: str, name: str,
                             structured: bool) -> tuple:
    """REQ-D-010：on_cast_error 声明 → 属性级映射 ((属性, 状态), ...)。

    仅结构化源（source）绑定支持（手写 source_sql 请自行保证 CAST 语义）；
    属性必须是已声明的非 string 类型（string 无 CAST，无从失败）；
    状态仅允许 "fail"（硬 CAST 回退能力）/ "quarantine"（TRY_CAST 失败行隔离），
    未声明属性缺省 = null（TRY_CAST 降级 NULL，B2-08 现状）；
    quarantine 别名不得以 __raw_ 开头（与隔离检测隐藏列命名冲突）。
    """
    if raw is None:
        return ()
    if not structured:
        raise ValueError(
            f"{ctx}（{name}）on_cast_error 仅支持结构化源（source）绑定；"
            f"手写 source_sql 的 CAST 语义请在上游 SQL 内保证")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{ctx}（{name}）on_cast_error 必须是非空属性级映射 "
            f"{{属性: \"fail\"|\"quarantine\"}}（REQ-D-010）")
    items: list[tuple[str, str]] = []
    for prop, state in raw.items():
        if prop not in otype.properties:
            raise ValueError(
                f"{ctx}（{name}）on_cast_error 属性 '{prop}' 不在对象属性声明 "
                f"{sorted(otype.properties)} 内")
        if otype.properties[prop] == "string":
            raise ValueError(
                f"{ctx}（{name}）on_cast_error 属性 '{prop}' 是 string 类型——"
                f"string 投影无 CAST，无从失败（REQ-D-010：仅允许非 string 值类型）")
        if state is None:
            continue   # 显式 null = 保持缺省 TRY_CAST 降级语义
        if state not in ("fail", "quarantine"):
            raise ValueError(
                f"{ctx}（{name}）on_cast_error['{prop}'] 状态 '{state}' 非法，"
                f"仅允许 fail / quarantine（null=缺省降级 NULL）（REQ-D-010）")
        if str(prop).startswith("__raw_"):
            raise ValueError(
                f"{ctx}（{name}）on_cast_error 属性 '{prop}' 不得以 __raw_ 开头"
                f"（与隔离检测隐藏列命名冲突，REQ-D-010）")
        items.append((prop, state))
    return tuple(items)


NULL_POLICY_STATES = ("allow", "reject", "quarantine")
DEDUP_CONFLICT_POLICIES = ("keep_latest", "keep_first", "fail")


def _parse_null_policy(raw, otype: ObjectType, ctx: str, name: str) -> tuple:
    """REQ-D-014：null_policy 声明 → 属性级映射 ((属性, 状态), ...)。

    状态 ∈ allow（缺省，NULL 保留，与现状一致）/ reject（空值行剔除）/
    quarantine（空值行整行隔离）。空值策略对 string 与非 string 都适用
    （"金额为空"与"备注为空"严重性不同），且支持手写 source_sql（空值判定
    不依赖 __raw_ 隐藏列）；只返回 reject/quarantine 动作项（allow=不动作）。
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{ctx}（{name}）null_policy 必须是非空属性级映射 "
            f"{{属性: \"allow\"|\"reject\"|\"quarantine\"}}（REQ-D-014）")
    items: list[tuple[str, str]] = []
    for prop, state in raw.items():
        if prop not in otype.properties:
            raise ValueError(
                f"{ctx}（{name}）null_policy 属性 '{prop}' 不在对象属性声明 "
                f"{sorted(otype.properties)} 内")
        if state is None or state == "allow":
            continue   # 显式 allow/null = 缺省语义（NULL 保留）
        if state not in NULL_POLICY_STATES:
            raise ValueError(
                f"{ctx}（{name}）null_policy['{prop}'] 状态 '{state}' 非法，"
                f"仅允许 allow / reject / quarantine（REQ-D-014）")
        items.append((prop, state))
    return tuple(items)


def _parse_dedup_key(raw, otype: ObjectType, ctx: str, name: str) -> tuple:
    """REQ-D-015：key 声明 → (业务键列元组, on_conflict 策略)。

    key = {"columns": ["serial_no", ...], "on_conflict": "keep_latest"}；
    columns 必须是非空属性名数组（业务键，非全行比对）；on_conflict ∈
    keep_latest（缺省）/ keep_first / fail。
    """
    if raw is None:
        return (), ""
    if not isinstance(raw, dict) or not raw.get("columns"):
        raise ValueError(
            f"{ctx}（{name}）key 必须是 {{\"columns\": [业务键列...], "
            f"\"on_conflict\"?: ...}} 非空映射（REQ-D-015）")
    cols = raw["columns"]
    if not isinstance(cols, list) or not all(isinstance(c, str) and c for c in cols):
        raise ValueError(
            f"{ctx}（{name}）key.columns 必须是非空字符串数组（REQ-D-015）")
    unknown = [c for c in cols if c not in otype.properties]
    if unknown:
        raise ValueError(
            f"{ctx}（{name}）key.columns {unknown} 不在对象属性声明 "
            f"{sorted(otype.properties)} 内（REQ-D-015）")
    conflict = raw.get("on_conflict", "keep_latest")
    if conflict not in DEDUP_CONFLICT_POLICIES:
        raise ValueError(
            f"{ctx}（{name}）key.on_conflict='{conflict}' 非法，仅允许 "
            f"{DEDUP_CONFLICT_POLICIES}（REQ-D-015）")
    return tuple(cols), conflict


def _compile_structured_source(src: dict, otype: ObjectType,
                               ctx: str,
                               transform_map: tuple = (),
                               on_cast_error_map: tuple = ()) -> tuple[str, str, tuple, tuple]:
    """
    结构化源 → (source_sql, source_table, typed_raw, projections)。
    类型感知：非 string 属性编译期 TRY_CAST（与 TYPE_SQL 物化列类型同口径）——
    脏值（如 2024-02-30、2025-13-01）降级 NULL 不中断 build（鲁棒性 B2-08/09/10），
    构建期按列计数经 run_health 落诊断（source_value_cast_failed），不静默丢失。
    typed_raw = ((别名, 源列, 值类型), ...) 非 string 属性，供构建期脏值计数；
    projections = ((别名, 源列, 值类型), ...) 全列，供缺列预检后重渲染类型化 NULL。
    transform_map（REQ-D-009 / AD-1）：((别名, (op, ...)), ...) 属性级声明，
    声明式 op 链编译为 SQL 表达式注入 TRY_CAST 之前；未声明属性行为不变。
    on_cast_error_map（REQ-D-010）：((别名, 状态), ...)——fail 属性渲染硬 CAST
    （脏值中断 build 的回退能力）；quarantine 属性保持 TRY_CAST 并追加 __raw_<别名>
    隐藏列（原始源列值，构建期据此检出失败行整行隔离）。
    """
    _require(src, ("table", "columns"), f"{ctx}.source")
    table, columns = src["table"], src["columns"]
    if not isinstance(columns, dict) or not columns:
        raise ValueError(f"{ctx}.source.columns 必须是非空映射 {{别名: 源列}}")
    # REQ-D-012 1:1 约束守护：同一 binding 内一源列只允许映射一个别名（可追溯性底线）。
    # 出路：从同一列派生多属性请改用 source_sql 在上游处理（派生属性豁免，不在此列）。
    seen_cols: dict[str, str] = {}
    for alias, raw in columns.items():
        col = str(raw)
        dup = seen_cols.get(col)
        if dup is not None:
            raise ValueError(
                f"{ctx}.source 同一源列映射多个属性：'{col}' 同时被别名 "
                f"'{dup}' 与 '{alias}' 引用（REQ-D-012：一源列一属性；"
                f"如需从同一列派生多个属性，请改用 source_sql 在上游处理）")
        seen_cols[col] = alias
    tf_map = {alias: ops for alias, ops in transform_map}
    oce = {alias: state for alias, state in on_cast_error_map}
    parts: list[str] = []
    typed: list[tuple] = []
    projections: list[tuple] = []
    for alias, raw in columns.items():
        t = otype.properties.get(alias, "string")
        projections.append((alias, raw, t))
        parts.append(_render_projection(alias, raw, t, tf_map.get(alias),
                                        hard=(oce.get(alias) == "fail")))
        if oce.get(alias) == "quarantine":
            parts.append(f'"{raw}" AS "__raw_{alias}"')
        if t != "string":
            typed.append((alias, raw, t))
    return f"SELECT {', '.join(parts)} FROM {table}", table, tuple(typed), tuple(projections)


# REQ-D-012：split 类 op 识别（split / split_part / split_str 等，大小写敏感的声明约定）。
_SPLIT_OP_RE = re.compile(r"split(_\w+)?")


def _transform_split_ops(transform) -> list[str]:
    """收集 transform 声明中的 split 类 op 名。

    transform 形态（属性级映射，批 D2 全量校验）：{属性: [op, ...] | op}；
    op 允许携带参数（"op:param"），取冒号前的 op 名判定。
    非 dict / 结构错误的 transform 交由 transform 层（REQ-D-009）校验，这里只守护 split。
    """
    ops: list[str] = []
    if not isinstance(transform, dict):
        return ops
    for v in transform.values():
        for tok in (v if isinstance(v, list) else [v]):
            op = str(tok).split(":", 1)[0].strip()
            if _SPLIT_OP_RE.fullmatch(op):
                ops.append(op)
    return ops


def _parse_op_list(ops, ctx: str, where: str) -> tuple:
    """op 列表归一：str → [str]；非字符串元素硬失败。"""
    if isinstance(ops, str):
        ops = [ops]
    if not isinstance(ops, list) or not all(isinstance(o, str) and o.strip() for o in ops):
        raise ValueError(f"{ctx} {where} 必须是 op 名字符串数组（或单个 op 名字符串）")
    return tuple(o.strip() for o in ops)


def _parse_clean_map(raw, otype: ObjectType, ctx: str, name: str) -> tuple:
    """REQ-D-005：clean 声明 → 属性级映射 ((属性, (op, ...)), ...)。

    数组形式向后兼容 = {"_name": [...]}（_name 为名称列约定别名）；
    映射形式的键必须是 _name 或对象已声明属性。规则按声明顺序执行（AC-4）。
    校验：op 必须已注册（clean 层可用名，即 CLEAN_RULE_NAMES）且 impl=py
    （clean 在 Python 侧执行，SQL op 属 transform 层）。
    """
    if raw is None or raw == () or raw == []:
        return ()
    if isinstance(raw, list):
        raw = {"_name": raw} if raw else ()
    if not isinstance(raw, dict):
        raise ValueError(
            f"{ctx}（{name}）clean 必须是 op 名数组或属性级映射 "
            f"{{属性: [op, ...]}}（REQ-D-005；数组形式等价 {{\"_name\": [...]}}）")
    allowed = set(otype.properties) | {"_name"}
    items: list[tuple[str, tuple]] = []
    for prop, ops in raw.items():
        if prop not in allowed:
            raise ValueError(
                f"{ctx}（{name}）clean 属性 '{prop}' 不在对象属性声明 "
                f"{sorted(otype.properties)} 内（_name 表示名称列）")
        ops_t = _parse_op_list(ops, ctx, f"clean.{prop}")
        if ops_t:
            items.append((prop, ops_t))
    flat = {op for _, ops in items for op in ops}
    for tok in sorted(flat):
        try:
            _clean_ops.validate_op(tok, "clean")
        except ValueError as e:
            raise ValueError(
                f"{ctx}（{name}）clean op '{tok}' 非法：{e}"
                f"（clean 在 Python 侧执行，可用 {sorted(CLEAN_RULE_NAMES)}）")
    return tuple(items)


def _parse_transform_map(raw, otype: ObjectType, ctx: str, name: str,
                         structured: bool) -> tuple:
    """REQ-D-009：transform 声明 → 属性级映射 ((属性, (op, ...)), ...)。

    仅结构化源（source）绑定支持——transform 编译进 SQL 投影，手写 source_sql
    请在上游完成变换。校验（AC-6）：未知 op 硬失败；仅允许带 sql_template 的
    transform/any 层声明式 op；带参 op（"op:param"）的参数必须在该 op 的
    param_enum 白名单内（REQ-D-011，自由文本硬失败防注入）。
    """
    if raw is None:
        return ()
    if not structured:
        raise ValueError(
            f"{ctx}（{name}）transform 仅支持结构化源（source）绑定；"
            f"手写 source_sql 的变换请在上游 SQL 内完成")
    if not isinstance(raw, dict):
        raise ValueError(
            f"{ctx}（{name}）transform 必须是属性级映射 {{属性: [op, ...]}}（REQ-D-009）")
    allowed = set(otype.properties) | {"_name"}
    items: list[tuple[str, tuple]] = []
    for prop, ops in raw.items():
        if prop not in allowed:
            raise ValueError(
                f"{ctx}（{name}）transform 属性 '{prop}' 不在对象属性声明 "
                f"{sorted(otype.properties)} 内（_name 表示名称列）")
        ops_t = _parse_op_list(ops, ctx, f"transform.{prop}")
        if ops_t:
            items.append((prop, ops_t))
    if not items:
        return ()
    flat = {op for _, ops in items for op in ops}
    avail = _clean_ops.transform_layer_names()
    for tok in sorted(flat):
        try:
            _clean_ops.validate_op(tok, "transform")
        except ValueError as e:
            raise ValueError(
                f"{ctx}（{name}）transform op '{tok}' 非法：{e}"
                f"（transform 在 SQL 投影内编译，可用 {sorted(avail)}；REQ-D-009 AC-6）")
    return tuple(items)


# ----------------------------------------------------------------------
# bindings
# ----------------------------------------------------------------------
# REQ-P-033：归一 JOIN 声明校验（v1 校验一致——build_sql 仍是唯一执行源）。
# 规则：① 声明的每个归一 JOIN 必须在 build_sql 中逐字存在（表/别名/ON 条件一致）；
#       ② 声明的 select 必须以输出列名（as）进入投影（"sel AS as" 或列名恰为 as）；
#       ③ build_sql 中出现 raw_name 等值 JOIN 实体表（归一形态）而未声明 → 硬失败；
#       ④ 业务 JOIN（时间窗/自连接等非 raw_name 等值）不受影响。
_NORM_KEYS = ("as", "table", "alias", "on", "select")
_RAW_EQUAL_JOIN_RE = re.compile(
    r"(?:LEFT\s+)?JOIN\s+(obj_\w+)\s+(\w+)\s+ON\s+\w+\.raw_name\s*=", re.I)


def _validate_normalize(items: list, build_sql: str, ctx: str) -> tuple:
    sql_flat = re.sub(r"\s+", " ", build_sql)
    declared: set[tuple[str, str]] = set()
    used_as: set[str] = set()
    for j, it in enumerate(items):
        nctx = f"{ctx} normalize[{j}]"
        if not isinstance(it, dict):
            raise ValueError(f"{nctx} 必须是映射 {{as,table,alias,on,select}}")
        _require(it, _NORM_KEYS, nctx)
        table, alias, as_ = it["table"], it["alias"], it["as"]
        on_flat = re.sub(r"\s+", " ", str(it["on"]))
        if not str(table).startswith("obj_"):
            raise ValueError(f"{nctx} table '{table}' 必须是 obj_* 语义表")
        if as_ in used_as:
            raise ValueError(f"{nctx} 输出列名 '{as_}' 重复（REQ-P-033）")
        used_as.add(as_)
        if f"JOIN {table} {alias} ON {on_flat}" not in sql_flat:
            raise ValueError(
                f"{nctx} 声明的归一 JOIN（{table} {alias} ON {on_flat}）"
                f"在 build_sql 中不存在或不一致（REQ-P-033：声明与实现须一致）")
        select = re.sub(r"\s+", " ", str(it["select"]))
        sel_col = select.split(".")[-1]
        if f"{select} AS {as_}" not in sql_flat and sel_col != as_:
            raise ValueError(
                f"{nctx} select '{select}' 未以输出列 '{as_}' 进入 build_sql "
                f"投影（REQ-P-033）")
        declared.add((table, alias))
    for table, alias in _RAW_EQUAL_JOIN_RE.findall(build_sql):
        if (table, alias) not in declared:
            raise ValueError(
                f"{ctx} build_sql 存在未声明的归一 JOIN（{table} {alias}）"
                f"——请在 normalize 段声明（REQ-P-033：归一必须声明化）")
    return tuple(dict(it) for it in items)


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
        typed_raw: tuple = ()
        projections: tuple = ()
        # REQ-D-012：禁止 transform 声明 split 类 op——任何形式的列拆分不得进入声明层。
        # 出路：复合列在 source_sql 中于上游拆分（声明层仍 1:1），或显式 composite 降级（REQ-D-013）。
        split_ops = _transform_split_ops(b.get("transform"))
        if split_ops:
            raise ValueError(
                f"{ctx}（{name}）transform 声明含 split 类 op：{sorted(set(split_ops))}"
                f"（REQ-D-012：禁止列拆分进入声明层；请在 source_sql 中于上游完成拆分，"
                f"保持一源列一属性）")
        # ---- REQ-D-009：transform 属性级映射（仅结构化源，SQL 投影内 TRY_CAST 前注入）----
        # 解析先于结构化源编译（编译期需按属性注入 op 表达式）
        transform_map = _parse_transform_map(
            b.get("transform"), otype, ctx, name,
            structured=("source" in b))
        # ---- REQ-D-010：on_cast_error 三态（解析先于编译——fail/quarantine 决定 CAST 渲染）----
        on_cast_error = _parse_on_cast_error_map(
            b.get("on_cast_error"), otype, ctx, name,
            structured=("source" in b))
        # ---- REQ-D-014：null_policy 属性级空值策略（allow/reject/quarantine）----
        null_policy = _parse_null_policy(b.get("null_policy"), otype, ctx, name)
        # ---- REQ-D-015：业务键去重（key.columns + on_conflict）----
        dedup_key, dedup_conflict = _parse_dedup_key(b.get("key"), otype, ctx, name)
        if "source" in b:
            sql, table, typed_raw, projections = _compile_structured_source(
                b["source"], otype, ctx, transform_map, on_cast_error)
            source_sql, source_table = sql, table
            # optional_columns：可选源列名（缺列降级类型化 NULL，不硬失败，鲁棒性 B5-01）
            opt_cols = b.get("optional_columns", [])
            if not isinstance(opt_cols, list) or not all(
                    isinstance(c, str) and c for c in opt_cols):
                raise ValueError(f"{ctx}（{name}）optional_columns 必须是源列名字符串数组")
            raw_cols = set(b["source"]["columns"].values())
            unknown_opt = [c for c in opt_cols if c not in raw_cols]
            if unknown_opt:
                raise ValueError(
                    f"{ctx}（{name}）optional_columns {unknown_opt} 不在 source.columns "
                    f"源列 {sorted(raw_cols)} 内（只能声明实际投影的源列）")
            optional_raw = tuple(opt_cols)
        else:
            optional_raw = ()
            if "optional_columns" in b:
                raise ValueError(
                    f"{ctx}（{name}）optional_columns 仅支持结构化源（source）绑定；"
                    f"手写 source_sql 的缺列请用 optional:true 整表降级")
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

        # ---- REQ-D-005：clean 属性级作用域 ----
        # 数组形式向后兼容（= {"_name": [...]}，_name 为名称列约定别名）；
        # 映射形式 {属性: [op, ...] | op}，属性按声明顺序、规则按声明顺序执行。
        clean_map = _parse_clean_map(b.get("clean"), otype, ctx, name)
        clean = tuple(dict.fromkeys(
            op for _, ops in clean_map for op in ops))   # 去重平集（既有字段口径）

        obj_out[name] = ObjectBinding(
            object=name, source_sql=source_sql, source_table=source_table,
            clean=clean, optional=bool(b.get("optional", False)),
            typed_raw=typed_raw, projections=projections,
            optional_raw=optional_raw, clean_map=clean_map,
            transform=transform_map, on_cast_error=on_cast_error,
            null_policy=null_policy, dedup_key=dedup_key,
            dedup_on_conflict=dedup_conflict)

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
        norm = b.get("normalize", [])
        if not isinstance(norm, list):
            raise ValueError(f"{ctx} normalize 必须是列表（REQ-P-033）")
        normalize = _validate_normalize(norm, b["build_sql"], ctx)
        # REQ-D-013 AC-3：composite 属性不得作为归一 JOIN 的 ON 键（编译期可判定——
        # 复合列拆分前不可参与实体关联，否则归并/画像语义全部失真）
        for j, it in enumerate(normalize):
            obj_name = str(it["table"])[4:]   # table 形如 obj_<name>
            ot = obj_map.get(obj_name)
            if ot is None or not ot.composite_props:
                continue
            on_cols = set(re.findall(
                rf"\b{re.escape(str(it['alias']))}\.(\w+)", str(it["on"])))
            hit = sorted(on_cols & set(ot.composite_props))
            if hit:
                raise ValueError(
                    f"{ctx} normalize[{j}] 的 ON 条件引用对象 '{obj_name}' 的复合列 "
                    f"{hit}（REQ-D-013 AC-3：composite 属性不得作为归一 JOIN 键——"
                    f"复合列拆分前不可参与实体关联）")
        link_out[name] = LinkBinding(link=name, build_sql=b["build_sql"],
                                     normalize=normalize)

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
                required: bool,
                allowed_dimensions: set | None = None) -> dict[str, RuleSpec]:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"ontology 声明文件缺失：{path}")
        return {}
    data = _read_json(path)
    from core import functions as fn_mod
    # REQ-G-011：合法维度集来自 dimensions.json 声明（缺省回落内置 5 维）
    _dims = allowed_dimensions if allowed_dimensions is not None else set(DEFAULT_DIMENSIONS)

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
        if dimension and dimension not in _dims:
            raise ValueError(f"{ctx}（{rid}）dimension='{dimension}' 非法，"
                             f"须在 dimensions.json 声明，可用 {sorted(_dims)}（REQ-G-011）")
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
            exclusive_group=r.get("exclusive_group") or None,
            primary_rule=bool(r.get("primary_rule", False)),
            overlap_resolution=r.get("overlap_resolution") or None,
            excludes=tuple(r.get("excludes") or ()),
            zero_is_clean=bool(r.get("zero_is_clean", False)),
        )
        ol = out[rid].overlap_resolution
        if ol not in ALLOWED_OVERLAP_RESOLUTION:
            raise ValueError(
                f"{ctx}（{rid}）overlap_resolution='{ol}' 非法，"
                f"允许 {sorted(x for x in ALLOWED_OVERLAP_RESOLUTION if x is not None)}")
    # REQ-025：装载尾双向核对（AC2 单向声明→告警不硬失败）
    import warnings
    for rid, spec in out.items():
        for other in spec.excludes:
            if other in out and rid not in out[other].excludes:
                warnings.warn(
                    f"rules excludes 单向声明：{rid} 排除 {other}，但 {other} 未声明排除 {rid}"
                    "（AC2 装载告警）", stacklevel=2)
    # REQ-025：exclusive_group 内最多一个 primary_rule=True
    group_primary: dict[str, str] = {}
    for rid, spec in out.items():
        if not spec.exclusive_group or not spec.primary_rule:
            continue
        g = spec.exclusive_group
        if g in group_primary:
            raise ValueError(
                f"rules exclusive_group='{g}' 同时存在多个 primary_rule："
                f"{group_primary[g]} 与 {rid}（组内最多一个）")
        group_primary[g] = rid
    return out
