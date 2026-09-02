"""
core/entity.py
组织层级对齐（任务 2）：把「子公司 / 母公司 / 分支机构 / 项目部」归并到统一法人主体。

问题背景：
  同一法人主体在不同数据源里常以下挂单位出现，若只做人名/字号精确匹配会漏并：
    - "宏业建设有限公司"（中标方） vs "宏业建设第一项目部"（合同付款方）
    - "A建材（集团）" vs "A建材北京分公司" vs "A建材朝阳区分公司"
    - "财政局" vs "杭州市财政局" vs "杭州市财政局预算处"
  这类「同一法人的组织层级变体」比人名别名更常见，是资金链断裂的高频原因。

设计原则（与人名对齐一致，复用 entity_resolution 的分层算法）：
  1. 共享统一社会信用代码 / 注册地址 / 法定代表人 / 银行账号 → 强合并（confidence=1.0）
  2. 规范化后精确匹配（剥组织后缀、去括号、去"有限/集团/分公司/项目部"）→ 合并
  3. 别名字典（业务已知：宏业建设 = 宏业建设第一项目部）→ 强合并
  4. 前缀包含（短名是长名的前缀，如 "宏业建设" ⊂ "宏业建设有限公司"）→ 候选，需正兵复核
  红线：与人名对齐完全相同 —— AI 只做候选推荐 + 证据打分，最终合并须由正兵确认。

对外暴露：
  OrganizationResolver         主类（ingest / add_aliases / resolve / report / mapping）
  normalize_org_name           组织名规范化（剥层级后缀）
  build_org_table_from_duckdb  从工商信息表采集组织记录
  apply_org_to_duckdb          把 canonical 列回写到业务表（复用 entity_bridge 思路）
"""
from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Set, Tuple, Optional
from pathlib import Path

from .store import Store  # 复用 Store 的 query/execute


# ----------------------------------------------------------------------
# 1. 组织名规范化：剥除层级后缀
# ----------------------------------------------------------------------
# 优先级从高到低（长后缀先剥，避免 "有限公司" 先吃掉 "股份有限责任公司"）
_ORG_SUFFIXES = [
    "股份有限公司", "股份有限责任公司", "有限责任公司", "有限合伙", "普通合伙",
    "（集团）", "（集团)", "(集团)", "（有限）",
    "集团有限公司", "集团有限责任公司",
    "有限公司", "分公司", "子公司",
    "第一项目部", "第二项目部", "第三项目部", "项目部",
    "第一分公司", "第二分公司", "第三分公司",
    "北京分公司", "上海分公司", "广州分公司", "深圳分公司",
    "浙江分公司", "杭州分公司",
]
# 去括号备注：财政局（预算处）/ 宏业建设（华东区）
_PAREN_RE = re.compile(r"[（(].*?[）)]")
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")

# 前缀包含匹配的短名集合（常见法人核心字号，命中即候选）
# 长度太短易误并（如 "宏业" ⊂ "宏业兄弟科技"），设最小核心字号长度
_MIN_CORE_LEN = 4


def normalize_org_name(raw: str) -> str:
    """
    把组织各种写法归一为一个可比对的标准字符串。
    规则：NFKC 全角→半角 → 去空白噪声 → 剥层级后缀 → 去括号备注 → 小写。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    s = unicodedata.normalize("NFKC", s)
    if _HAS_CJK.search(s):
        s = re.sub(r"\s+", "", s)   # 含中文：空白属噪声
    else:
        s = re.sub(r"\s+", " ", s).strip()

    # 剥层级后缀（按长度降序）
    for suf in sorted(_ORG_SUFFIXES, key=len, reverse=True):
        s = s.replace(suf, "")
    # 去括号备注（财政局（预算处）→ 财政局）
    s = _PAREN_RE.sub("", s)
    return s.lower()


# ----------------------------------------------------------------------
# 2. 组织层级关系
# ----------------------------------------------------------------------
@dataclass
class OrgEvidence:
    """合并证据：哪些共同属性支持「是同一法人」。"""
    common_credit_codes: List[str] = field(default_factory=list)   # 统一社会信用代码
    common_legal_reps: List[str] = field(default_factory=list)    # 法定代表人
    common_addresses: List[str] = field(default_factory=list)     # 注册地址
    common_accounts: List[str] = field(default_factory=list)      # 银行账号
    common_source_rows: List[str] = field(default_factory=list)   # 溯源行


@dataclass
class OrgCluster:
    """一个合并后的法人主体（等价类）。"""
    entity_id: str
    canonical_name: str             # 标准名（人工确认 / 出现频次最高者）
    variants: List[str] = field(default_factory=list)   # 所有原始写法
    evidence: OrgEvidence = field(default_factory=OrgEvidence)
    confidence: float = 1.0
    needs_review: bool = False      # 前缀包含等弱证据 → True
    merge_reason: str = ""          # 合并依据（供正兵复核）

    def to_dict(self) -> dict:
        return asdict(self)


class OrganizationResolver:
    """
    组织层级对齐入口。

    用法：
        org = OrganizationResolver()
        org.add_aliases({"宏业建设": ["宏业建设第一项目部", "宏业建设（集团）"]})
        org.ingest(records)     # records: [{name, credit_code, legal_rep, address, account, source_row_id}]
        clusters = org.resolve()
        mapping = org.mapping()
    """

    # 强合并所需的精确证据键
    STRONG_KEYS = ("credit_code", "legal_rep", "address", "account")

    def __init__(self, prefix_threshold: float = 0.9):
        self.prefix_threshold = prefix_threshold
        self._records: List[dict] = []
        self._alias_map: Dict[str, str] = {}      # normalized_variant → normalized_canon
        self._canon_origin: Dict[str, str] = {}   # normalized_canon → 原始 canon
        self._clusters: List[OrgCluster] = []
        self._resolved = False

    # ---- 配置 ----
    def add_aliases(self, alias_dict: Dict[str, List[str]]):
        """业务已知别名：canonical → [variant, ...]"""
        for canon, variants in alias_dict.items():
            norm_canon = normalize_org_name(canon)
            self._canon_origin[norm_canon] = canon
            for v in variants:
                nv = normalize_org_name(v)
                self._alias_map[nv] = norm_canon
            self._alias_map[norm_canon] = norm_canon

    # ---- 数据接入 ----
    def ingest(self, records: List[dict]):
        """records 字段：name(必填), credit_code, legal_rep, address, account, source_row_id"""
        for r in records:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            self._records.append({
                "name": name,
                "credit_code": _norm_attr(r.get("credit_code")),
                "legal_rep": _norm_attr(r.get("legal_rep")),
                "address": _norm_attr(r.get("address")),
                "account": _norm_attr(r.get("account")),
                "source_row_id": r.get("source_row_id", ""),
            })
        self._resolved = False

    # ---- 核心：分层合并 ----
    def resolve(self) -> List[OrgCluster]:
        # 第 1、2、3 层：规范化分组（别名优先）
        groups: Dict[str, List[dict]] = {}
        for rec in self._records:
            norm = normalize_org_name(rec["name"])
            norm = self._alias_map.get(norm, norm)
            groups.setdefault(norm, []).append(rec)

        # 注入人工 canon（使 canonical 名优选人工确认的标准名）
        for norm, recs in groups.items():
            if norm in self._canon_origin:
                canon_name = self._canon_origin[norm]
                if not any(r["name"] == canon_name for r in recs):
                    recs.append({
                        "name": canon_name, "credit_code": "", "legal_rep": "",
                        "address": "", "account": "", "source_row_id": "", "_is_canon": True,
                    })

        # 逐组合并：精确证据 + 别名 → 强合并
        self._clusters = []
        for norm, recs in groups.items():
            has_alias = norm in self._canon_origin
            ev = _collect_org_evidence(recs)
            conf = _org_confidence(recs, ev, has_alias=has_alias)
            self._clusters.append(OrgCluster(
                entity_id=_make_id(_pick_canonical(recs)),
                canonical_name=_pick_canonical(recs),
                variants=sorted({r["name"] for r in recs if not r.get("_is_canon")}),
                evidence=ev,
                confidence=conf,
                needs_review=conf < 1.0,
                merge_reason="强证据(信用代码/法人/地址/账号)" if conf >= 1.0 else "规范化名称一致(待复核)",
            ))

        # 第 4 层：跨组前缀包含匹配 → 仅标记候选（不自动合并）
        self._prefix_link()

        # 精确证据跨组强合并（共享信用代码/法人等）
        self._clusters = _merge_by_shared_strong(self._clusters, self._records)

        # 第 4b 层：簇内前缀包含检测（必须在强合并之后）
        # 场景：强合并后同一簇内，某原始写法（剥组织后缀后）是另一原始写法的核心字号真子集
        # 例：泰和建材 ⊂ 泰和建材公司（共享信用代码已并入同簇，但存在层级扩展写法）
        # → 标 needs_review，作为「层级扩展 / 别名」候选供正兵确认。
        # 注：须放在 _merge_by_shared_strong 之后，否则规范化不同的 variants 尚未归入同簇。
        self._intracluster_prefix_link()

        self._clusters.sort(key=lambda c: c.confidence, reverse=True)
        self._resolved = True
        return self._clusters

    def _prefix_link(self):
        """
        前缀包含：短名是长名的前缀（且去除组织后缀后核心字号 >= 最小长度）→ 标记 needs_review。
        示例：canon="宏业建设"，另一簇="宏业建设第一项目部" → 候选（已在同组则忽略）。
        """
        n = len(self._clusters)
        for i in range(n):
            a = self._clusters[i]
            core_a = normalize_org_name(a.canonical_name)
            if len(core_a) < _MIN_CORE_LEN:
                continue
            for j in range(n):
                if i == j:
                    continue
                b = self._clusters[j]
                core_b = normalize_org_name(b.canonical_name)
                if core_a == core_b:
                    continue
                # a 是 b 的核心（b 以后缀/层级词扩展）
                if core_b.startswith(core_a) or core_a.startswith(core_b):
                    a.needs_review = True
                    b.needs_review = True
                    if not a.merge_reason or a.merge_reason == "规范化名称一致(待复核)":
                        a.merge_reason = f"前缀包含候选：{a.canonical_name} ↔ {b.canonical_name}(需正兵确认)"

    def _intracluster_prefix_link(self):
        """
        簇内前缀包含：同一规范化簇内，某原始写法（剥组织后缀后）是另一原始写法的核心字号真子集
        → 标记该簇 needs_review，作为「层级扩展 / 别名」候选供正兵确认。

        必要性：规范化一致的两个写法（"泰和建材" vs "泰和建材公司"）在 _prefix_link 里
        属于同一簇、core 完全相同，跨簇逻辑无法捕获；此处比对原始名，补齐这一盲区。
        """
        for cluster in self._clusters:
            variants = [v for v in cluster.variants if v]
            if len(variants) < 2:
                continue
            # 两两比对原始名（剥组织后缀后）
            marked = False
            for idx_i in range(len(variants)):
                core_i = normalize_org_name(variants[idx_i])
                if len(core_i) < _MIN_CORE_LEN:
                    continue
                for idx_j in range(len(variants)):
                    if idx_i == idx_j:
                        continue
                    core_j = normalize_org_name(variants[idx_j])
                    if core_j.startswith(core_i) or core_i.startswith(core_j):
                        # 排除「完全相同」的退化情形（已去重，此处为前缀真子集）
                        if core_i != core_j:
                            marked = True
                            break
                if marked:
                    break
            if marked:
                cluster.needs_review = True
                if not cluster.merge_reason or "前缀" not in cluster.merge_reason:
                    cluster.merge_reason = (
                        f"簇内前缀包含候选：{cluster.canonical_name} 存在层级扩展写法(需正兵确认)"
                    )

    # ---- 结果 ----
    def mapping(self) -> Dict[str, str]:
        """原始组织名 → canonical_name（仅返回强合并，模糊候选不入映射）。"""
        if not self._resolved:
            self.resolve()
        m: Dict[str, str] = {}
        for c in self._clusters:
            if c.confidence >= 1.0:   # 只暴露经确认的合并
                for v in c.variants:
                    m[v] = c.canonical_name
        return m

    def clusters(self) -> List[OrgCluster]:
        if not self._resolved:
            self.resolve()
        return self._clusters

    def review_candidates(self) -> List[OrgCluster]:
        """待正兵确认的组织对齐候选（needs_review=True 的簇 + 跨簇前缀对）。"""
        return [c for c in self.clusters() if c.needs_review]

    def report(self) -> dict:
        clusters = self.clusters()
        return {
            "total_records": len(self._records),
            "total_org_entities": len(clusters),
            "merged_count": sum(len(c.variants) - 1 for c in clusters),
            "strong_merges": [
                {"canonical": c.canonical_name, "variants": c.variants, "reason": c.merge_reason}
                for c in clusters if c.confidence >= 1.0 and len(c.variants) > 1
            ],
            "review_candidates": [
                {"canonical": c.canonical_name, "variants": c.variants, "reason": c.merge_reason}
                for c in clusters if c.needs_review
            ],
            "clusters": [c.to_dict() for c in clusters],
        }


# ----------------------------------------------------------------------
# 便捷函数：从 DuckDB 工商表采集
# ----------------------------------------------------------------------
def build_org_table_from_duckdb(
    store_or_conn,
    table: str = "工商信息",
    cols: Optional[Dict[str, str]] = None,
) -> OrganizationResolver:
    """
    从 DuckDB 工商信息表采集组织记录。
    cols = {"name": "主体", "credit_code": "统一社会信用代码", "legal_rep": "法定代表人",
            "address": "注册地址", "account": "银行账号"}
    未提供的列自动用空字符串，不影响强合并（只要有一项精确证据即可）。
    """
    conn = getattr(store_or_conn, "conn", store_or_conn)
    cols = cols or {"name": "主体"}

    # 列存在性校验：不同数据源的工商信息表列名各异
    # （有的是"统一社会信用代码/法定代表人/注册地址"，有的是"法人/状态/关联"）。
    # 缺失列自动跳过并降级（该维度证据为空），避免因列名不符直接抛 BinderException。
    try:
        available = {r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()}
    except Exception:
        available = set(cols.values())

    select = []
    name_col = cols.get("name", "主体")
    if name_col not in available:
        raise ValueError(f'表 "{table}" 缺少名称列 "{name_col}"，可用列={sorted(available)}')
    select.append(f'"{name_col}" AS name')

    for attr, dbcol in cols.items():
        if attr == "name":
            continue
        if dbcol in available:
            select.append(f'"{dbcol}" AS {attr}')
        else:
            # 补空列保持 schema 一致（该维度证据为空，仅靠其他证据判定）
            select.append(f"CAST(NULL AS VARCHAR) AS {attr}")
    select.append("ROW_NUMBER() OVER () AS source_row_id")
    rows = conn.execute(f'SELECT {", ".join(select)} FROM "{table}"').fetchall()
    col_names = [d[0] for d in conn.description]
    records = [dict(zip(col_names, r)) for r in rows]
    org = OrganizationResolver()
    org.ingest(records)
    org.resolve()
    return org


def apply_org_to_duckdb(store_or_conn, org: OrganizationResolver,
                        tables: List[str], name_columns: List[str]) -> int:
    """
    把 canonical_组织 列回写到业务表（与 entity_bridge.apply_to_duckdb 对称）。
    仅回写 confidence>=1.0 的强合并；前缀候选保留原名，等正兵确认后再合入。
    返回新增 canonical 列涉及的组织名数量。
    """
    conn = getattr(store_or_conn, "conn", store_or_conn)
    strong = org.mapping()
    if not strong:
        return 0
    conn.execute("DROP TABLE IF EXISTS _org_map_tmp")
    conn.execute('CREATE TEMP TABLE _org_map_tmp (raw VARCHAR, canonical VARCHAR)')
    conn.executemany("INSERT INTO _org_map_tmp VALUES (?, ?)", list(strong.items()))
    applied = 0
    for table in tables:
        # 列存在性校验：不同业务表 schema 各异（如 招投标档案 只有"中标方"没有"主体"）。
        # 缺失列跳过而非抛错，保证「部分表对齐」不阻断整条管线。
        try:
            available = {r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()}
        except Exception:
            continue   # 表不存在则跳过该表
        for col in name_columns:
            if col not in available:
                continue
            canon_col = f"canonical_org_{col}"
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{canon_col}" VARCHAR')
            conn.execute(f"""
                UPDATE "{table}"
                SET "{canon_col}" = COALESCE(
                    (SELECT canonical FROM _org_map_tmp WHERE raw = "{col}"),
                    "{col}"
                )
            """)
            applied += 1
    conn.execute("DROP TABLE IF EXISTS _org_map_tmp")
    # 语义：返回实际回写的 (表, 列) 组合数（此前返回 mapping 长度，与"回写"语义不符）
    return applied


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _norm_attr(v) -> str:
    if v is None:
        return ""
    return unicodedata.normalize("NFKC", str(v)).strip().lower()


def _pick_canonical(recs: List[dict]) -> str:
    freq: Dict[str, int] = {}
    for r in recs:
        freq[r["name"]] = freq.get(r["name"], 0) + 1
    def key(name):
        has_cjk = bool(_HAS_CJK.search(name))
        return (has_cjk, freq[name], -len(name))
    return max(freq, key=key)


def _collect_org_evidence(recs: List[dict]) -> OrgEvidence:
    ev = OrgEvidence()
    ev.common_credit_codes = sorted({r["credit_code"] for r in recs if r["credit_code"]})
    ev.common_legal_reps = sorted({r["legal_rep"] for r in recs if r["legal_rep"]})
    ev.common_addresses = sorted({r["address"] for r in recs if r["address"]})
    ev.common_accounts = sorted({r["account"] for r in recs if r["account"]})
    ev.common_source_rows = [r["source_row_id"] for r in recs if r["source_row_id"]]
    return ev


def _org_confidence(recs: List[dict], ev: OrgEvidence, has_alias: bool = False) -> float:
    """共享精确证据 或 人工别名 = 1.0；仅靠名称归一 = 0.9。"""
    if ev.common_credit_codes or ev.common_legal_reps or ev.common_addresses or ev.common_accounts:
        return 1.0
    if has_alias:
        return 1.0
    if len({r["name"] for r in recs}) == 1:
        return 1.0
    return 0.9


def _share_strong_org(a_recs: List[dict], b_recs: List[dict]) -> bool:
    def keys(recs, k):
        return {r[k] for r in recs if r[k]}
    for attr in OrganizationResolver.STRONG_KEYS:
        if keys(a_recs, attr) & keys(b_recs, attr):
            return True
    return False


def _records_for_cluster(cluster: OrgCluster, all_records: List[dict]) -> List[dict]:
    names = set(cluster.variants)
    return [r for r in all_records if r["name"] in names]


def _merge_by_shared_strong(clusters: List[OrgCluster],
                            all_records: List[dict]) -> List[OrgCluster]:
    """跨规范化组：共享精确证据（信用代码/法人）的簇做并查集合并。"""
    n = len(clusters)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(n):
        for j in range(i + 1, n):
            if _share_strong_org(_records_for_cluster(clusters[i], all_records),
                                  _records_for_cluster(clusters[j], all_records)):
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    merged: List[OrgCluster] = []
    for root, idxs in groups.items():
        if len(idxs) == 1:
            merged.append(clusters[idxs[0]])
            continue
        recs: List[dict] = []
        for idx in idxs:
            recs.extend(_records_for_cluster(clusters[idx], all_records))
        canon = _pick_canonical(recs)
        ev = OrgEvidence()
        for idx in idxs:
            oev = clusters[idx].evidence
            ev.common_credit_codes = sorted(set(ev.common_credit_codes) | set(oev.common_credit_codes))
            ev.common_legal_reps = sorted(set(ev.common_legal_reps) | set(oev.common_legal_reps))
            ev.common_addresses = sorted(set(ev.common_addresses) | set(oev.common_addresses))
            ev.common_accounts = sorted(set(ev.common_accounts) | set(oev.common_accounts))
            ev.common_source_rows = sorted(set(ev.common_source_rows) | set(oev.common_source_rows))
        merged.append(OrgCluster(
            entity_id=_make_id(canon),
            canonical_name=canon,
            variants=sorted({v for idx in idxs for v in clusters[idx].variants}),
            evidence=ev, confidence=1.0, needs_review=False,
            merge_reason="跨组精确证据(信用代码/法人/地址/账号)强合并",
        ))
    return merged


def _make_id(name: str) -> str:
    import hashlib
    return "org_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10]


# ----------------------------------------------------------------------
# 接口统一：为根包 EntityResolver（人名）动态注入 review_candidates()
# 说明：OrganizationResolver 自身已实现 review_candidates；人名用的 EntityResolver
# （entity_resolution.py）只有 needs_review 属性。为避免修改上游源码，这里统一注入。
# ----------------------------------------------------------------------
def _review_candidates(self):
    """返回 needs_review=True 的簇（按簇是否仅含模糊候选判定）。"""
    if not hasattr(self, "_resolved") or not self._resolved:
        self.resolve()
    return [c for c in self._clusters if getattr(c, "needs_review", False)]


def _report(self):
    """兼容：聚合人名簇为 report dict（与原 entity_resolution.EntityResolver.report 一致）。"""
    if hasattr(self, "report") and callable(getattr(self, "report", None)):
        return self.report()
    # 兜底构造（用于注入后的 person resolver）
    clusters = self._clusters if hasattr(self, "_clusters") else []
    return {
        "total_records": len(getattr(self, "_records", [])),
        "total_entities": len(clusters),
        "merged_count": sum(len(c.variants) - 1 for c in clusters),
        "needs_review": [c.canonical_name for c in clusters if getattr(c, "needs_review", False)],
        "strong_merges": [c.canonical_name for c in clusters if c.confidence >= 1.0 and len(c.variants) > 1],
        "clusters": [c.to_dict() for c in clusters],
    }


def _load_person_resolver():
    """
    定位并导入根包的 EntityResolver。
    搜索顺序：
      1. 优先使用 ROOT_PACKAGE_DIR / 'entity_resolution.py'（与 core 同级的 workspace 根）
         —— 用绝对路径加载，避免被「同名 skills / 其他包」遮蔽 sys.path。
      2. 回退到普通 import（兼容 pip 安装的场景）。
    返回 EntityResolver 类，找不到则返回 None（人名对齐降级）。
    """
    import importlib.util
    here = Path(__file__).resolve().parent          # .../core
    candidates = [
        here.parent / "entity_resolution.py",        # ① workspace 根（推荐）
        Path("/data/workspace/entity_resolution.py"),  # ② 绝对兜底
    ]
    for src in candidates:
        if src.exists():
            mod_name = f"_person_resolver_{src.stem}"
            # 缓存：已加载则复用同一模块对象。
            # 否则每次调用都会 exec 出新模块，导致 _inject_person_api 打的补丁
            # （review_candidates/report）作用在一个对象上、实例却来自另一个 → AttributeError。
            cached = sys.modules.get(mod_name)
            if cached is not None and hasattr(cached, "EntityResolver"):
                return getattr(cached, "EntityResolver")
            spec = importlib.util.spec_from_file_location(mod_name, str(src))
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # 关键：必须先注册进 sys.modules 再 exec_module。
            # dataclasses 在处理 @dataclass 时需要 sys.modules[cls.__module__].__dict__，
            # 未注册会抛 AttributeError: 'NoneType' object has no attribute '__dict__'。
            sys.modules[mod_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(mod_name, None)
                continue
            return getattr(module, "EntityResolver", None)
    # 回退：普通 import（pip 安装场景）
    try:
        from entity_resolution import EntityResolver as _ER  # type: ignore
        return _ER
    except ImportError:
        return None


def _inject_person_api():
    """
    给根包 EntityResolver 补上 review_candidates / report（与 OrgCluster 接口对齐），
    供 ReviewQueue.from_resolvers(person_resolver=...) 统一消费。
    容错：entity_resolution 不可用时跳过，不影响组织对齐主流程。
    """
    _PersonER = _load_person_resolver()
    if _PersonER is None:
        return  # 人名对齐可选；缺失时仅 person review 不可用
    if not hasattr(_PersonER, "review_candidates"):
        _PersonER.review_candidates = _review_candidates
    # report：仅在没有原生 report() 时注入兜底
    if not callable(getattr(_PersonER, "report", None)):
        _PersonER.report = _report


# 模块导入时自动注入（幂等，重复 import 无副作用）
_inject_person_api()


# ----------------------------------------------------------------------
# 与现有人名对齐的联合入口（供 main 管线调用）
# ----------------------------------------------------------------------
def run_entity_resolution(store: Store, alias_dict: Optional[Dict[str, List[str]]] = None,
                          org_alias_dict: Optional[Dict[str, List[str]]] = None) -> dict:
    """
    一站式实体对齐：人名（person）+ 组织（org）一起跑，产出统一映射。
    接入位置：DataIngestManager 之后、首轮 skill_invoke 之前。

    返回：
        {"person": EntityResolver, "org": OrganizationResolver,
         "person_mapping": {...}, "org_mapping": {...}}
    """
    # 延迟导入：避免 core 包循环引用
    from .registry import _resolve_person_from_store  # 见 registry.py 新增函数
    person_resolver = _resolve_person_from_store(store)
    if alias_dict:
        person_resolver.add_aliases(alias_dict)
    person_resolver.resolve()

    org_resolver = build_org_table_from_duckdb(
        store, table="工商信息",
        # 列名按数据源实际 schema（工商信息表为 主体/法人/状态/关联）；
        # 缺失列由 build_org_table_from_duckdb 自动降级为空证据，不阻断流程。
        cols={"name": "主体", "legal_rep": "法人", "address": "关联"},
    )
    if org_alias_dict:
        org_resolver.add_aliases(org_alias_dict)
    org_resolver.resolve()

    return {
        "person": person_resolver,
        "org": org_resolver,
        "person_mapping": person_resolver.mapping(),
        "org_mapping": org_resolver.mapping(),
    }
