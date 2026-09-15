"""
core/impact.py
S5-F3 本体变更影响面分析（确定性、实时计算、无大模型依赖）。

红线（S5 PRD §7）：
  R1 宁可多报不可漏报：结构化能确定的引用记 certain；动态 SQL / 自由文本里
     只能做词法命中的引用一律计入并标 uncertain（E3-3），绝不静默丢弃。
  R2 已生成线索单独成类，并据 state.sqlite 处置真值标出「已固证/已立案」。
  D10 计算失败 ≠ 无影响：每类独立 try，失败记入 failures 且该类
     unavailable；全失败 status=failed，调用方必须显式警示，禁止显示「无影响」。
  R3 只报告影响，不做任何线索自动迁移（本模块纯只读）。

五类产物（§F3.2）：clues（已生成线索）/ rules / views / tables（已物化表）/
functions；另出 dynamic_refs（bindings 动态 SQL 等不确定引用，§8.4 文案）。

引擎不直接读 DuckDB / state.sqlite：物化表名清单、线索产物、处置状态由
路由层装配后传入（与 core 纯离线、store 层开库纪律一致）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

COVERAGE_NOTE = (
    "影响面分析覆盖规则/视图/函数/物化表/已生成线索，"
    "不覆盖动态 SQL 中的未解析引用"
)

# 已固证口径：已固证 + 已立案（进入举证/司法程序，证据内容更不可静默失效）
LOCKED_STATUSES = frozenset({"已固证", "已立案"})

# 通用 (文件 → (条目段, 主键键, 变更类型))
_SECTION_SPECS: dict[str, tuple[str, str, str]] = {
    "rules": ("rules", "id", "rule_removed"),
    "functions": ("functions", "name", "function_removed"),
    "views": ("views", "name", "view_removed"),
    "actions": ("actions", "name", "action_removed"),
    "states": ("states", "name", "state_removed"),
    "jians": ("jians", "name", "jian_removed"),
    "dimensions": ("dimensions", "name", "dimension_removed"),
    "scoring": ("dimensions", "name", "scoring_dimension_removed"),
    "thresholds": ("thresholds", "rule", "threshold_removed"),
    "verify_playbooks": ("playbooks", "id", "playbook_removed"),
    "derived_properties": ("properties", "name", "derived_property_removed"),
}
# 字典键集合段 (文件 → (段, 变更类型))
_DICT_SECTION_SPECS: dict[str, tuple[str, str]] = {
    "data_elements": ("elements", "data_element_removed"),
    "enum_space": ("space", "enum_group_removed"),
}
# 影响面允许分析的本体文件（llm_policy 永不进入，S5 R6）
ANALYZABLE_FILES = frozenset(
    set(_SECTION_SPECS) | set(_DICT_SECTION_SPECS)
    | {"objects", "links", "bindings", "policies", "case_knowledge"}
)

_ASCII = re.compile(r"^[\x21-\x7e]+$")


def _word_hit(blob: str, token: str) -> bool:
    """词法命中：ASCII token 要求边界（phone 不命中 smartphone）；CJK 直接子串。"""
    if not token:
        return False
    if _ASCII.match(token):
        return re.search(r"(?<![A-Za-z0-9_])" + re.escape(token)
                         + r"(?![A-Za-z0-9_])", blob) is not None
    return token in blob


# ----------------------------------------------------------------------
# 变更提取（old → new 的删除/改名；新增不改下游，不产生影响面）
# ----------------------------------------------------------------------
@dataclass
class Change:
    kind: str
    ref: str
    tokens: list[str]
    label: str
    extra: dict = field(default_factory=dict)


def _prop_names(props: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(props, dict):
        out.update(k for k in props if isinstance(k, str))
    return out


def _extract_changes(file: str, old: dict, new: dict) -> list[Change]:
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    changes: list[Change] = []

    if file == "objects":
        old_objs = {o.get("name"): o for o in old.get("objects", [])
                    if isinstance(o, dict) and o.get("name")}
        new_objs = {o.get("name"): o for o in new.get("objects", [])
                    if isinstance(o, dict) and o.get("name")}
        for name, oo in old_objs.items():
            if name not in new_objs:
                changes.append(Change(
                    "object_removed", name, [name, f"obj_{name}"],
                    f"删除对象类型 {name}", {"object": name}))
                continue
            old_props = _prop_names(oo.get("properties"))
            new_props = _prop_names(new_objs[name].get("properties"))
            for p in sorted(old_props - new_props):
                changes.append(Change(
                    "object_property_removed", f"{name}.{p}",
                    [f"{name}.{p}", f"obj_{name}.{p}", p],
                    f"删除对象属性 {name}.{p}",
                    {"object": name, "property": p}))
        return changes

    if file == "links":
        old_links = {o.get("name"): o for o in old.get("links", [])
                     if isinstance(o, dict) and o.get("name")}
        new_links = {o.get("name"): o for o in new.get("links", [])
                     if isinstance(o, dict) and o.get("name")}
        for name, ol in old_links.items():
            if name not in new_links:
                changes.append(Change(
                    "link_removed", name, [name, f"lnk_{name}"],
                    f"删除关系类型 {name}", {"link": name}))
                continue
            old_props = _prop_names(ol.get("properties"))
            new_props = _prop_names(new_links[name].get("properties"))
            for p in sorted(old_props - new_props):
                changes.append(Change(
                    "link_property_removed", f"{name}.{p}",
                    [f"{name}.{p}", f"lnk_{name}.{p}", p],
                    f"删除关系属性 {name}.{p}",
                    {"link": name, "property": p}))
        return changes

    if file in _SECTION_SPECS:
        section, idkey, kind = _SECTION_SPECS[file]
        old_items = old.get(section, [])
        new_items = new.get(section, [])
        old_ids = {it.get(idkey) for it in old_items
                   if isinstance(it, dict) and it.get(idkey) is not None}
        new_ids = {it.get(idkey) for it in new_items
                   if isinstance(it, dict) and it.get(idkey) is not None}
        # 对象属性特殊：objects 已单列；其余条目删除
        for rid in sorted(old_ids - new_ids, key=lambda x: str(x)):
            changes.append(Change(kind, str(rid), [str(rid)],
                                  f"删除条目 {rid}", {"id": rid}))
        # objects 系文件（dimensions/scoring）的属性变化不逐条追踪（值配置变更，
        # 非结构性删除）；jians/规则等条目的删除是主要风险面。
        return changes

    if file in _DICT_SECTION_SPECS:
        section, kind = _DICT_SECTION_SPECS[file]
        old_map = old.get(section, {})
        new_map = new.get(section, {})
        if isinstance(old_map, dict) and isinstance(new_map, dict):
            for k in sorted(set(old_map) - set(new_map)):
                changes.append(Change(kind, str(k), [str(k)],
                                      f"删除 {k}", {"id": k}))
        return changes

    # 其余文件（bindings/policies/case_knowledge）：不做结构化 diff，
    # 调用方一般不开放通用提案；返回空（不猜测）。
    return changes


# ----------------------------------------------------------------------
# 入参
# ----------------------------------------------------------------------
@dataclass
class ImpactInput:
    file: str
    old_doc: dict
    new_doc: dict
    pack_dir: Path
    # 物化表名清单（obj_*/lnk_*）；None=存储不可读（tables 类 unavailable）
    materialized_tables: list[str] | None = None
    # 最新线索产物（dict 列表）；None=产物读取失败（clues 类 unavailable），
    # 空列表=无已生成线索（正常空）
    clues: list[dict] | None = None
    clues_version: int | None = None
    # clue_id → 处置状态（state.sqlite 真值）
    clue_status: dict[str, str] = field(default_factory=dict)
    # 全域/行业层存在性（§14-2：改全域层显示影响 N 个案件）
    standard_layer: dict | None = None


def _read_pack_json(pack_dir: Path, name: str):
    """返回 (doc, error)：缺失=(None,None)；解析失败=(None, 原因串)。"""
    path = Path(pack_dir) / f"{name}.json"
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        return None, f"{name}.json 读取失败：{type(e).__name__}: {e}"[:200]


def _new_item(category: str, item_id: str, *, title: str = "",
              certainty: str = "certain", refs: Iterable[str] = (),
              via: str = "", status: str | None = None,
              solidified: bool = False, kind: str = "") -> dict:
    return {
        "category": category,
        "kind": kind or category.rstrip("s"),
        "id": str(item_id),
        "title": title or str(item_id),
        "certainty": certainty,
        "refs": sorted(set(refs)),
        "via": via,
        "status": status,
        "solidified": solidified,
    }


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def compute_impact(inp: ImpactInput) -> dict:
    """计算影响面。永不抛异常：任何扫描失败降级为该类 unavailable（D10）。"""
    failures: list[dict] = []
    categories = {
        "clues": {"status": "ok", "items": []},
        "rules": {"status": "ok", "items": []},
        "views": {"status": "ok", "items": []},
        "tables": {"status": "ok", "items": []},
        "functions": {"status": "ok", "items": []},
    }
    dynamic_refs: list[dict] = []

    try:
        changes = _extract_changes(inp.file, inp.old_doc, inp.new_doc)
    except Exception as e:  # noqa: BLE001 —— D10：diff 失败不可显示无影响
        return _result(inp.file, [], categories, failures, dynamic_refs,
                       fatal=f"变更解析失败：{type(e).__name__}: {e}")

    if changes:
        try:
            _scan_all(inp, changes, categories, failures, dynamic_refs)
        except Exception as e:  # noqa: BLE001 —— 兜底：引擎自身缺陷也不得静默
            failures.append({"category": "all",
                             "reason": f"影响面计算异常：{type(e).__name__}: {e}"})

    return _result(inp.file, changes, categories, failures, dynamic_refs)


def _scan_all(inp: ImpactInput, changes: list[Change], categories,
              failures, dynamic_refs) -> None:
    pack_dir = Path(inp.pack_dir)

    rules_doc, err = _read_pack_json(pack_dir, "rules")
    if err:
        _fail(categories, failures, "rules", err)
        rules_doc = {"rules": []}
    elif rules_doc is None:
        rules_doc = {"rules": []}

    funcs_doc, err = _read_pack_json(pack_dir, "functions")
    if err:
        _fail(categories, failures, "functions", err)
        funcs_doc = {"functions": []}
    elif funcs_doc is None:
        funcs_doc = {"functions": []}

    views_doc, err = _read_pack_json(pack_dir, "views")
    if err:
        _fail(categories, failures, "views", err)
        views_doc = {"views": []}
    elif views_doc is None:
        views_doc = {"views": []}

    objects_doc, _ = _read_pack_json(pack_dir, "objects")
    bindings_doc, bind_err = _read_pack_json(pack_dir, "bindings")
    thresholds_doc, _ = _read_pack_json(pack_dir, "thresholds")
    playbooks_doc, _ = _read_pack_json(pack_dir, "verify_playbooks")

    # 删除数据元 → 展开为被绑定对象属性的变更令牌（DE 是属性的 mask/clean 载体）
    bound_prop_tokens: list[tuple[str, Change]] = []
    de_changes = [c for c in changes if c.kind == "data_element_removed"]
    if de_changes and isinstance(objects_doc, dict):
        for de in de_changes:
            de_id = de.ref
            for obj in objects_doc.get("objects", []):
                if not isinstance(obj, dict):
                    continue
                oname = obj.get("name")
                for pname, pspec in (obj.get("properties") or {}).items():
                    if isinstance(pspec, dict) and pspec.get("data_element") == de_id:
                        prop_ref = f"{oname}.{pname}"
                        bound_prop_tokens.append((prop_ref, de))

    # ---- 函数（结构化 requires/inputs = 确定；SQL 词法命中 = 不确定）----
    if categories["functions"]["status"] == "ok":
        try:
            _scan_functions(changes, bound_prop_tokens,
                            funcs_doc.get("functions", []),
                            categories["functions"]["items"])
        except Exception as e:  # noqa: BLE001
            _fail(categories, failures, "functions",
                  f"函数扫描异常：{type(e).__name__}: {e}")

    certain_funcs = {it["id"] for it in categories["functions"]["items"]
                     if it["certainty"] == "certain"}
    uncertain_funcs = {it["id"] for it in categories["functions"]["items"]
                       if it["certainty"] == "uncertain"}

    # ---- 规则 ----
    if categories["rules"]["status"] == "ok":
        try:
            _scan_rules(changes, rules_doc.get("rules", []),
                        thresholds_doc, playbooks_doc,
                        certain_funcs, uncertain_funcs,
                        categories["rules"]["items"])
        except Exception as e:  # noqa: BLE001
            _fail(categories, failures, "rules",
                  f"规则扫描异常：{type(e).__name__}: {e}")

    # ---- 视图 ----
    if categories["views"]["status"] == "ok":
        try:
            _scan_views(changes, views_doc.get("views", []),
                        categories["views"]["items"])
        except Exception as e:  # noqa: BLE001
            _fail(categories, failures, "views",
                  f"视图扫描异常：{type(e).__name__}: {e}")

    # ---- 已物化表 ----
    try:
        if inp.materialized_tables is None:
            _fail(categories, failures, "tables",
                  "案件 DuckDB 不可读，无法核对已物化表（不代表无影响）")
        else:
            _scan_tables(changes, inp.materialized_tables,
                         categories["tables"]["items"])
    except Exception as e:  # noqa: BLE001
        _fail(categories, failures, "tables",
              f"物化表扫描异常：{type(e).__name__}: {e}")

    # ---- 已生成线索 ----
    try:
        if inp.clues is None:
            _fail(categories, failures, "clues",
                  "线索产物读取/解析失败，无法核对已生成线索（不代表无影响）")
        else:
            _scan_clues(changes, inp.clues, inp.clue_status,
                        rules_doc.get("rules", []),
                        {it["id"] for it in categories["rules"]["items"]
                         if it["certainty"] == "certain"},
                        categories["clues"]["items"])
    except Exception as e:  # noqa: BLE001
        _fail(categories, failures, "clues",
              f"线索扫描异常：{type(e).__name__}: {e}")

    # ---- 动态 SQL 引用（bindings.build_sql/source_sql，§8.4 不确定）----
    if bind_err:
        # bindings 读不出 = 无法确定动态引用，计入 failures（R1：不静默）
        failures.append({"category": "dynamic_sql", "reason": bind_err})
    elif isinstance(bindings_doc, dict):
        try:
            _scan_bindings(changes, bindings_doc, dynamic_refs)
        except Exception as e:  # noqa: BLE001
            failures.append({"category": "dynamic_sql",
                             "reason": f"动态 SQL 扫描异常：{type(e).__name__}: {e}"})


def _fail(categories, failures, category: str, reason: str) -> None:
    categories[category]["status"] = "unavailable"
    failures.append({"category": category, "reason": str(reason)[:200]})


# ----------------------------------------------------------------------
# 分类扫描
# ----------------------------------------------------------------------
def _match_refs(tokens: list[str], blob: str) -> list[str]:
    return [t for t in tokens if _word_hit(blob, t)]


def _scan_functions(changes, bound_prop_tokens, functions, items) -> None:
    for f in functions:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        fname = f["name"]
        requires = f.get("requires") if isinstance(f.get("requires"), dict) else {}
        req_objs = set(requires.get("objects") or [])
        req_links = set(requires.get("links") or [])
        req_props = requires.get("props") if isinstance(
            requires.get("props"), dict) else {}
        inputs = f.get("inputs") if isinstance(f.get("inputs"), list) else []
        sql = f.get("sql") if isinstance(f.get("sql"), str) else ""

        certain: list[str] = []
        uncertain: list[str] = []
        for c in changes:
            k = c.kind
            if k == "object_removed":
                obj = c.extra["object"]
                if obj in req_objs or f"obj_{obj}" in inputs:
                    certain.append(c.ref)
                elif sql and _word_hit(sql, f"obj_{obj}"):
                    uncertain.append(c.ref)
            elif k == "link_removed":
                lnk = c.extra["link"]
                if lnk in req_links or f"lnk_{lnk}" in inputs:
                    certain.append(c.ref)
                elif sql and _word_hit(sql, f"lnk_{lnk}"):
                    uncertain.append(c.ref)
            elif k in ("object_property_removed", "link_property_removed"):
                owner_key = "object" if k == "object_property_removed" else "link"
                owner, prop = c.extra[owner_key], c.extra["property"]
                if owner in (req_props or {}) and prop in (req_props.get(owner) or []):
                    certain.append(c.ref)
                elif sql and _match_refs(c.tokens, sql):
                    uncertain.append(c.ref)
            elif k == "function_removed":
                continue
            else:
                # 通用条目删除：只在 SQL 文本里出现 → 不确定
                if sql and _match_refs(c.tokens, sql):
                    uncertain.append(c.ref)

        # DE 删除 → 绑定属性令牌：requires.props 确定，SQL 不确定
        for prop_ref, de in bound_prop_tokens:
            owner, prop = prop_ref.split(".", 1)
            if owner in (req_props or {}) and prop in (req_props.get(owner) or []):
                certain.append(f"{de.ref}（绑定属性 {prop_ref}）")
            elif sql and (_word_hit(sql, prop) or _word_hit(sql, de.ref)):
                uncertain.append(f"{de.ref}（绑定属性 {prop_ref}）")

        if certain:
            items.append(_new_item(
                "functions", fname, title=f.get("title") or fname,
                certainty="certain", refs=certain,
                via="requires/inputs 结构化引用", kind="function"))
        elif uncertain:
            items.append(_new_item(
                "functions", fname, title=f.get("title") or fname,
                certainty="uncertain", refs=uncertain,
                via="SQL 文本词法命中（动态引用，无法确定）", kind="function"))


def _scan_rules(changes, rules, thresholds_doc, playbooks_doc,
                certain_funcs, uncertain_funcs, items) -> None:
    existing = {r.get("id"): r for r in rules if isinstance(r, dict)}
    seen: set[str] = set()

    def add(rid, certainty, refs, via, title=""):
        key = f"{rid}:{certainty}"
        if key in seen:
            # 合并引用
            for it in items:
                if it["id"] == rid and it["certainty"] == certainty:
                    it["refs"] = sorted(set(it["refs"]) | set(refs))
                    return
        seen.add(key)
        items.append(_new_item(
            "rules", rid, title=title or rid, certainty=certainty,
            refs=refs, via=via, kind="rule"))

    for r in rules:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        rid = r["id"]
        func = r.get("function")
        blob = json.dumps(r, ensure_ascii=False)
        refs_certain: list[str] = []
        refs_uncertain: list[str] = []
        for c in changes:
            k = c.kind
            if k == "function_removed" and func == c.ref:
                refs_certain.append(c.ref)
            elif k == "dimension_removed" and r.get("dimension") == c.ref:
                refs_certain.append(c.ref)
            elif k == "jian_removed" and c.ref in (r.get("jian_types") or []):
                refs_certain.append(c.ref)
            elif k == "rule_removed" and c.ref in (r.get("excludes") or []):
                refs_certain.append(c.ref)
            elif k in ("object_removed", "link_removed",
                       "object_property_removed", "link_property_removed"):
                if func in certain_funcs:
                    refs_certain.append(c.ref)
                elif _match_refs(c.tokens, blob):
                    refs_uncertain.append(c.ref)
            elif k == "rule_removed":
                continue
            else:
                if _match_refs(c.tokens, blob):
                    refs_uncertain.append(c.ref)
        if func in uncertain_funcs and not refs_certain:
            # 依赖的函数 SQL 可能引用 → 规则本身不确定
            for c in changes:
                if c.kind in ("object_removed", "link_removed",
                              "object_property_removed", "link_property_removed"):
                    refs_uncertain.append(c.ref)
        if refs_certain:
            add(rid, "certain", refs_certain,
                "function/dimension/jian_types 结构化引用",
                title=r.get("title") or rid)
        if refs_uncertain:
            add(rid, "uncertain", refs_uncertain,
                "规则文本/参数词法命中或依赖函数的动态 SQL",
                title=r.get("title") or rid)

    # 被删规则 → thresholds / verify_playbooks / 其他规则的 excludes
    removed_rules = [c for c in changes if c.kind == "rule_removed"]
    if removed_rules:
        thresholds = (thresholds_doc or {}).get("thresholds", []) \
            if isinstance(thresholds_doc, dict) else []
        for th in thresholds:
            if isinstance(th, dict):
                for c in removed_rules:
                    if th.get("rule") == c.ref:
                        items.append(_new_item(
                            "rules", f"threshold:{c.ref}",
                            title=f"阈值策略 {c.ref}", certainty="certain",
                            refs=[c.ref], via="thresholds.json 的 rule 引用",
                            kind="threshold"))
        playbooks = (playbooks_doc or {}).get("playbooks", []) \
            if isinstance(playbooks_doc, dict) else []
        for pb in playbooks:
            if isinstance(pb, dict):
                for c in removed_rules:
                    if (pb.get("match") or {}).get("rule_id") == c.ref:
                        items.append(_new_item(
                            "rules", f"playbook:{pb.get('id')}",
                            title=f"核查剧本 {pb.get('id')}",
                            certainty="certain", refs=[c.ref],
                            via="verify_playbooks match.rule_id",
                            kind="playbook"))


def _scan_views(changes, views, items) -> None:
    for v in views:
        if not isinstance(v, dict) or not v.get("name"):
            continue
        base = v.get("base_object")
        props = set(v.get("properties") or [])
        refs: list[str] = []
        for c in changes:
            if c.kind == "object_removed" and base == c.extra.get("object"):
                refs.append(c.ref)
            elif c.kind == "object_property_removed" \
                    and base == c.extra.get("object") \
                    and c.extra.get("property") in props:
                refs.append(c.ref)
        if refs:
            items.append(_new_item(
                "views", v["name"], title=v.get("description") or v["name"],
                certainty="certain", refs=refs,
                via=f"base_object={base} 的投影列", kind="view"))


def _scan_tables(changes, tables: list[str], items) -> None:
    table_set = set(tables)
    for c in changes:
        if c.kind == "object_removed":
            t = f"obj_{c.extra['object']}"
            if t in table_set:
                items.append(_new_item(
                    "tables", t, certainty="certain", refs=[c.ref],
                    via="已物化语义表，变更后需重跑 BUILD", kind="table"))
        elif c.kind == "link_removed":
            t = f"lnk_{c.extra['link']}"
            if t in table_set:
                items.append(_new_item(
                    "tables", t, certainty="certain", refs=[c.ref],
                    via="已物化语义链接表，变更后需重跑 BUILD", kind="table"))
        elif c.kind in ("object_property_removed", "link_property_removed"):
            prefix = "obj_" if c.kind == "object_property_removed" else "lnk_"
            t = f"{prefix}{c.extra.get('object') or c.extra.get('link')}"
            if t in table_set:
                items.append(_new_item(
                    "tables", t, certainty="certain", refs=[c.ref],
                    via="已物化语义表的列受影响，变更后需重跑 BUILD",
                    kind="table"))


def _scan_clues(changes, clues, status_map: dict, rules,
                certain_rule_ids: set, items) -> None:
    rule_by_id = {r.get("id"): r for r in rules if isinstance(r, dict)}
    for clue in clues:
        if not isinstance(clue, dict):
            continue
        cid = clue.get("clue_id") or clue.get("id")
        if not cid:
            continue
        blob = json.dumps(clue, ensure_ascii=False, default=str)
        detail = clue.get("detail") if isinstance(clue.get("detail"), dict) else {}
        rule_id = detail.get("rule_id")
        jian_types = set(clue.get("jian_types") or [])
        certain: list[str] = []
        uncertain: list[str] = []
        for c in changes:
            k = c.kind
            if k == "rule_removed" and rule_id == c.ref:
                certain.append(c.ref)
            elif k in ("rule_removed",) :
                pass
            elif k == "jian_removed" and c.ref in jian_types:
                certain.append(c.ref)
            elif k == "object_removed":
                obj = c.extra["object"]
                if _word_hit(blob, f"obj_{obj}") or \
                        f'"对象类型": "{obj}"' in blob or \
                        (rule_id and rule_id in certain_rule_ids):
                    certain.append(c.ref)
            elif k == "link_removed":
                if _word_hit(blob, f"lnk_{c.extra['link']}") or \
                        (rule_id and rule_id in certain_rule_ids):
                    certain.append(c.ref)
            elif k in ("object_property_removed", "link_property_removed"):
                if rule_id and rule_id in certain_rule_ids:
                    certain.append(c.ref)
                elif _match_refs(c.tokens, blob):
                    uncertain.append(c.ref)
            elif k == "dimension_removed" and rule_id:
                rd = rule_by_id.get(rule_id)
                if rd and rd.get("dimension") == c.ref:
                    certain.append(c.ref)
                elif _match_refs(c.tokens, blob):
                    uncertain.append(c.ref)
            else:
                if _match_refs(c.tokens, blob):
                    uncertain.append(c.ref)
        status = status_map.get(cid) or clue.get("status") or ""
        locked = status in LOCKED_STATUSES
        if certain:
            items.append(_new_item(
                "clues", cid, title=clue.get("title") or str(cid),
                certainty="certain", refs=certain,
                via="rule_id/语义表结构化引用", status=status,
                solidified=locked, kind="clue"))
        elif uncertain:
            items.append(_new_item(
                "clues", cid, title=clue.get("title") or str(cid),
                certainty="uncertain", refs=uncertain,
                via="线索内容词法命中（无法确定）", status=status,
                solidified=locked, kind="clue"))


def _scan_bindings(changes, bindings_doc: dict, dynamic_refs) -> None:
    blob = json.dumps(bindings_doc, ensure_ascii=False)
    for c in changes:
        if c.kind == "object_removed":
            token = f"obj_{c.extra['object']}"
            if _word_hit(blob, token):
                dynamic_refs.append({
                    "file": "bindings.json", "token": token,
                    "ref": c.ref,
                    "reason": "绑定 SQL（source_sql/build_sql）中出现该语义表名"})
        elif c.kind == "link_removed":
            token = f"lnk_{c.extra['link']}"
            if _word_hit(blob, token):
                dynamic_refs.append({
                    "file": "bindings.json", "token": token,
                    "ref": c.ref,
                    "reason": "链接 build_sql 中出现该语义表名"})


# ----------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------
def _result(file, changes, categories, failures, dynamic_refs,
            fatal: str | None = None) -> dict:
    if fatal:
        failures = [{"category": "all", "reason": fatal}, *failures]
        for c in categories.values():
            c["status"] = "unavailable"

    n_failures = sum(1 for c in categories.values()
                     if c["status"] == "unavailable")
    if fatal or n_failures == len(categories):
        status = "failed"
    elif n_failures or any(f["category"] == "dynamic_sql" for f in failures):
        status = "partial"
    else:
        status = "complete"

    totals = {name: len(categories[name]["items"])
              for name in categories}
    totals["solidified_clues"] = sum(
        1 for it in categories["clues"]["items"] if it["solidified"])
    totals["uncertain"] = sum(
        1 for cat in categories.values()
        for it in cat["items"] if it["certainty"] == "uncertain")
    totals["dynamic_refs"] = len(dynamic_refs)
    totals["total"] = sum(len(cat["items"]) for cat in categories.values())

    return {
        "file": file,
        "status": status,
        "changes": [{"kind": c.kind, "ref": c.ref, "label": c.label}
                    for c in changes],
        "categories": categories,
        "dynamic_refs": dynamic_refs,
        "failures": failures,
        "totals": totals,
        "coverage_note": COVERAGE_NOTE,
    }


# ----------------------------------------------------------------------
# 影响面指纹（E4-3：提案创建后下游发生变化 → 发布前要求重新评估）
# ----------------------------------------------------------------------
_FINGERPRINT_FILES = (
    "objects", "links", "rules", "functions", "views", "bindings",
    "jians", "dimensions", "scoring", "thresholds", "verify_playbooks",
    "actions", "states", "data_elements", "enum_space",
    "derived_properties", "case_knowledge",
)


def impact_fingerprint(pack_dir: Path, *, materialized_tables: list[str] | None,
                       clues_artifact: Path | None) -> str:
    """下游依赖状态指纹：扫描文件字节 + 物化表清单 + 线索产物字节。"""
    import hashlib
    h = hashlib.sha1()
    base = Path(pack_dir)
    for name in _FINGERPRINT_FILES:
        p = base / f"{name}.json"
        if p.is_file():
            h.update(name.encode())
            try:
                h.update(p.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
    h.update(repr(sorted(materialized_tables or [])).encode())
    if clues_artifact is not None and Path(clues_artifact).is_file():
        try:
            h.update(Path(clues_artifact).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:16]
