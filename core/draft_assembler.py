"""
core/draft_assembler.py
REQ-P M6（REQ-P-035）：新表接入的草案组装推荐器 + ETL 步骤序列推荐器。

定位：把 M3/M4 的画像（core.ontology_profile.TableProfile）升级为「诊断 + 生成」——
  - DraftAssembler：把外部表画像组装成 objects/links/bindings 三件草案；
  - recommend_steps：给出接入 ETL 步骤序列（依赖排序，只输出清单不执行）。

红线（三禁令不破）：
  1. 全部输出只写 output/drafts/<table>/，**绝不写 ontology/**（AC 源码扫描固化）；
     草案头带 _draft/_status=待核实/_evidence，人工审核复制进 ontology/<pack>/ 后，
     经 build_ontology（loader 校验）+ 人工确认两道闸才生效；
  2. 每条建议必携 _evidence 画像证据（列/候选关联/overlap），可追溯；
  3. 值类型映射输出必须是 TYPE_SQL 认识的属性类型（AC 与 TYPE_NAMES 双向核对）；
  4. recommend_steps 无任何 IO 副作用，执行仍走 data_ingest / build_ontology 既有命令；
  5. 清洗规则名只能引用 loader 已注册规则（CLEAN_RULE_NAMES），缺则建议「需新增」，
     绝不编造名字。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from core.ontology import TYPE_NAMES, CLEAN_RULE_NAMES

ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------------
# 值类型映射（value_type.classify 类别 → ontology 属性类型）
# 否定式标识/名称类 → string；金额 → decimal；日期串 → date；纯整数 → integer。
# 输出集合必须 ⊆ TYPE_NAMES（test 双向核对；未列入的类别落 string 保守默认）。
# ----------------------------------------------------------------------
VALUE_TYPE_TO_PROP_TYPE: dict[str, str] = {
    "account": "string",     # 账号/卡号（标识类，归一到 account 对象）
    "phone": "string",       # 手机号（标识类，归一到 person）
    "id_card": "string",     # 身份证（标识类，归一到 person）
    "person": "string",      # 人名（肯定式，需人工确认）
    "org": "string",         # 机构名（肯定式，需人工确认）
    "empty": "string",       # 全空列：保守 string，画像证据标注
    "number": "integer",     # 纯整数
    "amount": "decimal",     # 金额串（¥/千分位/万元）
    "date_str": "date",      # 日期串
    "boolean": "boolean",    # 列级布尔启发（见 _column_boolean）
}

# 列级布尔值词表（distinct ≤2 且全集命中才判 boolean，避免吞掉 0/1 编号列）
_BOOLEAN_TOKENS = {"是", "否", "有", "无", "true", "false", "yes", "no",
                   "y", "n", "1", "0", "对", "错"}

# 标识类否定式（高基数 → pk 候选）
_ID_TYPES = ("account", "id_card", "number")
# 名称类肯定式（→ name_property 候选）
_NAME_TYPES = ("person", "org")
# metadata 列名启发（content/长文本/时间戳类非标识列 → 建议 metadata_props）
_METADATA_NAME_RE = re.compile(
    r"(content|note|remark|备注|说明|描述|摘要|内容|title|标题|意见|详情)")

DRAFT_HEADER = {
    "schema_version": 2,
    "_draft": True,
    "_status": "待核实",
    "_note": ("本文件为画像自动组装的【待核实】草案，不是生效声明；"
              "人工审核后复制进 ontology/<pack>/，经 build_ontology loader 校验生效。"),
}


def _dominant_type(cp) -> tuple[str, dict]:
    """列主导值类型：布尔列启发优先，其次取除 empty 外计数最多的类别。"""
    if cp.distinct > 0 and cp.distinct <= 2 and cp.samples:
        toks = {str(s).strip().lower() for s in cp.samples}
        if toks and toks <= _BOOLEAN_TOKENS:
            return "boolean", cp.type_dist
    dist = {k: v for k, v in cp.type_dist.items() if k != "empty"}
    if not dist:
        return "empty", cp.type_dist
    return max(dist.items(), key=lambda kv: kv[1])[0], cp.type_dist


# 列名语义启发（裸整数金额/日期串等值类型无法区分时，列名是强信号）
_AMOUNT_NAME_RE = re.compile(r"(金额|amount|价款|款项|余额|数额)")
_DATE_NAME_RE = re.compile(r"(日期|date|时间|time|年月日)")
_BOOL_NAME_RE = re.compile(r"(是否|有无|is_|has_|flag|标记)")
# 非标识列名（即使值形态像账号/编号也不做 pk：金额/日期/备注/名称类）
_NOT_PK_NAME_RE = re.compile(
    r"(金额|amount|价款|款项|余额|数额|日期|date|时间|time|备注|note|remark|"
    r"说明|内容|名称|姓名|name|标题|title)")


def _column_name_hint(name: str) -> str | None:
    """列名语义启发 → 属性类型；None 表示列名无强信号，走值类型判定。"""
    if _AMOUNT_NAME_RE.search(name):
        return "decimal"
    if _DATE_NAME_RE.search(name):
        return "date"
    if _BOOL_NAME_RE.search(name):
        return "boolean"
    return None


def _prop_type(cp) -> str:
    """属性类型：列名语义启发优先（裸整数金额/日期靠列名纠偏），其次值类型映射。"""
    hinted = _column_name_hint(cp.name)
    if hinted is not None:
        return hinted
    dom, _ = _dominant_type(cp)
    return VALUE_TYPE_TO_PROP_TYPE.get(dom, "string")


@dataclass
class _Evidence:
    """草案证据条目（序列化为 _evidence）。"""
    kind: str
    detail: dict

    def to_dict(self) -> dict:
        return {"kind": self.kind, **self.detail}


class DraftAssembler:
    """把 TableProfile 组装成 objects/links/bindings 三件草案（只生成，不落 ontology）。"""

    def __init__(self, profile, pack: str = "default"):
        self.p = profile
        self.pack = pack
        # 对象名占位：外部表英文标识需人工命名（中文表名不能直接做语义表名）
        self.object_name = "ext_TODO_RENAME"
        self._ev: list[_Evidence] = []

    # ---- 证据 ----
    def _add_evidence(self, kind: str, **detail) -> None:
        self._ev.append(_Evidence(kind, detail))

    # ---- objects.json 草案 ----
    def draft_object(self) -> dict:
        props: dict[str, str] = {}
        metadata_suggest: list[str] = []
        pk_candidates: list[str] = []
        name_candidates: list[str] = []

        # 第一遍：类型映射 + metadata 建议（name 候选需排除 metadata 列）
        meta_set: set[str] = set()
        for cp in self.p.columns:
            props[cp.name] = _prop_type(cp)
            max_len = max((len(str(s)) for s in cp.samples), default=0)
            if _METADATA_NAME_RE.search(cp.name) or max_len > 50:
                metadata_suggest.append(cp.name)
                meta_set.add(cp.name)
                self._add_evidence(
                    "metadata_props_suggestion", column=cp.name,
                    reason=("列名命中内容/备注类" if _METADATA_NAME_RE.search(cp.name)
                            else f"长文本（样例最大长度 {max_len}>50）"),
                    note="建议加入 metadata_props：不参与实体连接，画像不对其计值类型分")

        # 第二遍：混装证据 + pk/name 候选
        for cp in self.p.columns:
            dom, _ = _dominant_type(cp)
            if cp.mixed:
                self._add_evidence(
                    "mixed_column", column=cp.name,
                    type_dist=cp.type_dist, landings=cp.landing_suggestions,
                    note="混装列：须先 split_mixed 拆分再绑定，落点不明确时连线会双向误报")
            uniq_ratio = cp.distinct / self.p.row_count if self.p.row_count else 0
            # pk 候选：否定式标识类 + 高基数 + 非混装 + 列名非金额/日期/备注/名称
            if (dom in _ID_TYPES and not cp.mixed and cp.name not in meta_set
                    and not _NOT_PK_NAME_RE.search(cp.name)
                    and uniq_ratio >= 0.9 and cp.null_rate < 0.5):
                pk_candidates.append(cp.name)
                self._add_evidence(
                    "pk_candidate", column=cp.name, dominant_type=dom,
                    distinct=cp.distinct, uniqueness=round(uniq_ratio, 3))
            # name_property 候选：人名/机构名主导 + 非混装 + 非 metadata 列
            if (dom in _NAME_TYPES and not cp.mixed
                    and cp.name not in meta_set):
                name_candidates.append(cp.name)
                self._add_evidence(
                    "name_property_candidate", column=cp.name, dominant_type=dom,
                    needs_confirmation=cp.needs_confirmation)

        name_property = name_candidates[0] if name_candidates else (
            pk_candidates[0] if pk_candidates else self.p.columns[0].name
            if self.p.columns else "id")
        obj = {
            "name": self.object_name,
            "title": f"{self.p.table_name}（外部接入候选，对象名待人工命名）",
            "kind": "entity",
            "name_property": name_property,
            "properties": props,
            "_candidates": {
                "pk": pk_candidates,
                "name_property": name_candidates,
                "metadata_props_suggested": metadata_suggest,
            },
            "_evidence": [e.to_dict() for e in self._ev],
        }
        doc = dict(DRAFT_HEADER)
        doc["pack"] = self.pack
        doc["objects"] = [obj]
        return doc

    # ---- links.json 草案（候选关联 → 链接 + endpoints ref）----
    def draft_links(self) -> dict:
        links: list[dict] = []
        for c in self.p.candidates:
            lname = f"{self.object_name}_to_{c.target_obj}"
            links.append({
                "name": lname,
                "from_obj": self.object_name,
                "to_obj": c.target_obj,
                "properties": {},
                "_endpoints_hint": {
                    "from_col_raw": c.col,
                    "to_ref": {"object": c.target_obj, "prop": c.target_prop},
                    "double_column": (
                        "双列方案（对齐 REQ-P-031）：raw 列保留 + "
                        f"{c.col}_id 外键；build_sql 用 LEFT JOIN，NULL 边不丢"),
                },
                "_evidence": [{
                    "kind": "candidate_assoc",
                    "column": c.col, "target": f"{c.target_obj}.{c.target_prop}",
                    "overlap_ratio": c.overlap_ratio,
                    "direction": c.direction,
                    "note": f"overlap {c.overlap_ratio:.0%} ≥ 阈值，建议归一 JOIN；恒为候选待人工确认"}],
            })
        doc = dict(DRAFT_HEADER)
        doc["pack"] = self.pack
        doc["links"] = links
        return doc

    # ---- bindings.json 草案（object_binding + link_bindings）----
    def draft_bindings(self) -> dict:
        # object_binding：clean 只引用已注册规则；混装/特殊清洗 → 标"需新增"
        clean_suggest = ["strip"] if "strip" in CLEAN_RULE_NAMES else []
        missing_rules: list[str] = []
        if any(cp.mixed for cp in self.p.columns):
            missing_rules.append(
                "split_mixed（混装拆分：loader 无此清洗规则，需先按 recommend_steps "
                "拆分源列或新增清洗规则，不得编造规则名）")
        obj_binding = {
            "object": self.object_name,
            "source": f"TODO: data/{self.p.table_name}.parquet（外部表入冷层后的相对路径）",
            "source_sql": None,
            "clean": clean_suggest,
            "optional": False,
            "_evidence": [{
                "kind": "binding_placeholder",
                "clean_used": clean_suggest,
                "clean_available": sorted(CLEAN_RULE_NAMES),
                "clean_missing": missing_rules,
                "note": "加 binding 即自动建表（G-014）；source 路径/source_sql 由人工接入时填"}],
        }
        link_bindings: list[dict] = []
        for c in self.p.candidates:
            lname = f"{self.object_name}_to_{c.target_obj}"
            link_bindings.append({
                "link": lname,
                "build_sql": (
                    f"SELECT src.*, t.{c.target_obj}_id AS {c.col}_id "
                    f"FROM {self.object_name} src "
                    f"LEFT JOIN obj_{c.target_obj} t "
                    f"ON t.{c.target_prop} = src.{c.col}"),
                "_evidence": [{
                    "kind": "link_binding_sql",
                    "target": f"{c.target_obj}.{c.target_prop}",
                    "overlap_ratio": c.overlap_ratio,
                    "note": ("LEFT JOIN 保边（NULL 不丢）；双列：raw 保留 + "
                             f"{c.col}_id 外键；SQL 为草案，人工核对列名后生效")}],
            })
        doc = dict(DRAFT_HEADER)
        doc["pack"] = self.pack
        doc["object_bindings"] = [obj_binding]
        doc["link_bindings"] = link_bindings
        return doc

    # ---- 落盘（只写 output/drafts/，绝不写 ontology/）----
    def write_drafts(self, out_dir: str | Path = "output/drafts") -> list[Path]:
        base = Path(out_dir)
        safe = re.sub(r"[^\w一-鿿.-]+", "_", self.p.table_name).strip("_") or "table"
        target = base / safe
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for fname, doc in (("objects.draft.json", self.draft_object()),
                           ("links.draft.json", self.draft_links()),
                           ("bindings.draft.json", self.draft_bindings())):
            fp = target / fname
            fp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            written.append(fp)
        return written


# ----------------------------------------------------------------------
# ETL 步骤序列推荐器（零 IO；只输出清单，执行走既有命令）
# ----------------------------------------------------------------------
STEP_ORDER = ["split_mixed", "clean_rules", "type_cast", "cold_table",
              "bind_object", "bind_links", "reprofile"]

# 干净表退化序列（无混装、无缺值、无候选关联）
_CLEAN_SEQUENCE = ["cold_table", "bind_object", "reprofile"]


def recommend_steps(profile) -> list[dict]:
    """按依赖排序给出接入步骤；每步 {step, why, how, done_when}，可追溯画像证据。

    依赖纪律（AC 固化）：
      1. 混装拆分先于一切绑定（落点不明确时连线双向误报，G-015 教训）；
      2. 清洗规则先于冷层建表（CTAS 用源列类型）；clean 名只能引用已注册规则；
      3. 冷层建表 = 加 object_binding 自动建表（G-014）；
      4. 绑定后必须 reprofile 复检（闭环验证混装/空值率改善）；
      5. 干净表退化 [cold_table, bind_object, reprofile]；
      6. 本函数无 IO 副作用。
    """
    steps: list[dict] = []
    mixed_cols = [c.name for c in profile.columns if c.mixed]
    sparse_cols = [c.name for c in profile.columns if c.null_rate >= 0.5]
    typed_cols = [(c.name, _prop_type(c)) for c in profile.columns
                  if _prop_type(c) in ("decimal", "date", "integer", "boolean")]
    has_candidates = bool(profile.candidates)

    # 退化判定：无混装、无高空值列、无候选关联 → 干净表三步
    if not mixed_cols and not sparse_cols and not has_candidates:
        return [
            {"step": "cold_table",
             "why": f"干净表：{profile.row_count} 行、无混装、无高空值列、无候选关联",
             "how": "外部表入冷层 parquet 后，加 object_binding（source 指向冷层路径）即自动建表（G-014）",
             "done_when": "obj_<新对象> 表存在且行数=冷层行数"},
            {"step": "bind_object",
             "why": "对象草案 properties 类型已由值类型映射给出",
             "how": "人工审核 output/drafts 下 objects/bindings 草案，复制进 ontology/<pack>/ 后 build_ontology",
             "done_when": "build_ontology 通过且 loader 无硬失败"},
            {"step": "reprofile",
             "why": "闭环复检（绑定后画像验证）",
             "how": "重跑 OntologyProfiler / build_table_profile",
             "done_when": "新对象各属性空值率/混装与接入前画像一致或改善"},
        ]

    if mixed_cols:
        steps.append({
            "step": "split_mixed",
            "why": f"混装列（归一落点不明确）：{mixed_cols}；先拆分才能绑定，"
                   "否则探测/连线同时报两个方向（G-015）",
            "how": "按 landing_suggestions 拆分为多列（如账号列/人名列），或在源端归一；"
                   "loader 无 split 清洗规则，不得在 clean 里编造规则名",
            "done_when": "重跑 build_table_profile，各列 mixed=False 且落点唯一"})
    steps.append({
        "step": "clean_rules",
        "why": ("建表前清洗（CTAS 用源列类型）：空白/全角/千分位等噪声；"
                + (f"高空值列 {sparse_cols} 需核对口径；" if sparse_cols else "")),
        "how": f"clean 只能引用已注册规则 {sorted(CLEAN_RULE_NAMES)}（如 strip）；"
               "需要其他清洗时在 core.ontology 注册新规则后再引用，不准编造名字",
        "done_when": "bindings.json object_binding.clean 全部 ∈ 已注册规则名，loader 校验通过"})
    if typed_cols:
        steps.append({
            "step": "type_cast",
            "why": f"结构化列需 CAST 物化：{[(n, t) for n, t in typed_cols]}",
            "how": "object_binding.source_sql 中 CAST（amount→DOUBLE/decimal、date_str→DATE、"
                   "number→BIGINT）；编译器对结构化 source 编译期 CAST",
            "done_when": "obj_ 表列类型与 objects.json properties 声明一致（非全 VARCHAR）"})
    steps.append({
        "step": "cold_table",
        "why": f"外部表 {profile.table_name}（{profile.row_count} 行）需入冷层",
        "how": "经 data_ingest 适配器入 data/*.parquet（L3 冷层）；加 object_binding 即自动建表（G-014）",
        "done_when": "冷层 parquet 存在且 object_binding.source 指向它"})
    steps.append({
        "step": "bind_object",
        "why": "对象草案待人工审核生效",
        "how": "审核 output/drafts/<table>/objects.draft.json（对象名/pk/name_property/metadata_props），"
               "复制进 ontology/<pack>/objects.json 后 build_ontology",
        "done_when": "build_ontology 通过、对象名非 ext_TODO_RENAME 占位"})
    if has_candidates:
        steps.append({
            "step": "bind_links",
            "why": (f"候选关联 {len(profile.candidates)} 条（overlap ≥ 阈值）："
                    + "、".join(f"{c.col}→{c.target_obj}.{c.target_prop}"
                                f"({c.overlap_ratio:.0%})" for c in profile.candidates)),
            "how": "审核 links/bindings 草案：双列方案（raw 保留 + <col>_id 外键），"
                   "link_binding.build_sql 用 LEFT JOIN obj_<target> 归一",
            "done_when": "build_ontology 后 lnk_<link> 行数与左表一致（LEFT JOIN 不丢边）"})
    steps.append({
        "step": "reprofile",
        "why": "闭环复检：验证混装已拆分、空值率改善、候选关联已连线",
        "how": "重跑 build_table_profile + OntologyProfiler；健康度诊断 profile_missing_column/"
               "map_normalize_gap 清零",
        "done_when": "新画像无 mixed 列、归一缺口 0、关联链接已物化"})

    # 按 STEP_ORDER 排序（去重保序）
    order = {s: i for i, s in enumerate(STEP_ORDER)}
    steps.sort(key=lambda s: order.get(s["step"], 99))
    return steps
