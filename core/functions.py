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

import inspect
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


# ---- 通讯维度：单一对端通话高频 ----
@register_function("call_frequency_spike")
def _call_frequency_spike(store, params: dict, ctx=None) -> dict:
    import statistics
    threshold = int(params.get("absolute_threshold", 30))
    tbl = ctx.table("call") if ctx is not None else "obj_call"
    pairs = store.query(
        f"SELECT caller_raw, callee_raw, COUNT(*) AS c FROM {tbl} "
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
                 f"{top['c']} 次（无其他对端可比，按绝对频次判据 ≥{threshold} 次；"
                 f"无可比对端，不构成突增判定）")
        diag = {"is_degraded": True,
                "degrade_reason": "只有一个通话对端（其他对端未入库），中位数判据不可用 → 降级到绝对频次阈值；建议补全量通话对端清单后重跑",
                "total_pairs": total_pairs, "unique_parties": len(all_parties),
                "median_value": 0,
                "threshold_used": f"绝对频次阈值 absolute_threshold = {threshold}"}
    return {"hit": hit, "basis": basis, "diagnostics": diag,
            "subject": top["caller_raw"],
            # 输出列名用语义属性名（与 call 对象的 name_property 口径一致），
            # 不用中文业务列名——后者靠猜语义，换领域即失效。
            "pairs": [{"caller_raw": r["caller_raw"],
                       "callee_raw": r["callee_raw"], "times": r["c"]}
                      for r in pairs[:5]]}


# ---- 通讯维度补充：通话对端覆盖诊断（全量对照前置检查）----
@register_function("call_pair_coverage")
def _call_pair_coverage(store, params: dict, ctx=None) -> dict:
    import statistics
    min_peers = int(params.get("min_peer_count", 3))
    tbl = ctx.table("call") if ctx is not None else "obj_call"
    rows = store.query(
        f"SELECT caller_raw, callee_raw, COUNT(*) AS c FROM {tbl} "
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
# REQ-G-013/R5：对象→间类映射与间类顺序不再硬编码，改由 jians.json 声明。
# **红线**：交叉等级规则（单源=观察/双源=线索/三源=可立案依据候选）的映射关系
# （min_independent_sources 1/2/3）保持硬编码，不进配置；名称可由 cross_levels 配置。
# 在册但语义层未建模的数据源（诚实暴露缺口，不充数）——tipoff/osint 已建模则从缺口移除
_UNMODELED: dict[str, list[str]] = {}


def _wujian(pack: str):
    from core.wujian import load_wujian
    return load_wujian(pack)


def _jian_order(pack: str) -> list[str]:
    """P6：间类展示顺序来自已挂载五间词汇（packs/wujian）；无包返回 []。"""
    wj = _wujian(pack)
    return wj.jian_order if wj else []


def _cross_level_name(n: int, pack: str) -> str:
    """P6：升格名来自五间词汇 cross_levels；无包回落通用名。映射（1/2/3）硬编码。"""
    wj = _wujian(pack)
    if wj is not None:
        name = wj.cross_level_name(n)
        if name:
            return name
    return {1: "观察", 2: "线索", 3: "可立案依据候选"}[n]


def count_independent(sources: list[str],
                      related_pairs: list[dict] | None = None) -> int:
    """R9-3：计算独立源数量。

    related_pairs 中声明的同源对（a/b）合并计为 1；
    未在 related_pairs 中的源各自独立计数。
    """
    if not sources:
        return 0
    related_pairs = related_pairs or []
    # 并查集：相关源合并
    parent = {s: s for s in sources}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    src_set = set(sources)
    for pair in related_pairs:
        a, b = pair.get("a"), pair.get("b")
        if a in src_set and b in src_set:
            union(a, b)
    return len({find(s) for s in sources})


def _jian_entries(pack: str) -> list[tuple[str, str, str, str]]:
    """从五间词汇映射收集五间数据源
    → [(语义表名, 间类, 数据源展示名, 对象/链接类型名), ...]。

    P6：以 packs/wujian 的 source_object_types 为唯一权威反查（支持一对多，
    如 org 同时属因间/死间），数据源展示名读词汇 source_names（原 jian_source）。
    映射中的对象/链接必须在 OntologyPack 中已声明（缺失跳过并留缺口）。
    五间包未安装/装载失败 → 空列表（交叉页整体降级为缺口）。
    """
    wj = _wujian(pack)
    if wj is None:
        return []
    try:
        from core.ontology_loader import load_pack
        spec = load_pack(pack)
    except Exception:
        return []
    obj_map = {o.name: o for o in spec.objects}
    lnk_map = {l.name: l for l in spec.links}
    entries: list[tuple[str, str, str, str]] = []
    for jd in wj.jians:
        jian = jd.name
        for t in jd.source_object_types:
            if t in obj_map:
                o = obj_map[t]
                entries.append((f"obj_{o.name}", jian,
                                wj.source_name_for(t, o.title), o.name))
            elif t in lnk_map:
                l = lnk_map[t]
                entries.append((f"lnk_{l.name}", jian,
                                wj.source_name_for(t, l.title), l.name))
            # 未声明的对象/链接类型：跳过（缺口由 _UNMODELED 展示）
    return entries


@register_function("jian_cross_level")
def _jian_cross_level(store, params: dict) -> dict:
    pack = params.get("pack", "default")
    entries = _jian_entries(pack)
    hits: dict[str, list[str]] = {}
    # 方案 B 行集口径：命中同时保留结构化明细（source/table/obj_name/n），
    # 供用间线索适配器挂「表级汇总行」做溯源；依据字符串字段保留兼容旧消费方。
    hits_detail: dict[str, list[dict]] = {}
    hit_sources: list[str] = []
    for table, jian, src, obj_name in entries:
        try:
            n = store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception:
            n = 0
        if n:
            hits.setdefault(jian, []).append(f"{src}→{table}({n}行)")
            hits_detail.setdefault(jian, []).append(
                {"source": src, "table": table,
                 "obj_name": obj_name, "n": int(n)})
            hit_sources.append(obj_name)
    # 计算总数据源集合（含未建模缺口提示）
    src_by_jian: dict[str, list[str]] = {}
    for _t, jn, s, _o in entries:
        src_by_jian.setdefault(jn, []).append(s)
    for jn, extras in _UNMODELED.items():
        src_by_jian.setdefault(jn, []).extend(extras)
    # R9：独立源数——按五间词汇 source_independence.related_pairs
    # 把声明同源的数据源合并；无包/无声明时每个对象类型独立
    wj = _wujian(pack)
    related_pairs = ([{"a": a, "b": b} for a, b in wj.related_pairs]
                     if wj is not None else [])
    n = count_independent(sorted(set(hit_sources)), related_pairs)
    # 红线：等级映射（1/2/3）硬编码；名称从五间词汇 cross_levels 读取
    level_n = 3 if n >= 3 else (2 if n == 2 else 1)
    level = _cross_level_name(level_n, pack)
    jian_order = _jian_order(pack)
    rows = []
    for j in jian_order:
        # 每间独立源数：等级判定的真正粒度。
        #
        # 全局独立源数（n）是**全案汇总**，只要数据接得全就恒为 3 级
        # （实测 9 个源 → 可立案依据候选），对单间毫无区分力——用它给
        # 每间贴等级，等于所有间都是最高级，规则"单源=观察"永不生效。
        #
        # 五间方法论的粒度是**每一间**：该间有几个独立数据源在支撑。
        # 内间只有举报材料 → 单源 → 观察（不是命题）；
        # 生间有通话+轨迹+流水 → 三源 → 可立案依据候选。
        j_srcs: list[str] = []
        for d in hits_detail.get(j, []):
            if isinstance(d, dict) and d.get("obj_name"):
                j_srcs.append(str(d["obj_name"]))
        j_n = count_independent(sorted(set(j_srcs)), related_pairs)
        j_level = _cross_level_name(3 if j_n >= 3 else (2 if j_n == 2 else 1),
                                    pack)
        rows.append({
            "间": j, "数据源": src_by_jian.get(j, []),
            "依据": hits.get(j, []),
            "命中明细": hits_detail.get(j, []),
            "命中": j in hits,
            "缺口": _UNMODELED.get(j, []),
            # 本间判定（产出分流的依据：观察 vs 线索）
            "本间独立源数": j_n,
            "本间独立数据源": sorted(set(j_srcs)),
            "本间交叉等级": j_level,
            # 是否够格成为命题：单源=观察，双源及以上=线索
            "够格为线索": j_n >= 2,
        })
    return {"rows": rows, "命中间类": sorted(hits),
            "独立源数": n, "独立数据源": sorted(set(hit_sources)),
            "交叉等级": level,
            "规则": "单源=观察 → 双源=线索 → 三源=可立案依据候选"}


# ---- 内间：举报线索与已知证据交叉 ----
@register_function("tipoff_cross_reference")
def _tipoff_cross_reference(store, params: dict, ctx=None) -> dict:
    tbl_tipoff = ctx.table("tipoff") if ctx is not None else "obj_tipoff"
    lnk_owns = ctx.link("owns") if ctx is not None else "lnk_owns"
    lnk_involved = ctx.link("involved_in") if ctx is not None else "lnk_involved_in"
    tbl_org = ctx.table("org") if ctx is not None else "obj_org"
    rows = store.query(f"SELECT * FROM {tbl_tipoff}")
    if not rows:
        return {"summary": {"total": 0, "by_person": {}, "high_priority": []},
                "recommendation": f"{tbl_tipoff} 为空（举报材料未入库/仅有空 schema 占位）→ 内间仍为缺口。接入方式见项目记忆：init_duckdb L2 空表兜底已就绪，放入 data/举报材料.parquet 后重跑 python -m scripts.init_duckdb 即可。",
                "hits": []}
    # 已在案三类证据的人员集合
    has_account: set[str] = {r["raw_name"] for r in store.query(
        f"SELECT DISTINCT owner_raw AS raw_name FROM {lnk_owns}")}
    has_bid_org: set[str] = set()
    for r in store.query(
        f"SELECT DISTINCT o.raw_name AS raw_name FROM {lnk_involved} i "
        f"JOIN {tbl_org} o ON o.org_id = i.org_id"):
        has_bid_org.add(r["raw_name"])
    has_org_link: set[str] = {r["raw_name"] for r in store.query(
        f"SELECT raw_name FROM {tbl_org} WHERE legal_rep IS NOT NULL OR relation IS NOT NULL")}

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
def load_case_knowledge(pack: str = "default",
                        base_dir: "Path | None" = None) -> dict:
    """加载 ontology/<pack>/case_knowledge.json；无知识包时返回空骨架（零命中不报错）。

    REQ-G-016：知识包存在时必须带 schema_version=2（与其余 ontology 声明同源）；
    版本不符/缺失硬失败，防止旧版知识包被静默装载。文件缺失仍回落空骨架（config_missing）。
    base_dir：案件快照 ontology 根（缺省 None = 模板包，CLI/MCP 行为不变）。
    """
    import json
    from pathlib import Path
    from core.ontology_loader import PACK_ROOT, SCHEMA_VERSION
    root = Path(base_dir) if base_dir else PACK_ROOT
    p = root / pack / "case_knowledge.json"
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
def _org_interest_links(store, params: dict, ctx=None) -> dict:
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

    tbl_org = ctx.table("org") if ctx is not None else "obj_org"
    try:
        orgs = store.query(f"SELECT raw_name, legal_rep, relation FROM {tbl_org}")
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
    subject = rows[0]["matched_person"][0] if rows and rows[0].get("matched_person") else ""
    return {"rows": rows, "subject": subject,
            "knowledge_version": kn.get("knowledge_version")}


# ---- 资金链路：两跳过桥（SQL 轨，与图库 Cypher 轨互为校验）----
@register_function("overpass_two_hop")
def _overpass_two_hop(store, params: dict) -> dict:
    """两跳过桥（SQL 轨）。store 是 ReadOnlyStore，不得访问 execute/conn。

    语义层 lnk_transfers 缺失时不回落直查 L2 源表（REQ-003），返回空集并自报
    degraded —— FunctionExecutor 捕获后落 function_empty_degraded 健康度诊断，
    与其余 py Function 的降级口径一致（不静默、不崩、不绕过红线）。
    """
    from core.graph import has_semantic_flow, overpass_two_hop_sql
    if not has_semantic_flow(store):
        return {"rows": [], "subject": "", "degraded": True,
                "degraded_reason": "语义层 lnk_transfers 缺失；"
                                   "py Function 只读通道不直查 L2 业务源表（REQ-003）"}
    paths = overpass_two_hop_sql(store, allow_unsafe_fallback=False)
    subject = paths[0].source if paths else ""
    return {"rows": [p.to_dict() for p in paths], "subject": subject}


# ---- P4 关系研判：语义层统一图算法（N 跳邻域/共同邻居/路径枚举）----
# 自由文本主体（target/subject_a/subject_b）跟随 location_colocated 先例：
# 不进 functions.json parameters（string 必须 enum），由编排层/镜头透传，
# graph.resolve_subject 内做标识符白名单 + 参数化查询。
from core.graph import (  # noqa: E402
    EDGE_KINDS as _EDGE_KINDS,
    load_node_names as _load_node_names,
    load_semantic_graph as _load_semantic_graph,
    node_type_of as _node_type_of,
    resolve_subject as _resolve_subject,
)


def _graph_query_params(params: dict) -> tuple[int, str]:
    depth = int(params.get("depth", 2))
    if not 1 <= depth <= 3:
        raise ValueError(f"depth 允许 1-3，得到 {depth}")
    edge_kinds = params.get("edge_kinds", "all")
    if edge_kinds not in _EDGE_KINDS:
        raise ValueError(f"edge_kinds={edge_kinds!r}，允许 {_EDGE_KINDS}")
    return depth, edge_kinds


def _node_brief(pk: str, names: dict) -> dict:
    return {"pk": pk, "type": _node_type_of(pk), "name": names.get(pk, pk)}


def _edge_brief(e) -> dict:
    d = e.to_dict()
    d["ref"] = e.ref()
    return d


def _gap_diagnostic(g) -> dict:
    return {
        "engine": "semantic",
        "loaded_links": list(g.loaded_links),
        "gaps": g.gaps,
        "is_degraded": bool(g.gaps),
    }


def _missing_subject(target, g) -> dict:
    return {
        "hit": False,
        "subject": None,
        "nodes": [],
        "edges": [],
        "degraded": True,
        "degraded_reason": f"主体 {target!r} 在语义层实体中不存在（未建档或未归一）",
        "diagnostics": _gap_diagnostic(g),
    }


@register_function("relation_neighborhood")
def _relation_neighborhood(store, params: dict, ctx=None) -> dict:
    """目标主体 N 跳关系圈层（跨资金/通话/持有/中标/同框边，无向 BFS）。"""
    depth, edge_kinds = _graph_query_params(params)
    target = params.get("target") or params.get("target_subject") or ""
    target_type = params.get("target_type", "auto")
    g = _load_semantic_graph(store, ctx=ctx, edge_kinds=edge_kinds)
    subject = _resolve_subject(store, target, target_type, ctx)
    if subject is None:
        return _missing_subject(target, g)
    hops, first_path = g.neighborhood(subject["pk"], depth)
    names = _load_node_names(store, set(hops), ctx)
    tree_edges: dict[tuple, object] = {}
    for pk, chain in first_path.items():
        for e in chain:
            # BFS 树：每个节点只有一条首达链，同一物理边行至多出现一次
            tree_edges.setdefault((e.edge, e.edge_pk), e)
    nodes = [
        {**_node_brief(pk, names), "hops": h}
        for pk, h in sorted(hops.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    diag = _gap_diagnostic(g)
    return {
        "hit": len(hops) > 1,
        "subject": subject,
        "nodes": nodes,
        "edges": [_edge_brief(e) for e in tree_edges.values()],
        "degraded": diag["is_degraded"],
        "degraded_reason": ("部分关系数据源未接入，圈层不完整："
                            + "; ".join(x["link"] for x in g.gaps)) if g.gaps else None,
        "diagnostics": diag,
    }


@register_function("relation_common_neighbors")
def _relation_common_neighbors(store, params: dict, ctx=None) -> dict:
    """两主体的共同邻居（跨类型混合关系圈层的交集）。"""
    _depth, edge_kinds = _graph_query_params(params)
    ta, tb = params.get("subject_a") or "", params.get("subject_b") or ""
    g = _load_semantic_graph(store, ctx=ctx, edge_kinds=edge_kinds)
    sa = _resolve_subject(store, ta, params.get("target_type_a", "auto"), ctx)
    sb = _resolve_subject(store, tb, params.get("target_type_b", "auto"), ctx)
    if sa is None or sb is None:
        missing = ta if sa is None else tb
        out = _missing_subject(missing, g)
        out["subject_a"] = sa
        out["subject_b"] = sb
        return out
    if sa["pk"] == sb["pk"]:
        raise ValueError("subject_a 与 subject_b 指向同一主体，共同邻居无意义")
    common = g.common_neighbors(sa["pk"], sb["pk"])
    pks = set(common) | {sa["pk"], sb["pk"]}
    names = _load_node_names(store, pks, ctx)
    items = [{
        **_node_brief(pk, names),
        "via_a": _edge_brief(ea),
        "via_b": _edge_brief(eb),
    } for pk, (ea, eb) in sorted(common.items())]
    diag = _gap_diagnostic(g)
    return {
        "hit": bool(items),
        "subject_a": sa,
        "subject_b": sb,
        "common": items,
        "count": len(items),
        "degraded": diag["is_degraded"],
        "degraded_reason": ("部分关系数据源未接入："
                            + "; ".join(x["link"] for x in g.gaps)) if g.gaps else None,
        "diagnostics": diag,
    }


@register_function("relation_paths")
def _relation_paths(store, params: dict, ctx=None) -> dict:
    """两主体间 N 跳内简单路径枚举（环剪枝 + max_paths 上限）。"""
    depth, edge_kinds = _graph_query_params(params)
    max_paths = int(params.get("max_paths", 20))
    if not 1 <= max_paths <= 100:
        raise ValueError(f"max_paths 允许 1-100，得到 {max_paths}")
    ta, tb = params.get("subject_a") or "", params.get("subject_b") or ""
    g = _load_semantic_graph(store, ctx=ctx, edge_kinds=edge_kinds)
    sa = _resolve_subject(store, ta, params.get("target_type_a", "auto"), ctx)
    sb = _resolve_subject(store, tb, params.get("target_type_b", "auto"), ctx)
    if sa is None or sb is None:
        missing = ta if sa is None else tb
        out = _missing_subject(missing, g)
        out["subject_a"] = sa
        out["subject_b"] = sb
        out["paths"] = []
        return out
    if sa["pk"] == sb["pk"]:
        raise ValueError("subject_a 与 subject_b 指向同一主体，路径枚举无意义")
    chains = g.paths(sa["pk"], sb["pk"], depth, max_paths)
    pks = {sa["pk"], sb["pk"]}
    for chain in chains:
        for e in chain:
            pks.update((e.src, e.dst))
    names = _load_node_names(store, pks, ctx)
    paths_out = []
    for chain in chains:
        node_seq = [sa["pk"]] + [e.dst for e in chain]
        paths_out.append({
            "length": len(chain),
            "nodes": [_node_brief(pk, names) for pk in node_seq],
            "edges": [_edge_brief(e) for e in chain],
        })
    truncated = len(chains) >= max_paths
    diag = _gap_diagnostic(g)
    diag["truncated_at_max_paths"] = truncated
    return {
        "hit": bool(paths_out),
        "subject_a": sa,
        "subject_b": sb,
        "paths": paths_out,
        "count": len(paths_out),
        "degraded": diag["is_degraded"] or truncated,
        "degraded_reason": (
            ("部分关系数据源未接入："
             + "; ".join(x["link"] for x in g.gaps)) if g.gaps else None)
            or ("路径数达到 max_paths 上限被截断" if truncated else None),
        "diagnostics": diag,
    }


# ----------------------------------------------------------------------
# P5 时间研判镜头：统一时间轴（资金/通话/轨迹事件）
# 见 .trae/documents/研判能力插件化_实施方案_v3.md §4-P5
# ----------------------------------------------------------------------
from datetime import date as _date


def _tl_table(ctx, object_name: str) -> str:
    """表名经 RuntimeContext 派生（换包不崩）；无 ctx 回落 obj_ 前缀。"""
    return ctx.table(object_name) if ctx is not None else f"obj_{object_name}"


def _tl_gap(object_name: str, e: Exception) -> dict:
    return {"object": object_name,
            "reason": f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"}


def _amount_brief(amount) -> str:
    if amount is None:
        return ""
    return f" {float(amount):g} 元"


def _collect_subject_events(store, target: str, ctx=None
                            ) -> tuple[list[dict], list[dict]]:
    """收集目标主体在资金/通话/轨迹三表的全部事件 → (events, gaps)。

    事件统一形态：
      {"type": 资金|通话|轨迹, "src_object": transaction|call|trackpoint,
       "event_pk", "date"(ISO), "role", "brief"}
    缺表/缺列（Catalog/Binder 且指向语义表）= 数据源未接入 → gaps，
    其余异常照抛。
    """
    events: list[dict] = []
    gaps: list[dict] = []

    # ---- 资金 ----
    tbl = _tl_table(ctx, "transaction")
    try:
        rows = store.query(
            f'SELECT txn_id AS event_pk, from_raw, to_raw, '
            f'CAST(amount AS DOUBLE) AS amount, CAST(date AS DATE) AS d '
            f'FROM {tbl} WHERE from_raw = ? OR to_raw = ?',
            (target, target))
    except Exception as e:
        if not _is_structural_degrade(e):
            raise
        gaps.append(_tl_gap("transaction", e))
    else:
        for r in rows:
            if not r["event_pk"] or not r["d"]:
                continue
            if r["from_raw"] == target:
                role, peer = "转出", r["to_raw"]
            else:
                role, peer = "转入", r["from_raw"]
            events.append({
                "type": "transaction", "src_object": "transaction",
                "event_pk": str(r["event_pk"]), "date": r["d"].isoformat(),
                "role": role,
                "brief": f'{role}→{peer}{_amount_brief(r["amount"])}'})

    # ---- 通话 ----
    tbl = _tl_table(ctx, "call")
    try:
        rows = store.query(
            f'SELECT call_id AS event_pk, caller_raw, callee_raw, '
            f'CAST(date AS DATE) AS d FROM {tbl} '
            f'WHERE caller_raw = ? OR callee_raw = ?',
            (target, target))
    except Exception as e:
        if not _is_structural_degrade(e):
            raise
        gaps.append(_tl_gap("call", e))
    else:
        for r in rows:
            if not r["event_pk"] or not r["d"]:
                continue
            if r["caller_raw"] == target:
                role, peer = "主叫", r["callee_raw"]
            else:
                role, peer = "被叫", r["caller_raw"]
            events.append({
                "type": "call", "src_object": "call",
                "event_pk": str(r["event_pk"]), "date": r["d"].isoformat(),
                "role": role, "brief": f"{role}→{peer}"})

    # ---- 轨迹 ----
    tbl = _tl_table(ctx, "trackpoint")
    try:
        rows = store.query(
            f'SELECT track_id AS event_pk, person_raw, location, '
            f'CAST(date AS DATE) AS d FROM {tbl} WHERE person_raw = ?',
            (target,))
    except Exception as e:
        if not _is_structural_degrade(e):
            raise
        gaps.append(_tl_gap("trackpoint", e))
    else:
        for r in rows:
            if not r["event_pk"] or not r["d"]:
                continue
            events.append({
                "type": "trackpoint", "src_object": "trackpoint",
                "event_pk": str(r["event_pk"]), "date": r["d"].isoformat(),
                "role": "出现",
                "brief": f'出现于 {r["location"] or "未知地点"}'})

    events.sort(key=lambda e: (e["date"], e["type"], e["event_pk"]))
    return events, gaps


def _event_type_counts(events: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    return counts


@register_function("timeline_event_sequence")
def _timeline_event_sequence(store, params: dict, ctx=None) -> dict:
    """目标主体跨类型事件序列邻接（统一时间轴，附相邻事件日差）。"""
    target = params.get("target") or params.get("target_subject") or ""
    subject = _resolve_subject(store, target,
                               params.get("target_type", "auto"), ctx)
    if subject is None:
        return {"hit": False, "subject": None, "timeline": [],
                "event_count": 0, "span_days": None, "type_counts": {},
                "degraded": True,
                "degraded_reason": f"主体 {target!r} 在语义层实体中不存在",
                "diagnostics": {"gaps": []}}
    events, gaps = _collect_subject_events(store, subject["name"], ctx)
    timeline: list[dict] = []
    prev: _date | None = None
    for e in events:
        d = _date.fromisoformat(e["date"])
        gap_days = None if prev is None else (d - prev).days
        timeline.append({**e, "gap_days": gap_days})
        prev = d
    span_days = None if not events else (
        _date.fromisoformat(events[-1]["date"])
        - _date.fromisoformat(events[0]["date"])).days
    return {
        "hit": len(events) >= 2,
        "subject": subject,
        "timeline": timeline,
        "event_count": len(events),
        "span_days": span_days,
        "type_counts": _event_type_counts(events),
        "degraded": bool(gaps),
        "degraded_reason": ("部分时间数据源未接入："
                            + "; ".join(x["object"] for x in gaps)) if gaps else None,
        "diagnostics": {"gaps": gaps},
    }


@register_function("timeline_rhythm")
def _timeline_rhythm(store, params: dict, ctx=None) -> dict:
    """目标主体事件周期节奏：间隔统计 + burst_days 内事件聚集簇。"""
    import statistics
    burst_days = int(params.get("burst_days", 3))
    if not 1 <= burst_days <= 14:
        raise ValueError(f"burst_days 允许 1-14，得到 {burst_days}")
    target = params.get("target") or params.get("target_subject") or ""
    subject = _resolve_subject(store, target,
                               params.get("target_type", "auto"), ctx)
    if subject is None:
        return {"hit": False, "subject": None, "bursts": [],
                "event_count": 0, "median_gap_days": None,
                "degraded": True,
                "degraded_reason": f"主体 {target!r} 在语义层实体中不存在",
                "diagnostics": {"gaps": []}}
    events, gaps = _collect_subject_events(store, subject["name"], ctx)
    if len(events) < 2:
        return {"hit": False, "subject": subject, "bursts": [],
                "event_count": len(events), "median_gap_days": None,
                "degraded": True,
                "degraded_reason": "事件不足 2 起，节奏不可计算",
                "diagnostics": {"gaps": gaps}}

    day_seq = [_date.fromisoformat(e["date"]) for e in events]
    intervals = [(b - a).days for a, b in zip(day_seq, day_seq[1:])]
    median_gap = statistics.median(intervals)

    # 聚集簇：相邻事件日差 ≤ burst_days 贪心成团（仅 ≥2 起成簇）。
    # 注意判据是「相邻两两间隔」而非「首末跨度」：日期 0/3/6/9（每步≤3）
    # 会链成跨度 9 天的一簇。span>burst_days 的簇打 chain=True，供下游
    # 区分「单日/窗宽内簇」与「链式长簇」，读图时勿把带宽当窗宽。
    bursts: list[list[dict]] = []
    cur = [events[0]]
    for e, d in zip(events[1:], day_seq[1:]):
        if (d - _date.fromisoformat(cur[-1]["date"])).days <= burst_days:
            cur.append(e)
        else:
            if len(cur) >= 2:
                bursts.append(cur)
            cur = [e]
    if len(cur) >= 2:
        bursts.append(cur)

    def _burst_out(b: list[dict]) -> dict:
        ds = [_date.fromisoformat(x["date"]) for x in b]
        gaps = [(y - x).days for x, y in zip(ds, ds[1:])]
        span = (ds[-1] - ds[0]).days
        return {
            "start": b[0]["date"], "end": b[-1]["date"],
            "event_count": len(b),
            "types": sorted({x["type"] for x in b}),
            "events": b,
            "span_days": span,
            "max_gap_days": max(gaps) if gaps else 0,
            "chain": span > burst_days,
        }

    bursts_out = [_burst_out(b) for b in bursts]
    return {
        "hit": bool(bursts_out),
        "subject": subject,
        "bursts": bursts_out,
        "burst_count": len(bursts_out),
        "event_count": len(events),
        "median_gap_days": median_gap,
        "burst_days": burst_days,
        "type_counts": _event_type_counts(events),
        "degraded": bool(gaps),
        "degraded_reason": ("部分时间数据源未接入："
                            + "; ".join(x["object"] for x in gaps)) if gaps else None,
        "diagnostics": {"gaps": gaps},
    }


@register_function("timeline_cross_collision")
def _timeline_cross_collision(store, params: dict, ctx=None) -> dict:
    """锚点项目开标/公示日前后 window_days：同主体跨类型（资金/通话/轨迹）碰撞。

    project 显式给出时只扫该项目；project 缺省（规则手册 R7 执行路径）时
    扫描全部有可用公示日的项目，逐锚点计算并在每行带 project/anchor_date。
    """
    window_days = int(params.get("window_days", 7))
    if not 1 <= window_days <= 60:
        raise ValueError(f"window_days 允许 1-60，得到 {window_days}")
    min_event_types = int(params.get("min_event_types", 2))
    if not 2 <= min_event_types <= 3:
        raise ValueError(f"min_event_types 允许 2-3，得到 {min_event_types}")
    project = (params.get("project") or "").strip()

    bp = _tl_table(ctx, "bid_project")

    def _missing(pj, reason):
        return {"hit": False, "project": pj, "anchor_date": None,
                "rows": [], "count": 0, "degraded": True,
                "degraded_reason": reason,
                "diagnostics": {"gaps": []}}

    # 锚点集：(project_dict, anchor_date)；single 模式 1 个，scan 模式全部
    if project:
        pj = _resolve_subject(store, project, "bid_project", ctx)
        if pj is None:
            return _missing(None, f"项目 {project!r} 在语义层不存在")
        try:
            arows = store.query(
                f'SELECT project_id, title, CAST(pub_date AS DATE) AS d '
                f'FROM {bp} WHERE project_id = ?', (pj["pk"],))
        except Exception as e:
            if not _is_structural_degrade(e):
                raise
            return _missing(pj, _tl_gap("bid_project", e)["reason"])
        if not arows or not arows[0]["d"]:
            return _missing(pj, f"项目 {pj['name']} 缺少可用公示日")
        anchors = [(pj, arows[0]["d"])]
        single_pj, single_anchor = pj, arows[0]["d"]
    else:
        try:
            prows = store.query(
                f'SELECT project_id, title, CAST(pub_date AS DATE) AS d '
                f'FROM {bp}')
        except Exception as e:
            if not _is_structural_degrade(e):
                raise
            return _missing(None, _tl_gap("bid_project", e)["reason"])
        anchors = [(
            {"pk": str(r["project_id"]), "type": "bid_project",
             "name": r["title"] or str(r["project_id"])}, r["d"])
            for r in prows if r["project_id"] and r["d"]]
        if not anchors:
            return _missing(None, "语义层无带可用公示日的项目，碰撞不可计算")
        single_pj, single_anchor = None, None

    def _scan_anchor(anchor, gaps_acc: list[dict], seen_gaps: set):
        """单锚点：窗口内三类事件按主体归集 → 主体→bucket。"""
        by_subject: dict[str, dict] = {}

        def _add(subject_name, entry_type: str, event: dict) -> None:
            if not subject_name:
                return
            bucket = by_subject.setdefault(
                subject_name, {"types": set(), "events": [], "seen": set()})
            # 自发自收去重：同一笔交易 from_raw==to_raw（或通话 caller==callee）
            # 会在两侧各登记一次同一 event_pk，不拦会让 event_count 翻倍、
            # events 列表出现重复行。键含类型，防跨对象 PK 撞号误杀。
            sig = (entry_type, str(event.get("event_pk") or ""))
            if sig in bucket["seen"]:
                return
            bucket["seen"].add(sig)
            bucket["types"].add(entry_type)
            bucket["events"].append(event)

        def _gap(obj_name: str, e: Exception) -> None:
            if obj_name not in seen_gaps:
                seen_gaps.add(obj_name)
                gaps_acc.append(_tl_gap(obj_name, e))

        # 资金：窗口内资金交易两侧主体
        tbl = _tl_table(ctx, "transaction")
        try:
            rows = store.query(
                f'SELECT txn_id AS event_pk, from_raw, to_raw, '
                f'CAST(date AS DATE) AS d FROM {tbl} '
                f'WHERE ABS(date_diff(\'day\', CAST(? AS DATE), '
                f'CAST(date AS DATE))) <= ?',
                (anchor, window_days))
        except Exception as e:
            if not _is_structural_degrade(e):
                raise
            _gap("transaction", e)
        else:
            for r in rows:
                if not r["event_pk"] or not r["d"]:
                    continue
                off = (r["d"] - anchor).days
                _add(r["from_raw"], "transaction", {
                    "type": "transaction", "src_object": "transaction",
                    "event_pk": str(r["event_pk"]),
                    "date": r["d"].isoformat(), "offset_days": off,
                    "role": "转出", "brief": f"转出（{off:+d} 天）"})
                _add(r["to_raw"], "transaction", {
                    "type": "transaction", "src_object": "transaction",
                    "event_pk": str(r["event_pk"]),
                    "date": r["d"].isoformat(), "offset_days": off,
                    "role": "转入", "brief": f"转入（{off:+d} 天）"})

        # 通话：窗口内通话两侧主体
        tbl = _tl_table(ctx, "call")
        try:
            rows = store.query(
                f'SELECT call_id AS event_pk, caller_raw, callee_raw, '
                f'CAST(date AS DATE) AS d FROM {tbl} '
                f'WHERE ABS(date_diff(\'day\', CAST(? AS DATE), '
                f'CAST(date AS DATE))) <= ?',
                (anchor, window_days))
        except Exception as e:
            if not _is_structural_degrade(e):
                raise
            _gap("call", e)
        else:
            for r in rows:
                if not r["event_pk"] or not r["d"]:
                    continue
                off = (r["d"] - anchor).days
                _add(r["caller_raw"], "call", {
                    "type": "call", "src_object": "call",
                    "event_pk": str(r["event_pk"]),
                    "date": r["d"].isoformat(), "offset_days": off,
                    "role": "主叫", "brief": f"主叫（{off:+d} 天）"})
                _add(r["callee_raw"], "call", {
                    "type": "call", "src_object": "call",
                    "event_pk": str(r["event_pk"]),
                    "date": r["d"].isoformat(), "offset_days": off,
                    "role": "被叫", "brief": f"被叫（{off:+d} 天）"})

        # 轨迹：窗口内轨迹点主体
        tbl = _tl_table(ctx, "trackpoint")
        try:
            rows = store.query(
                f'SELECT track_id AS event_pk, person_raw, '
                f'CAST(date AS DATE) AS d FROM {tbl} '
                f'WHERE ABS(date_diff(\'day\', CAST(? AS DATE), '
                f'CAST(date AS DATE))) <= ?',
                (anchor, window_days))
        except Exception as e:
            if not _is_structural_degrade(e):
                raise
            _gap("trackpoint", e)
        else:
            for r in rows:
                if not r["event_pk"] or not r["d"]:
                    continue
                off = (r["d"] - anchor).days
                _add(r["person_raw"], "trackpoint", {
                    "type": "trackpoint", "src_object": "trackpoint",
                    "event_pk": str(r["event_pk"]),
                    "date": r["d"].isoformat(), "offset_days": off,
                    "role": "出现", "brief": f"轨迹出现（{off:+d} 天）"})
        return by_subject

    gaps: list[dict] = []
    seen_gaps: set[str] = set()
    rows_out: list[dict] = []
    for pj, anchor in anchors:
        by_subject = _scan_anchor(anchor, gaps, seen_gaps)
        anchor_iso = anchor.isoformat()
        for name, bucket in sorted(by_subject.items()):
            if len(bucket["types"]) < min_event_types:
                continue
            ev = sorted(bucket["events"],
                        key=lambda x: (x["date"], x["type"], x["event_pk"]))
            offsets = [x["offset_days"] for x in ev]
            # 输出列名用语义属性名（英文），不用中文业务列名——后者是
            # 「靠中文猜语义」的病根，换领域即失效。主体列取本体 name_property
            # 口径（person 为 raw_name，此处统一用 subject_raw 便于下游定位）。
            rows_out.append({
                "subject_raw": name,
                "event_types": sorted(bucket["types"]),
                "type_count": len(bucket["types"]),
                "event_count": len(ev),
                "first_offset": min(offsets),
                "last_offset": max(offsets),
                "project": pj,
                "anchor_date": anchor_iso,
                "events": ev,
            })
    rows_out.sort(key=lambda x: (x["project"]["pk"], x["subject_raw"]))
    return {
        "hit": bool(rows_out),
        "project": single_pj,
        "anchor_date": single_anchor.isoformat() if single_anchor else None,
        "scanned_projects": len(anchors),
        "window_days": window_days,
        "min_event_types": min_event_types,
        "rows": rows_out,
        "count": len(rows_out),
        "degraded": bool(gaps),
        "degraded_reason": ("部分时间数据源未接入："
                            + "; ".join(x["object"] for x in gaps)) if gaps else None,
        "diagnostics": {"gaps": gaps},
    }


# ----------------------------------------------------------------------
# 执行器
# ----------------------------------------------------------------------
class FunctionExecutor:
    """按 functions.json 声明执行只读计算。"""

    def __init__(self, store, pack: str = "default", access=None, health=None,
                 base_dir=None):
        self.store = store
        # REQ-R1：py 函数只读护栏——所有 py 实现通过此代理访问数据
        from core.runtime_context import ReadOnlyStore
        self._ro_store = ReadOnlyStore(store)
        self.pack = pack
        # 案件快照基目录（Web 案件包隔离）：None=共享 ontology/（CLI/MCP 现状）
        self.base_dir = base_dir
        # REQ-009：access=None → system 旁路（既有调用行为不变）
        from core.access import system_context
        self.access = access if access is not None else system_context()
        from core.policy import PolicyEngine
        self.policy = PolicyEngine(pack)
        # REQ-G-010：运行诊断（None → NullRunHealth，既有调用零行为变化）
        from core.run_health import get_health
        self.health = get_health(health)

    def _specs(self) -> dict:
        return load_pack(self.pack, base_dir=self.base_dir).functions

    def catalog(self) -> list[dict]:
        """可发现的函数目录（MCP function_list 消费）+ 归属状态。

        归属回答"这个 Function 被谁用了"：经规则（有判定层、能产命题）/
        经镜头（研判手段、产观察）/ 被内置技能直调（有业务目标无判定层）/
        未接线。未接线的不会因此禁用——直接调用技术上没问题，只是没有
        判定语义，让人知道即可。
        """
        try:
            from core.function_bindings import scan_function_bindings
            binds = scan_function_bindings(self.pack, base_dir=self.base_dir)
        except Exception:
            binds = {}
        out = []
        for f in self._specs().values():
            b = binds.get(f.name) or {}
            out.append({
                "name": f.name, "title": f.title, "inputs": list(f.inputs),
                "output_type": f.output_type, "impl": f.impl,
                "parameters": f.parameters, "description": f.description,
                "readonly": True,
                # 归属（注册期可见，不用事后排查）
                "status": b.get("status", ""),
                "status_label": b.get("status_label", ""),
                "bound_by": b.get("bound_by", []),
                "note": b.get("note", ""),
            })
        return out

    def _make_ctx(self):
        """构造 RuntimeContext（py 函数运行时上下文 + 只读护栏）。"""
        from core.runtime_context import RuntimeContext, ReadOnlyStore
        return RuntimeContext(
            store=ReadOnlyStore(self.store),
            pack=self.pack,
            access=self.access,
            policy=self.policy,
            health=self.health,
            base_dir=self.base_dir,
        )

    def _call_py(self, impl_ref: str, params: dict):
        """调用 py 函数实现：用 inspect.signature 兼容新旧签名。

        新签名 fn(store, params, ctx) —— 接收 RuntimeContext，可 ctx.table()/ctx.link()
        旧签名 fn(store, params) —— 仅 store+params，行为不变（向后兼容）
        """
        fn = FUNCTION_IMPLS[impl_ref]
        try:
            sig = inspect.signature(fn)
            n_params = len([p for p in sig.parameters.values()
                            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
        except (TypeError, ValueError):
            n_params = 2
        if n_params >= 3:
            return fn(self._ro_store, params, self._make_ctx())
        return fn(self._ro_store, params)

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
            result = self._call_py(spec.impl_ref, merged)
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

# PLAN-GEO-001 P2：空间研判 Function（实现同在 core/geo.py，只读离线）。
register_function("geo_subject_sites")(_geo.geo_subject_sites)
register_function("geo_co_located_radius")(_geo.geo_co_located_radius)
register_function("geo_buffer_scan")(_geo.geo_buffer_scan)
register_function("geo_profile_cgt")(_geo.geo_profile_cgt)
