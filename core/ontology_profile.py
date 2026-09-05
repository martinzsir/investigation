"""
core/ontology_profile.py
本体画像前置设施（REQ-P M3，P0）：connectable_props 与实体连接探测编排器。

M3 范围（.trae/documents/REQ-P/REQ-P实施计划.md §三 P0）：
  - connectable_props(pack)：string 属性 − metadata_props（REQ-P-034）− runtime 对象，
    来源 pack 声明（声明是数据）；
  - EntityLinkExplorer（对齐沙盒名的编排器）：connectable_props + materialized 判定
    + 变体双轨；
  - 变体双轨（画像指南 铁律 2：变体数 0 不代表干净）：
      规则轨：同语言异写——复用 entity_resolution 的 _name_similarity/_pinyin_key
              （import 复用不复制），仅标 needs_review 候选，不自动合并；
      别名轨：跨语言/已知别名——读 case_knowledge.json 的 subject_aliases
              （经 core.functions.load_case_knowledge）；无别名表 → 降级标注。
  - 元数据属性（status/title/note…）不参与实体连接：variants() 只接受可连接属性
    （REQ-P-009 由本编排层保证，value_type 本身不管）。

红线：全部只读（gateway 只读入口 + load_pack 声明），观察不写回。
六层画像（OntologyProfiler）M4 扩完，健康度接线 M5（REQ-P-021）。
"""
from __future__ import annotations

import sys

from core.functions import load_case_knowledge
from core.ontology_loader import load_pack
from core.run_health import get_health
from core.threshold import load_profiler_settings
from core.value_type import analyze_column


def connectable_props(pack: str = "default") -> dict[str, list[str]]:
    """可连接属性清单：string 属性 − metadata_props − runtime 对象。

    返回 {对象名: [属性名, ...]}（声明序）；全部属性被排除/无 string 属性的
    对象（如 clue 全列 metadata）不出现在结果中。
    """
    spec = load_pack(pack)
    out: dict[str, list[str]] = {}
    for o in spec.objects:
        if o.runtime:
            continue
        props = [p for p, t in o.properties.items()
                 if t == "string" and p not in o.metadata_props]
        if props:
            out[o.name] = props
    return out


def _entity_resolution():
    """复用根包 entity_resolution（import 复用不复制）。

    core.entity 以文件路径加载该模块并缓存于 sys.modules
    （_person_resolver_entity_resolution）——优先复用缓存，其次普通 import，
    最后按文件路径加载。找不到即编程环境错误（fail-loud）。
    """
    cached = sys.modules.get("_person_resolver_entity_resolution")
    if cached is not None and hasattr(cached, "_name_similarity"):
        return cached
    try:
        import entity_resolution as mod  # repo 根在 sys.path
        return mod
    except ImportError:
        import importlib.util
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "entity_resolution.py"
        if not src.exists():
            raise ImportError(
                f"entity_resolution 模块缺失（期望位于 {src}）——"
                "变体规则轨不可用，请检查仓库完整性")
        spec = importlib.util.spec_from_file_location(
            "_ontology_profile_entity_resolution", str(src))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_ontology_profile_entity_resolution"] = mod
        spec.loader.exec_module(mod)
        return mod


class EntityLinkExplorer:
    """实体连接探测编排器（只读）：属性清单 + 物化判定 + 变体双轨。

    用法：
        ex = EntityLinkExplorer(gateway)
        ex.connectable_props()            # {对象: [可连接属性]}
        ex.materialized_objects()         # 已物化对象
        ex.variants("person", "raw_name") # 双轨变体（候选，不合并）
    """

    def __init__(self, gateway, pack: str = "default",
                 aliases: dict[str, list[str]] | None = None):
        self._gw = gateway
        self._pack = pack
        # 别名轨默认读 case_knowledge.json；显式传入可覆盖（测试/自定义包）
        self._aliases = aliases

    # ---- 属性清单 / 物化判定（全部经 gateway / 声明） ----
    def connectable_props(self) -> dict[str, list[str]]:
        return connectable_props(self._pack)

    def materialized_objects(self) -> list[str]:
        return self._gw.materialized_objects()

    def distinct_values(self, obj: str, prop: str, limit: int = 1000) -> list:
        return self._gw.distinct_values(obj, prop, limit=limit)

    # ---- 变体双轨 ----
    def rule_variants(self, values, threshold: float = 0.85) -> list[dict]:
        """规则轨：同语言异写（规范化后拼音/编辑距离相似）。

        相似对仅标 needs_review 候选，不自动合并（对齐 entity_resolution 红线）。
        比对前经 normalize_person_name 规范化（去空白/括号备注），报告原值。
        """
        er = _entity_resolution()
        normalize = er.normalize_person_name
        sim = er._name_similarity
        uniq = sorted({str(v) for v in values
                       if v is not None and str(v).strip()})
        out = []
        for i in range(len(uniq)):
            ni = normalize(uniq[i])
            for j in range(i + 1, len(uniq)):
                nj = normalize(uniq[j])
                if not ni or not nj or ni == nj:
                    continue
                s = sim(ni, nj)
                if s >= threshold:
                    out.append({"a": uniq[i], "b": uniq[j],
                                "similarity": round(s, 4),
                                "needs_review": True})
        return out

    def alias_variants(self, values) -> list[dict]:
        """别名轨：subject_aliases 命中（canonical ↔ alias 共现于同列）。"""
        aliases = self._aliases
        if aliases is None:
            aliases = load_case_knowledge(self._pack).get("subject_aliases") or {}
        present = {str(v).strip() for v in values
                   if v is not None and str(v).strip()}
        out = []
        for canon, vs in aliases.items():
            if canon not in present:
                continue
            for v in vs or []:
                if v != canon and v in present:
                    out.append({"canonical": canon, "alias": v,
                                "source": "case_knowledge"
                                if self._aliases is None else "explicit"})
        return out

    def variants(self, obj: str, prop: str) -> dict:
        """双轨汇总。铁律：变体数 0 不代表干净（note 固化）。

        只接受可连接属性（REQ-P-009）：metadata 属性 / runtime 对象硬失败——
        其值类型分布与变体对实体连接无意义（org.status「存续」刷屏问题）。
        """
        props = connectable_props(self._pack).get(obj, [])
        if prop not in props:
            raise ValueError(
                f"{obj}.{prop} 不是可连接属性（string − metadata_props；"
                f"{obj} 可连接：{props}）——元数据属性不参与实体连接")
        values = self._gw.distinct_values(obj, prop)
        if self._aliases is None:
            has_alias_track = bool(
                load_case_knowledge(self._pack).get("subject_aliases"))
        else:
            has_alias_track = bool(self._aliases)
        return {
            "obj": obj,
            "prop": prop,
            "rule_variants": self.rule_variants(values),
            "alias_variants": self.alias_variants(values),
            "alias_track_available": has_alias_track,
            "note": ("变体数 0 不代表干净——规则轨只覆盖同语言异写，"
                     "别名轨依赖 case_knowledge 完备性"),
        }


# ======================================================================
# REQ-P M4：六层画像 OntologyProfiler（L1/L2/L3/L4/L5；L0 见 core.data_map）
# ======================================================================
# 五间顺序（L4 反查表固定次序；间类归类只来自 objects/links.json 的 jian 声明
# （REQ-G-013），本模块不做任何硬编码间类映射——AC 以源码扫描固化）
JIAN_ORDER = ("因间", "内间", "反间", "死间", "生间")

# L5 质量分权重（模块常量；阻断 block / 告警 warn）
SCORE_BLOCK = {"mixed": -25, "null_rate_high": -20, "zero_rows": -30}
SCORE_WARN = {"unmaterialized": -12, "low_cardinality": -8,
              "affirmative_type": -5, "no_wan_integer": -5, "has_variants": -5}
# 启发式可推翻扣分项（区间上沿 = 推翻这些项后的分数）
REVIEWABLE_DEDUCTIONS = frozenset({"affirmative_type", "has_variants"})

_NULL_RATE_BLOCK = 0.5     # 空值率 ≥50% 阻断
_LOW_CARDINALITY = 2       # 基数 ≤2 告警
_PROFILE_NOTE = ("结论均为【待核实】候选；画像只观察不写回，"
                 "启发式扣分（肯定式识别/变体）可人工推翻")


class OntologyProfiler:
    """六层本体画像（只读编排器；全部取数经 gateway，不写任何库表）。

    用法：
        prof = OntologyProfiler(gw, focus_entities=["张卫国"],
                                anchor_date="2021-10-01")
        report = prof.profile_all()   # L1/L2 + L3 + L4 + L5

    参数纪律（对齐 R5 教训，不硬编码人名/日期）：
      focus_entities  关注主体名单（调用方传；空则 focus 指标 not_evaluated）
      anchor_date     时间窗锚点日期（调用方传；空则 window 指标 not_evaluated）
      window_days     None 时读 thresholds.json profiler.window_days（REQ-P-014）
    """

    def __init__(self, gateway, pack: str = "default", *,
                 focus_entities: list | None = None,
                 anchor_date: str | None = None,
                 window_days: int | None = None,
                 health=None):
        self._gw = gateway
        self._pack = pack
        self._spec = load_pack(pack)
        self._focus = [str(v) for v in (focus_entities or []) if v]
        self._anchor = anchor_date
        # REQ-P-021：health=None → NullRunHealth（空操作，既有调用零行为变化）
        self._health = get_health(health)
        settings = load_profiler_settings(pack)
        self._window = (int(window_days) if window_days is not None
                        else int(settings["window_days"]))
        self._sample_limit = int(settings["value_sample_limit"])
        self._explorer = EntityLinkExplorer(gateway, pack)

    # ---- 入口 ----
    def profile_all(self) -> dict:
        # 版本锚点检查（G-007）：取不到本体版本 → 审计/血缘锚定 unknown
        ont_ver = None
        try:
            ont_ver = self._gw.explain().get("ontology_version")
        except Exception:
            ont_ver = None
        if not ont_ver:
            self._health.record(
                "version_anchor_missing", "warning",
                source="ontology_profile",
                reason="本体版本锚点缺失（语义层未构建？审计/血缘将锚定 unknown）")

        mat_objects = set(self._gw.materialized_objects())
        mat_props = self._gw.materialized_props()
        connectable = connectable_props(self._pack)
        known = self._known_entities(mat_objects)

        l1l2: list[dict] = []
        l3: list[dict] = []
        deductions: list[dict] = []
        for o in self._spec.objects:
            if o.runtime:
                continue   # runtime 对象（decision）无数据源，不画像
            obj_mat = o.name in mat_objects
            if not obj_mat:
                # 对象级落一条（避免每属性重复）；info 级——未物化是待接入，非错误
                self._health.record(
                    "profile_unmaterialized", "info",
                    source="ontology_profile",
                    reason=f"对象 {o.name} 未物化，画像占位（表不存在）",
                    object=o.name)
            for prop, ptype in o.properties.items():
                l1l2.append(self._profile_prop(
                    o.name, prop, ptype, obj_mat, mat_props,
                    connectable.get(o.name, []), known, l3, deductions))
            # 对象级：资金属性无万元整数 → 告警（decimal 不在可连接属性内，
            # 故挂对象级，不破坏"只对可连接属性计属性级分"的口径）
            if obj_mat:
                self._object_wan_check(o.name, o.properties, deductions)

        return {
            "pack": self._pack,
            "l0": "not_applicable（物化后无文件层；L0 拓扑见 core.data_map.DataMap）",
            "l1_l2": l1l2,
            "l3": l3,
            "l4": self._jian_map(mat_objects),
            "l5": self._score(deductions),
            "params": {"window_days": self._window,
                       "anchor_date": self._anchor,
                       "focus_entities": self._focus},
            "health": self._health.health_section(),
            "note": _PROFILE_NOTE,
        }

    # ---- L1/L2：列层/值层 ----
    def _profile_prop(self, obj, prop, ptype, obj_mat, mat_props,
                      conn_props, known, l3, deductions) -> dict:
        key = f"{obj}.{prop}"
        is_conn = prop in conn_props
        entry = {
            "obj": obj, "prop": prop, "declared_type": ptype,
            "connectable": is_conn,
            "materialized_object": obj_mat,
            "materialized_prop": bool(mat_props.get(key, False)),
        }
        if not obj_mat:
            entry["status"] = "unmaterialized_object"
            if is_conn:
                self._deduct(deductions, "prop", key, "unmaterialized",
                             f"对象 {obj} 未物化，属性 {prop} 无法画像", "warn")
            return entry
        if not entry["materialized_prop"]:
            entry["status"] = "missing_column"
            if is_conn:
                self._deduct(deductions, "prop", key, "unmaterialized",
                             f"对象 {obj} 已物化但表中无列 {prop}（schema 演进）", "warn")
                self._health.record(
                    "profile_missing_column", "warning",
                    source="ontology_profile",
                    reason=f"对象 {obj} 已物化但表中无列 {prop}（schema 演进/部分物化）",
                    object=obj, prop=prop)
            return entry

        vp = self._gw.value_profile(obj, prop)
        entry.update({"status": "ok", "value_profile": {
            "row_count": vp["row_count"], "non_null": vp["non_null"],
            "null_rate": round(vp["null_rate"], 4),
            "distinct": vp["distinct"], "samples": vp["samples"]}})

        if is_conn:
            self._score_prop_l1l2(key, vp, deductions)
        if ptype == "string" and is_conn:
            self._string_prop(obj, prop, entry, known, l3, deductions)
        elif ptype == "decimal":
            ind = self._gw.prop_indicator(obj, prop, "wan_integer_rate")
            l3.append({"obj": obj, "prop": prop, **ind})
        elif ptype == "date":
            self._date_prop(obj, prop, l3)
        return entry

    def _score_prop_l1l2(self, key, vp, deductions) -> None:
        if vp["row_count"] == 0:
            self._deduct(deductions, "prop", key, "zero_rows",
                         "0 行（对象已物化但无数据）", "block")
        elif vp["null_rate"] >= _NULL_RATE_BLOCK:
            self._deduct(deductions, "prop", key, "null_rate_high",
                         f"空值率 {vp['null_rate']:.0%} ≥ {_NULL_RATE_BLOCK:.0%}", "block")
        if 0 < vp["distinct"] <= _LOW_CARDINALITY:
            self._deduct(deductions, "prop", key, "low_cardinality",
                         f"基数 {vp['distinct']} ≤ {_LOW_CARDINALITY}", "warn")

    def _string_prop(self, obj, prop, entry, known, l3, deductions) -> None:
        key = f"{obj}.{prop}"
        vals = self._gw.distinct_values(obj, prop, limit=self._sample_limit)
        ana = analyze_column(vals)
        entry["type_dist"] = ana["type_dist"]
        entry["mixed"] = ana["mixed"]
        entry["landing_suggestions"] = ana["landing_suggestions"]
        entry["needs_confirmation"] = ana["needs_confirmation"]
        if ana["mixed"]:
            self._deduct(deductions, "prop", key, "mixed",
                         f"混装：落点 {ana['landing_suggestions']}（归一落点不明确）", "block")
        if ana["needs_confirmation"]:
            self._deduct(deductions, "prop", key, "affirmative_type",
                         "含肯定式识别（人名/机构名为启发式，需人工确认）", "warn")
        # 变体双轨（复用 M3 编排器；只数候选，不合并）
        rv = self._explorer.rule_variants(vals)
        av = self._explorer.alias_variants(vals)
        entry["variants"] = {"rule": len(rv), "alias": len(av)}
        if rv or av:
            self._deduct(deductions, "prop", key, "has_variants",
                         f"变体候选 {len(rv) + len(av)} 个（规则 {len(rv)}/别名 {len(av)}）",
                         "warn")
        # L3：关注命中 / 与已知实体重合
        if self._focus:
            l3.append({"obj": obj, "prop": prop,
                       **self._gw.prop_indicator(
                           obj, prop, "focus_hit_rate", values=self._focus)})
        else:
            l3.append({"obj": obj, "prop": prop, "metric": "focus_hit_rate",
                       "status": "not_evaluated", "reason": "未提供 focus_entities"})
        known_vals = self._known_for(ana["landing_suggestions"], known)
        if known_vals:
            l3.append({"obj": obj, "prop": prop,
                       **self._gw.prop_indicator(
                           obj, prop, "known_overlap_count", values=known_vals)})

    def _date_prop(self, obj, prop, l3) -> None:
        if not self._anchor:
            l3.append({"obj": obj, "prop": prop, "metric": "window_coverage",
                       "status": "not_evaluated",
                       "reason": "未提供 anchor_date（锚点由调用方传入，不硬编码）"})
            return
        l3.append({"obj": obj, "prop": prop,
                   **self._gw.prop_indicator(
                       obj, prop, "window_coverage",
                       anchor_date=self._anchor, window_days=self._window)})

    def _object_wan_check(self, obj, properties, deductions) -> None:
        for prop, ptype in properties.items():
            if ptype != "decimal":
                continue
            ind = self._gw.prop_indicator(obj, prop, "wan_integer_rate")
            if ind["denominator"] > 0 and ind["value"] == 0:
                self._deduct(deductions, "object", obj, "no_wan_integer",
                             f"{obj}.{prop} 无万元整数交易（资金信号弱）", "warn")

    # ---- L4：五间（声明化；无 DEFAULT_JIAN_MAP）----
    def _jian_map(self, mat_objects) -> dict:
        forward = {j: {"objects": [], "links": []} for j in JIAN_ORDER}
        for o in self._spec.objects:
            if o.jian in forward:
                forward[o.jian]["objects"].append(o.name)
        for l in self._spec.links:
            if l.jian in forward:
                forward[l.jian]["links"].append(l.name)
        reverse = []
        for j in JIAN_ORDER:
            objs = forward[j]["objects"]
            reverse.append({
                "jian": j,
                "objects": objs,
                "links": forward[j]["links"],
                "declared": bool(objs or forward[j]["links"]),
                "has_materialized": any(x in mat_objects for x in objs),
            })
        return {"forward": forward, "reverse": reverse}

    # ---- L5：质量分 ----
    def _score(self, deductions: list[dict]) -> dict:
        total = 0
        reviewable_pts = 0
        for d in deductions:
            w = SCORE_BLOCK.get(d["code"], SCORE_WARN.get(d["code"], 0))
            d["points"] = w
            total += w
            if d["code"] in REVIEWABLE_DEDUCTIONS:
                reviewable_pts += w
        score = max(0, 100 + total)
        score_hi = max(score, min(100, 100 + total - reviewable_pts))
        return {
            "score": score,
            "score_range": [score, score_hi],
            "deductions": deductions,
            "reviewable": True,
            "weights": {"block": SCORE_BLOCK, "warn": SCORE_WARN},
            "note": _PROFILE_NOTE,
        }

    # ---- 辅助 ----
    def _known_entities(self, mat_objects: set) -> dict[str, set]:
        """已知实体身份值集合（person/account/org 的 raw_name），供重合数指标。"""
        known: dict[str, set] = {"person": set(), "account": set(), "org": set()}
        for ent in known:
            if ent in mat_objects:
                try:
                    known[ent] = set(self._gw.distinct_values(
                        ent, "raw_name", limit=self._sample_limit))
                except Exception:
                    known[ent] = set()
        return known

    @staticmethod
    def _known_for(landings: list[str], known: dict[str, set]) -> list:
        out: set = set()
        for land in landings:
            out |= known.get(land, set())
        return sorted(out)

    @staticmethod
    def _deduct(deductions, scope, ref, code, reason, severity) -> None:
        deductions.append({"scope": scope, "ref": ref, "code": code,
                           "reason": reason, "severity": severity})


def record_map_gaps(health, gaps, source: str = "data_map") -> int:
    """把 DataMap.normalize_gaps() 检出的归一缺口落运行诊断（REQ-P-021）。

    data_map 是零依赖静态模块（只 json/re，不连库），诊断接线由编排层经本函数
    完成；gaps=None 表示 bindings 缺失「缺口未计算」（≠无缺口），不落诊断。
    返回落诊断条数。
    """
    h = get_health(health)
    n = 0
    for g in gaps or []:
        h.record("map_normalize_gap", "warning", source=source,
                 reason=f"归一缺口：{g.get('object')}.{g.get('prop')} "
                        f"未被任何 link build_sql 等值归一（断链温床）",
                 **{k: v for k, v in g.items()
                    if isinstance(v, (str, int, float, bool))})
        n += 1
    return n
