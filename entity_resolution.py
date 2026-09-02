"""
entity_resolution.py —— 人名实体对齐（根包模块，由 core.entity 按绝对路径加载）。

问题背景：
  同一个人在不同数据源里写法各异：
    - "张卫国" vs "张卫国（董事长）" vs "张 卫 国"
    - 共享手机号的两条记录几乎必是同一人
  若不对齐，资金链会在「同名不同写法」处断裂。

分层算法（与 core.entity.OrganizationResolver 对称）：
  1. 共享精确证据（手机号）            → 强合并 confidence=1.0
  2. 规范化后精确匹配                  → 强合并 confidence=1.0
  3. 别名字典（业务已知）              → 强合并 confidence=1.0
  4. 拼音相似 / 编辑距离 / 前缀包含    → 仅标 needs_review 候选，不自动合并

红线（与组织对齐完全相同）：
  AI 只做候选推荐 + 证据打分，最终合并须由具名正兵在 core.review 工作台确认。

对外暴露：
  EntityResolver          主类（ingest / add_aliases / resolve / mapping / report / review_candidates）
  EntityCluster           合并后的等价类
  PersonEvidence          合并证据（共享手机号 + 溯源行）
  normalize_person_name   人名规范化（NFKC / 去空白 / 去括号备注）

注：本模块只依赖标准库（pypinyin 可选，缺失时退化编辑距离），
    不 import core 包，避免循环引用 —— core 反向加载本模块。
"""
from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Dict, List

# 可选依赖：拼音相似度（INSTALL.md：不装则退化为编辑距离，模糊匹配精度略降）
try:
    from pypinyin import lazy_pinyin
    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False

_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
_PAREN_RE = re.compile(r"[（(].*?[）)]")

# 前缀包含匹配的最短人名核心长度（中文姓名至少 2 字）
_MIN_CORE_LEN = 2


# ----------------------------------------------------------------------
# 1. 人名规范化
# ----------------------------------------------------------------------
def normalize_person_name(raw) -> str:
    """
    NFKC 全角→半角 → 去空白噪声 → 去括号备注 → 小写。
    "张卫国（董事长）" → "张卫国"；"张 卫 国" → "张卫国"。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    s = unicodedata.normalize("NFKC", s)
    if _HAS_CJK.search(s):
        s = re.sub(r"\s+", "", s)   # 含中文：空白属噪声
    else:
        s = re.sub(r"\s+", " ", s).strip()
    # 去括号备注（张卫国（董事长）→ 张卫国）
    s = _PAREN_RE.sub("", s)
    return s.lower()


# ----------------------------------------------------------------------
# 2. 相似度：拼音（可选）+ 编辑距离兜底
# ----------------------------------------------------------------------
def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _pinyin_key(name: str) -> str:
    """中文名转拼音串（无 pypinyin 时返回空串，由调用方退化）。"""
    if not _HAS_PYPINYIN or not name:
        return ""
    try:
        return "".join(lazy_pinyin(name))
    except Exception:
        return ""


def _name_similarity(a: str, b: str) -> float:
    """0~1 相似度：优先拼音串编辑距离比，退化原串编辑距离比。"""
    if not a or not b:
        return 0.0
    ka, kb = _pinyin_key(a), _pinyin_key(b)
    if ka and kb:
        a, b = ka, kb
    dist = _edit_distance(a, b)
    return 1.0 - dist / max(len(a), len(b))


# ----------------------------------------------------------------------
# 3. 数据结构
# ----------------------------------------------------------------------
@dataclass
class PersonEvidence:
    """合并证据：哪些共同属性支持「是同一人」。"""
    common_phones: List[str] = field(default_factory=list)      # 共享手机号
    common_source_rows: List[str] = field(default_factory=list) # 溯源行


@dataclass
class EntityCluster:
    """一个合并后的人名实体（等价类）。"""
    entity_id: str
    canonical_name: str             # 标准名（出现频次最高 / 别名字典指定）
    variants: List[str] = field(default_factory=list)   # 所有原始写法
    evidence: PersonEvidence = field(default_factory=PersonEvidence)
    confidence: float = 1.0
    needs_review: bool = False      # 模糊相似 / 前缀包含等弱证据 → True
    merge_reason: str = ""          # 合并依据（供正兵复核）

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------
# 4. 主类
# ----------------------------------------------------------------------
class EntityResolver:
    """
    人名实体对齐入口。

    用法：
        er = EntityResolver(fuzzy_threshold=0.85)
        er.add_aliases({"张卫国": ["张卫国（董事长）"]})
        er.ingest([{"name": "张卫国", "phone": "138...", "source_row_id": "r1"}])
        clusters = er.resolve()
        mapping = er.mapping()
    """

    # 强合并所需的精确证据键（当前仅手机号；可按需扩展身份证/账号）
    STRONG_KEYS = ("phone",)

    def __init__(self, fuzzy_threshold: float = 0.85):
        self.fuzzy_threshold = fuzzy_threshold
        self._records: List[dict] = []
        self._alias_map: Dict[str, str] = {}      # normalized_variant → normalized_canon
        self._canon_origin: Dict[str, str] = {}   # normalized_canon → 原始 canon
        self._clusters: List[EntityCluster] = []
        self._resolved = False

    # ---- 配置 ----
    def add_aliases(self, alias_dict: Dict[str, List[str]]):
        """业务已知别名：canonical → [variant, ...]"""
        for canon, variants in alias_dict.items():
            norm_canon = normalize_person_name(canon)
            self._canon_origin[norm_canon] = canon
            for v in variants:
                nv = normalize_person_name(v)
                self._alias_map[nv] = norm_canon
            self._alias_map[norm_canon] = norm_canon

    # ---- 数据接入 ----
    def ingest(self, records: List[dict]):
        """records 字段：name(必填), phone(可选), source_row_id(可选)，其余键忽略。"""
        for r in records:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            self._records.append({
                "name": name,
                "phone": _norm_attr(r.get("phone")),
                "source_row_id": r.get("source_row_id", ""),
            })
        self._resolved = False

    # ---- 核心：分层合并 ----
    def resolve(self) -> List[EntityCluster]:
        # 第 1、2、3 层：规范化分组（别名优先）
        groups: Dict[str, List[dict]] = {}
        for rec in self._records:
            norm = normalize_person_name(rec["name"])
            norm = self._alias_map.get(norm, norm)
            groups.setdefault(norm, []).append(rec)

        # 注入人工 canon（使 canonical 名优选人工确认的标准名）
        for norm, recs in groups.items():
            if norm in self._canon_origin:
                canon_name = self._canon_origin[norm]
                if not any(r["name"] == canon_name for r in recs):
                    recs.append({
                        "name": canon_name, "phone": "", "source_row_id": "", "_is_canon": True,
                    })

        # 逐组合并：同组即同一实体（规范化一致 / 别名 / 共享手机号在组内聚合）
        self._clusters = []
        for norm, recs in groups.items():
            has_alias = norm in self._canon_origin
            ev = _collect_person_evidence(recs)
            conf = _person_confidence(recs, ev, has_alias=has_alias)
            self._clusters.append(EntityCluster(
                entity_id=_make_id(_pick_canonical(recs)),
                canonical_name=_pick_canonical(recs),
                variants=sorted({r["name"] for r in recs if not r.get("_is_canon")}),
                evidence=ev,
                confidence=conf,
                needs_review=conf < 1.0,
                merge_reason="强证据(共享手机号/规范化一致)" if conf >= 1.0 else "规范化名称一致(待复核)",
            ))

        # 跨组：共享手机号 → 强合并（并查集）
        self._clusters = _merge_by_shared_phone(self._clusters, self._records)

        # 第 4 层：模糊相似 / 前缀包含 → 仅标记候选（不自动合并）
        self._fuzzy_link()

        self._clusters.sort(key=lambda c: c.confidence, reverse=True)
        self._resolved = True
        return self._clusters

    def _fuzzy_link(self):
        """
        跨簇模糊匹配：拼音相似 >= fuzzy_threshold，或前缀包含（短名 >= 2 字）。
        命中仅标 needs_review，作为候选供正兵确认 —— 严禁自动合并。
        例：张卫国 ↔ 张卫国弟（前缀）；张卫国 ↔ 张伟国（拼音近）。
        """
        n = len(self._clusters)
        for i in range(n):
            a = self._clusters[i]
            core_a = normalize_person_name(a.canonical_name)
            if len(core_a) < _MIN_CORE_LEN:
                continue
            for j in range(n):
                if i == j:
                    continue
                b = self._clusters[j]
                core_b = normalize_person_name(b.canonical_name)
                if core_a == core_b:
                    continue
                similar = (
                    core_b.startswith(core_a) or core_a.startswith(core_b)
                    or _name_similarity(core_a, core_b) >= self.fuzzy_threshold
                )
                if similar:
                    a.needs_review = True
                    b.needs_review = True
                    if not a.merge_reason or a.merge_reason == "规范化名称一致(待复核)":
                        a.merge_reason = f"模糊相似候选：{a.canonical_name} ↔ {b.canonical_name}(需正兵确认)"

    # ---- 结果 ----
    def mapping(self) -> Dict[str, str]:
        """原始人名 → canonical_name（仅返回强合并，模糊候选不入映射）。"""
        if not self._resolved:
            self.resolve()
        m: Dict[str, str] = {}
        for c in self._clusters:
            if c.confidence >= 1.0:   # 只暴露经确认的合并
                for v in c.variants:
                    m[v] = c.canonical_name
        return m

    def clusters(self) -> List[EntityCluster]:
        if not self._resolved:
            self.resolve()
        return self._clusters

    def review_candidates(self) -> List[EntityCluster]:
        """待正兵确认的人名对齐候选（needs_review=True 的簇）。"""
        return [c for c in self.clusters() if c.needs_review]

    def report(self) -> dict:
        clusters = self.clusters()
        return {
            "total_records": len(self._records),
            "total_entities": len(clusters),
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
# 内部工具（与 core.entity 对称）
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


def _collect_person_evidence(recs: List[dict]) -> PersonEvidence:
    ev = PersonEvidence()
    ev.common_phones = sorted({r["phone"] for r in recs if r["phone"]})
    ev.common_source_rows = [r["source_row_id"] for r in recs if r["source_row_id"]]
    return ev


def _person_confidence(recs: List[dict], ev: PersonEvidence, has_alias: bool = False) -> float:
    """共享手机号 或 人工别名 或 单一写法 = 1.0；多种写法仅靠名称归一 = 0.9。"""
    if ev.common_phones:
        return 1.0
    if has_alias:
        return 1.0
    if len({r["name"] for r in recs}) == 1:
        return 1.0
    return 0.9


def _records_for_cluster(cluster: EntityCluster, all_records: List[dict]) -> List[dict]:
    names = set(cluster.variants)
    return [r for r in all_records if r["name"] in names]


def _merge_by_shared_phone(clusters: List[EntityCluster],
                           all_records: List[dict]) -> List[EntityCluster]:
    """跨规范化组：共享手机号的簇做并查集强合并。"""
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

    def phones(recs):
        return {r["phone"] for r in recs if r["phone"]}

    for i in range(n):
        for j in range(i + 1, n):
            if phones(_records_for_cluster(clusters[i], all_records)) & \
               phones(_records_for_cluster(clusters[j], all_records)):
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    merged: List[EntityCluster] = []
    for root, idxs in groups.items():
        if len(idxs) == 1:
            merged.append(clusters[idxs[0]])
            continue
        recs: List[dict] = []
        for idx in idxs:
            recs.extend(_records_for_cluster(clusters[idx], all_records))
        canon = _pick_canonical(recs)
        ev = PersonEvidence()
        for idx in idxs:
            oev = clusters[idx].evidence
            ev.common_phones = sorted(set(ev.common_phones) | set(oev.common_phones))
            ev.common_source_rows = sorted(set(ev.common_source_rows) | set(oev.common_source_rows))
        merged.append(EntityCluster(
            entity_id=_make_id(canon),
            canonical_name=canon,
            variants=sorted({v for idx in idxs for v in clusters[idx].variants}),
            evidence=ev, confidence=1.0, needs_review=False,
            merge_reason="跨组精确证据(共享手机号)强合并",
        ))
    return merged


def _make_id(name: str) -> str:
    import hashlib
    return "person_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10]
