"""
core/functions.py
Ontology Function 层：只读、类型化、可枚举的计算单元（Palantir Function 裁剪版）。

与 Action 的边界：
  - Function 只读：SQL 实现强制 SELECT/WITH 白名单（DDL/DML 关键词硬拦截），
    py 实现为注册的可信代码；输出派生结果，绝不改对象——红线 3；
  - Action 可写：状态迁移、创建决策对象等副作用走 core.action_executor。

声明在 ontology/<pack>/functions.json；py 实现在本模块 FUNCTION_IMPLS 注册，
加载时校验 impl_ref 存在（未知名硬失败）。
"""
from __future__ import annotations

import re
from typing import Callable

import duckdb

from core.ontology_loader import load_pack

# SQL 只读白名单：首词必须是 SELECT/WITH；语句中出现 DDL/DML 关键词即拒绝
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|DETACH|COPY|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _assert_readonly(sql: str, name: str) -> None:
    head = sql.strip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise ValueError(f"function '{name}' 的 SQL 必须以 SELECT/WITH 开头（只读约束）")
    if _FORBIDDEN_SQL.search(sql):
        raise ValueError(f"function '{name}' 的 SQL 含写操作/DDL 关键词，违反只读约束")


def _is_missing_semantic_table(exc: BaseException) -> bool:
    """降级判定：CatalogException 且缺失的是 obj_*/lnk_* 语义表（数据源未接入）。"""
    if not isinstance(exc, duckdb.CatalogException):
        return False
    msg = str(exc)
    return "does not exist" in msg and bool(
        re.search(r"\b(?:obj_|lnk_)[a-z_]+", msg))


def _is_structural_degrade(exc: BaseException) -> bool:
    """REQ-G-003：结构降级判据（加宽版）。

    语义表缺失（CatalogException，表不存在）**或** 语义表在但列缺失/绑定失败
    （BinderException，数据源接入但 schema 不符）——只要引用到 obj_*/lnk_* 语义层，
    一律降级零命中并留痕，而非让整条规则崩掉。其余异常（真实 bug/语法错）照抛。
    """
    msg = str(exc)
    if not re.search(r"\b(?:obj_|lnk_)[a-z_]+", msg):
        return False
    if isinstance(exc, (duckdb.CatalogException, duckdb.BinderException)):
        return True
    return False


# ----------------------------------------------------------------------
# SQL 模板参数（{{param}}）：规则 rules.json 的 params 经此安全注入 SQL
# ----------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
PARAM_TYPES = {"integer", "decimal", "date", "boolean", "string"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sql_placeholders(sql: str) -> set[str]:
    """提取 SQL 模板中的全部占位符名。"""
    return set(_PLACEHOLDER.findall(sql))


def check_param_value(name: str, spec: dict, value, ctx: str) -> None:
    """参数值类型 + enum 白名单校验（装载期校验默认值、运行期校验入参，同一决策点）。"""
    ptype = spec.get("type", "string")
    if ptype == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{ctx} 参数 '{name}' 应为 integer，得到 {value!r}")
    elif ptype == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{ctx} 参数 '{name}' 应为 decimal，得到 {value!r}")
    elif ptype == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{ctx} 参数 '{name}' 应为 boolean，得到 {value!r}")
    elif ptype == "date":
        if not (isinstance(value, str) and _DATE_RE.match(value)):
            raise ValueError(f"{ctx} 参数 '{name}' 应为 ISO date(YYYY-MM-DD)，得到 {value!r}")
    else:  # string：仅允许 enum 白名单取值（自由文本一律拒绝，防注入）
        allowed = spec.get("enum")
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(f"{ctx} 参数 '{name}' 为 string 类型必须声明 enum 白名单"
                             f"（自由文本不接受，防 SQL 注入）")
        if value not in allowed:
            raise ValueError(f"{ctx} 参数 '{name}'={value!r} 不在 enum 白名单 {allowed}")


def _render_literal(spec: dict, value) -> str:
    ptype = spec.get("type", "string")
    if ptype == "integer":
        return str(int(value))
    if ptype == "decimal":
        return repr(float(value))
    if ptype == "boolean":
        return "TRUE" if value else "FALSE"
    if ptype == "date":
        return f"DATE '{value}'"
    return "'" + str(value).replace("'", "''") + "'"


def render_sql_template(sql: str, params_spec: dict, merged: dict, ctx: str) -> str:
    """把 {{param}} 占位渲染为类型化字面量。占位符与 parameters 双向核对（硬失败）。"""
    in_sql = sql_placeholders(sql)
    declared = set(params_spec)
    missing = in_sql - declared
    if missing:
        raise ValueError(f"{ctx} SQL 占位符未在 parameters 声明：{sorted(missing)}")
    unused = declared - in_sql
    if unused:
        raise ValueError(f"{ctx} parameters 已声明但 SQL 未使用：{sorted(unused)}")

    def sub(m: re.Match) -> str:
        n = m.group(1)
        if n not in merged:
            raise ValueError(f"{ctx} 参数 '{n}' 无默认值且调用未提供")
        check_param_value(n, params_spec[n], merged[n], ctx)
        return _render_literal(params_spec[n], merged[n])

    return _PLACEHOLDER.sub(sub, sql)


# ----------------------------------------------------------------------
# py 实现注册表（functions.json 的 impl_ref 指向这里的键）
# ----------------------------------------------------------------------
FUNCTION_IMPLS: dict[str, Callable] = {}


def register_function(name: str):
    def deco(fn: Callable) -> Callable:
        FUNCTION_IMPLS[name] = fn
        return fn
    return deco


# ---- 通讯维度：通话频次突增 ----
@register_function("call_frequency_spike")
def _call_frequency_spike(store, params: dict) -> dict:
    import statistics
    threshold = int(params.get("absolute_threshold", 30))
    pairs = store.query(
        "SELECT caller_raw, callee_raw, COUNT(*) AS c FROM obj_call "
        "GROUP BY caller_raw, callee_raw ORDER BY c DESC"
    )
    total_pairs = len(pairs)
    all_parties = set()
    for r in pairs:
        all_parties.add(r["caller_raw"])
        all_parties.add(r["callee_raw"])
    if not pairs:
        return {"hit": False, "basis": "无通话记录", "pairs": [],
                "diagnostics": {"is_degraded": True, "degrade_reason": "无 obj_call 数据",
                                "total_pairs": 0, "unique_parties": 0,
                                "median_value": 0, "threshold_used": 0}}
    top, rest = pairs[0], pairs[1:]
    if rest:
        median = statistics.median(r["c"] for r in rest)
        hit = median > 0 and top["c"] >= 2 * median
        basis = (f"{top['caller_raw']}→{top['callee_raw']} 通话 {top['c']} 次，"
                 f"为其他对端常态中位数 {median} 的 {top['c'] / median:.1f} 倍")
        diag = {"is_degraded": False, "degrade_reason": None,
                "total_pairs": total_pairs, "unique_parties": len(all_parties),
                "median_value": median,
                "threshold_used": f"2×常态中位数（= {2 * median}）"}
    else:
        median = 0
        hit = top["c"] >= threshold
        basis = (f"{top['caller_raw']}→{top['callee_raw']} 单一对端通话 "
                 f"{top['c']} 次（无其他对端可比，按绝对频次判据 ≥{threshold} 次）")
        diag = {"is_degraded": True,
                "degrade_reason": "只有一个通话对端（其他对端未入库），中位数判据不可用 → 降级到绝对频次阈值；建议补全量通话对端清单后重跑",
                "total_pairs": total_pairs, "unique_parties": len(all_parties),
                "median_value": 0,
                "threshold_used": f"绝对频次阈值 absolute_threshold = {threshold}"}
    return {"hit": hit, "basis": basis, "diagnostics": diag,
            "pairs": [{"主体": r["caller_raw"], "对端": r["callee_raw"], "次数": r["c"]}
                      for r in pairs[:5]]}


# ---- 通讯维度补充：通话对端覆盖诊断（全量对照前置检查）----
@register_function("call_pair_coverage")
def _call_pair_coverage(store, params: dict) -> dict:
    import statistics
    min_peers = int(params.get("min_peer_count", 3))
    rows = store.query(
        "SELECT caller_raw, callee_raw, COUNT(*) AS c FROM obj_call "
        "GROUP BY caller_raw, callee_raw ORDER BY c DESC"
    )
    # 按 caller 分组 → 每个 caller 的对端集合
    by_caller: dict[str, list[dict]] = {}
    for r in rows:
        by_caller.setdefault(r["caller_raw"], []).append(r)
    callers_report = []
    for caller, peers in sorted(by_caller.items()):
        peer_cnt = len(peers)
        call_cnts = [p["c"] for p in peers]
        med = statistics.median(call_cnts) if len(call_cnts) > 1 else call_cnts[0]
        top_pair = peers[0]
        # 缺少对端数
        missing_peer_need = max(0, min_peers - peer_cnt)
        callers_report.append({
            "caller": caller,
            "peer_count": peer_cnt,
            "unique_callees": [p["callee_raw"] for p in peers],
            "calls_per_peer_median": med,
            "top_pair": {"callee": top_pair["callee_raw"], "times": top_pair["c"]},
            "missing_peers_to_benchmark": missing_peer_need,
            "is_single_pair": peer_cnt == 1,
            "benchmark_status": ("✅ 对端充足（≥{0}）" if peer_cnt >= min_peers else
                                 f"⚠️ 对端不足（{peer_cnt}/{min_peers}，缺 {missing_peer_need} → call_frequency_spike 已降级）"),
        })
    total_unique_parties = len({r["caller_raw"] for r in rows} | {r["callee_raw"] for r in rows})
    any_degraded = any(c["is_single_pair"] for c in callers_report)
    return {
        "summary": {
            "total_pairs": len(rows),
            "total_unique_parties": total_unique_parties,
            "caller_count": len(by_caller),
            "degraded_callers": sum(1 for c in callers_report if c["is_single_pair"]),
            "min_peer_benchmark": min_peers,
        },
        "callers": callers_report,
        "recommendation": (
            "无降级：call_frequency_spike 结果可用" if not any_degraded else
            f"有 {sum(1 for c in callers_report if c['is_single_pair'])} 个主体仅单一对端通话，"
            "说明其通话全量清单未入库（常见漏采集：手机 SIM 卡 2、办公座机、社交 App 通话记录）。"
            "建议：① 补充运营商详单或全量 App 通话记录；② 入库后重跑 init_duckdb + 全管线，"
            "call_frequency_spike 会自动从「绝对阈值降级模式」升级为「2×中位数常态判据」。"
        ),
    }


# ---- 用间：五间交叉等级（语义代理表非空即命中）----
# REQ-G-013：对象→间类映射不再硬编码于此处，改由 objects.json/links.json 的
# `jian`/`jian_source` 字段声明（loader 校验五间枚举）。**红线**：交叉等级规则
# （单源=观察/双源=线索/三源=可立案依据候选）与间类展示顺序 _JIAN_ORDER 保持硬编码，不进配置。
_JIAN_ORDER = ["因间", "内间", "反间", "死间", "生间"]
# 在册但语义层未建模的数据源（诚实暴露缺口，不充数）——tipoff/osint 已建模则从缺口移除
_UNMODELED: dict[str, list[str]] = {}


def _jian_entries(pack: str) -> list[tuple[str, str, str]]:
    """从案件包声明收集五间数据源 → [(语义表名, 间类, 数据源展示名), ...]。

    obj_<name>/lnk_<name> 的 jian 非空才纳入；jian_source 缺省回落对象/链接 title。
    装载失败/精简测试包无声明 → 空列表（不硬编码业务表名，缺口如实呈现）。
    """
    try:
        from core.ontology_loader import load_pack
        spec = load_pack(pack)
    except Exception:
        return []
    entries: list[tuple[str, str, str]] = []
    for o in spec.objects:
        if o.jian:
            entries.append((f"obj_{o.name}", o.jian, o.jian_source or o.title))
    for l in spec.links:
        if l.jian:
            entries.append((f"lnk_{l.name}", l.jian, l.jian_source or l.title))
    return entries


@register_function("jian_cross_level")
def _jian_cross_level(store, params: dict) -> dict:
    entries = _jian_entries(params.get("pack", "default"))
    hits: dict[str, list[str]] = {}
    for table, jian, src in entries:
        try:
            n = store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception:
            n = 0
        if n:
            hits.setdefault(jian, []).append(f"{src}→{table}({n}行)")
    # 计算总数据源集合（含未建模缺口提示）
    src_by_jian: dict[str, list[str]] = {}
    for _t, jn, s in entries:
        src_by_jian.setdefault(jn, []).append(s)
    for jn, extras in _UNMODELED.items():
        src_by_jian.setdefault(jn, []).extend(extras)
    n = len(hits)
    # 红线：等级规则硬编码，不读配置
    level = "可立案依据候选" if n >= 3 else ("线索" if n == 2 else "观察")
    rows = [
        {"间": j, "数据源": src_by_jian.get(j, []),
         "依据": hits.get(j, []), "命中": j in hits,
         "缺口": _UNMODELED.get(j, [])}
        for j in _JIAN_ORDER
    ]
    return {"rows": rows, "命中间类": sorted(hits), "交叉等级": level,
            "规则": "单源=观察 → 双源=线索 → 三源=可立案依据候选"}


# ---- 内间：举报线索与已知证据交叉 ----
@register_function("tipoff_cross_reference")
def _tipoff_cross_reference(store, params: dict) -> dict:
    rows = store.query("SELECT * FROM obj_tipoff")
    if not rows:
        return {"summary": {"total": 0, "by_person": {}, "high_priority": []},
                "recommendation": "obj_tipoff 为空（举报材料未入库/仅有空 schema 占位）→ 内间仍为缺口。接入方式见项目记忆：init_duckdb L2 空表兜底已就绪，放入 data/举报材料.parquet 后重跑 python -m scripts.init_duckdb 即可。",
                "hits": []}
    # 已在案三类证据的人员集合
    has_account: set[str] = {r["raw_name"] for r in store.query(
        "SELECT DISTINCT owner_raw AS raw_name FROM lnk_owns")}
    has_bid_org: set[str] = set()
    for r in store.query(
        "SELECT DISTINCT o.raw_name AS raw_name FROM lnk_involved_in i "
        "JOIN obj_org o ON o.org_id = i.org_id"):
        has_bid_org.add(r["raw_name"])
    has_org_link: set[str] = {r["raw_name"] for r in store.query(
        "SELECT raw_name FROM obj_org WHERE legal_rep IS NOT NULL OR relation IS NOT NULL")}

    by_person: dict[str, dict] = {}
    for r in rows:
        target = r["target_raw"] or "(未注明被举报人)"
        entry = by_person.setdefault(target, {
            "target": target,
            "tip_count": 0,
            "tip_types": set(),
            "reporters": set(),
            "contents": [],
            "evidence_matches": {"has_account": target in has_account,
                                 "is_org_or_linked": target in has_org_link,
                                 "has_bid_org": target in has_bid_org},
        })
        entry["tip_count"] += 1
        if r["title"]: entry["tip_types"].add(r["title"])
        if r["reporter_raw"]: entry["reporters"].add(r["reporter_raw"])
        if r["content_raw"] and len(entry["contents"]) < 3:
            entry["contents"].append(r["content_raw"])
    # 标记优先级：≥2 类已在案证据独立支撑 = 高
    list_form = []
    for entry in by_person.values():
        m = entry["evidence_matches"]
        match_count = sum(1 for v in m.values() if v)
        if match_count == 0:
            priority = "观察（仅内间）"
        elif match_count == 1:
            priority = "线索（内间+1 类他间）"
        else:
            priority = "可立案依据候选（内间+{} 类独立证据互证）".format(match_count)
        entry["priority"] = priority
        entry["match_count"] = match_count
        entry["tip_types"] = sorted(entry["tip_types"])
        entry["reporters"] = sorted(entry["reporters"])
        entry["evidence_matches"] = m  # keep plain
        list_form.append(entry)
    list_form.sort(key=lambda e: (-e["match_count"], -e["tip_count"]))
    high_priority = [e["target"] for e in list_form if e["match_count"] >= 2]
    return {
        "summary": {
            "total_tipoffs": len(rows),
            "target_count": len(by_person),
            "high_priority": high_priority,
            "by_person_tip_counts": {e["target"]: e["tip_count"] for e in list_form},
        },
        "hits": list_form,
        "recommendation": (
            f"高优先级目标 {len(high_priority)} 个："
            + (", ".join(high_priority) if high_priority else "（暂无）")
            + "。建议：对高优先级目标首先推进法定立案程序；对 match_count=1 的补充一类他间证据即可升格。"
        ),
    }


# ---- 关系维度：工商登记利益关联（R5，REQ-024 知识包参数化）----
def load_case_knowledge(pack: str = "default") -> dict:
    """加载 ontology/<pack>/case_knowledge.json；无知识包时返回空骨架（零命中不报错）。

    REQ-G-016：知识包存在时必须带 schema_version=2（与其余 ontology 声明同源）；
    版本不符/缺失硬失败，防止旧版知识包被静默装载。文件缺失仍回落空骨架（config_missing）。
    """
    import json
    from core.ontology_loader import PACK_ROOT, SCHEMA_VERSION
    p = PACK_ROOT / pack / "case_knowledge.json"
    if not p.exists():
        return {"knowledge_version": None, "subject_aliases": {},
                "relation_assertions": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"case_knowledge.json schema_version={data.get('schema_version')}，"
            f"期望 {SCHEMA_VERSION}（REQ-G-016）；请升级知识包声明")
    return data


@register_function("org_interest_links")
def _org_interest_links(store, params: dict) -> dict:
    """工商利益关联：法人/关联人命中案件知识包中的主体/关系人即候选。

    人名只来自 case_knowledge.json（subject_aliases + 未过期 relation_assertions），
    functions.json/规则代码中不出现任何人名（REQ-024 AC1）；过期断言自动排除。
    """
    from datetime import date
    kn = load_case_knowledge(params.get("pack", "default"))
    today = date.today().isoformat()
    assertions = kn.get("relation_assertions", [])
    valid = [a for a in assertions
             if not a.get("valid_until") or str(a["valid_until"]) >= today]
    persons: set[str] = set(kn.get("subject_aliases", {}).keys())
    for aliases in kn.get("subject_aliases", {}).values():
        persons.update(aliases or [])
    persons.update(a.get("to", "") for a in valid)
    persons.discard("")
    rel_types = set(params.get("relation_types") or [])

    try:
        orgs = store.query("SELECT raw_name, legal_rep, relation FROM obj_org")
    except Exception:
        return {"rows": [], "knowledge_version": kn.get("knowledge_version")}

    rows = []
    for o in orgs:
        fields = [str(o.get("legal_rep") or ""), str(o.get("relation") or "")]
        matched = sorted(p for p in persons if any(p in f for f in fields))
        if not matched:
            continue
        sources = sorted({a.get("source", "") for a in valid
                          if a.get("from") == o.get("raw_name")
                          and a.get("to") in matched
                          and (not rel_types or a.get("type") in rel_types)})
        if rel_types and not sources:
            continue
        rows.append({
            "raw_name": o.get("raw_name"),
            "legal_rep": o.get("legal_rep"),
            "relation": o.get("relation"),
            "matched_person": matched,
            "knowledge_sources": sources,
            "knowledge_version": kn.get("knowledge_version"),
        })
    return {"rows": rows, "knowledge_version": kn.get("knowledge_version")}


# ---- 资金链路：两跳过桥（SQL 轨，与图库 Cypher 轨互为校验）----
@register_function("overpass_two_hop")
def _overpass_two_hop(store, params: dict) -> dict:
    from core.graph import overpass_two_hop_sql
    paths = overpass_two_hop_sql(store)
    return {"rows": [p.to_dict() for p in paths]}


# ----------------------------------------------------------------------
# 执行器
# ----------------------------------------------------------------------
class FunctionExecutor:
    """按 functions.json 声明执行只读计算。"""

    def __init__(self, store, pack: str = "default", access=None, health=None):
        self.store = store
        self.pack = pack
        # REQ-009：access=None → system 旁路（既有调用行为不变）
        from core.access import system_context
        self.access = access if access is not None else system_context()
        from core.policy import PolicyEngine
        self.policy = PolicyEngine(pack)
        # REQ-G-010：运行诊断（None → NullRunHealth，既有调用零行为变化）
        from core.run_health import get_health
        self.health = get_health(health)

    def _specs(self) -> dict:
        return load_pack(self.pack).functions

    def catalog(self) -> list[dict]:
        """可发现的函数目录（MCP function_list 消费）。"""
        return [
            {"name": f.name, "title": f.title, "inputs": list(f.inputs),
             "output_type": f.output_type, "impl": f.impl,
             "parameters": f.parameters, "description": f.description,
             "readonly": True}
            for f in self._specs().values()
        ]

    def invoke(self, name: str, params: dict | None = None) -> dict:
        specs = self._specs()
        if name not in specs:
            raise KeyError(f"未注册的 Function：{name}，可用 {sorted(specs)}")
        spec = specs[name]
        # REQ-010：函数输入对象/链接的策略检查（fail-closed，system 旁路）
        for tbl in spec.inputs:
            if tbl.startswith("obj_"):
                self.policy.check_object(self.access, tbl[4:])
            elif tbl.startswith("lnk_"):
                self.policy.check_link(self.access, tbl[4:])
        merged = {k: v.get("default") for k, v in spec.parameters.items()
                  if isinstance(v, dict) and "default" in v}
        merged.update(params or {})
        params_used = {k: merged[k] for k in spec.parameters}

        try:
            if spec.impl == "sql":
                sql = spec.sql
                if spec.parameters:
                    sql = render_sql_template(
                        sql, spec.parameters, merged, ctx=f"function '{name}'")
                _assert_readonly(sql, name)
                rows = self.store.query(sql)
                return {"function": name, "output_type": spec.output_type,
                        "rows": rows, "readonly": True, "params_used": params_used}
            result = FUNCTION_IMPLS[spec.impl_ref](self.store, merged)
            out = {"function": name, "output_type": spec.output_type,
                   "result": result, "readonly": True}
            # REQ-G-003：py 实现可自报降级（配置缺失/引用解析失败导致无法计算）
            if isinstance(result, dict) and result.get("degraded"):
                out["degraded"] = True
                out["degraded_reason"] = result.get("degraded_reason") or "py 函数自报降级"
                self.health.record(
                    "function_empty_degraded", "warning",
                    source=f"function:{name}",
                    reason=out["degraded_reason"], impl=spec.impl_ref)
            return out
        except (duckdb.CatalogException, duckdb.BinderException) as e:
            if not _is_structural_degrade(e):
                raise
            # 降级（REQ-020 golden / REQ-G-003 加宽）：函数消费的 obj_*/lnk_* 语义表
            # 不存在（Catalog）或表在但列缺失/绑定失败（Binder）——数据源未接入或 schema 不符，
            # 零命中返回而非报错；带 degraded 标记可审计，并落运行诊断。
            reason = str(e).splitlines()[0][:120]
            self.health.record(
                "function_empty_degraded", "warning",
                source=f"function:{name}", reason=reason,
                exc=type(e).__name__)
            return {"function": name, "output_type": spec.output_type,
                    "rows": [] if spec.impl == "sql" else None,
                    "result": None if spec.impl == "sql" else {"hit": False, "pairs": []},
                    "readonly": True, "degraded": True,
                    "degraded_reason": reason,
                    "params_used": params_used}


def invoke_function(store, name: str, params: dict | None = None,
                    pack: str = "default") -> dict:
    """模块级便捷入口。"""
    return FunctionExecutor(store, pack).invoke(name, params)


# REQ-G-021：地点标准化/同框 Function。实现独立在 core/geo.py（纯离线、只读、无网络）；
# 在此注册进 FUNCTION_IMPLS，供 functions.json 的 impl_ref 挂钩（loader 装载期校验存在）。
from core import geo as _geo  # noqa: E402

register_function("location_colocated")(_geo.location_colocated)
