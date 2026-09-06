"""
entity_resolution.py —— 人名实体对齐（根包模块，由 core.entity 按绝对路径加载）。

问题背景：
  同一个人在不同数据源里写法各异：
    - "张卫国" vs "张卫国（董事长）" vs "张 卫 国"
    - 共享手机号的两条记录几乎必是同一人
  若不对齐，资金链会在「同名不同写法」处断裂。

分层算法（与 core.entity.OrganizationResolver 对称）：
  1. 共享精确证据（手机号/身份证）    → 强合并 confidence=1.0
  2. 规范化后精确匹配                  → 强合并 confidence=1.0
     ★红线 R-1：规范化同名组内若存在 ≥2 组互不连通的强证据（不同电话/不同身份证），
       强制拆为独立簇并全部标 needs_review——同名 ≠ 同一人，
       严禁以「名字相同」为唯一依据高置信静默合并（同名不同人会被并错全部流水）。
  3. 别名字典（业务已知）              → 强合并 confidence=1.0（人工确认即强证据，豁免拆簇）
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
    common_id_cards: List[str] = field(default_factory=list)    # 共享身份证（可遮蔽格式）
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

    # 强合并所需的精确证据键（红线 R-1 后扩展身份证；共享任一键=强证据）
    STRONG_KEYS = ("phone", "id_card")

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
        """records 字段：name(必填), phone/id_card(可选强证据), source_row_id(可选)，其余键忽略。"""
        for r in records:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            self._records.append({
                "name": name,
                "phone": _norm_attr(r.get("phone")),
                "id_card": _norm_attr(r.get("id_card")),
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

        # 逐组合并：同组即同一实体（规范化一致 / 别名 / 组内共享强证据聚合）
        # ★红线 R-1：同名组内强证据互斥（≥2 组互不连通的电话/身份证）→ 强制拆簇待裁决
        self._clusters = []
        for norm, recs in groups.items():
            has_alias = norm in self._canon_origin
            subs = None if has_alias else _split_group_by_strong_keys(recs, self.STRONG_KEYS)
            if subs is None:
                ev = _collect_person_evidence(recs)
                conf = _person_confidence(recs, ev, has_alias=has_alias)
                cluster = EntityCluster(
                    entity_id=_make_id(_pick_canonical(recs)),
                    canonical_name=_pick_canonical(recs),
                    variants=sorted({r["name"] for r in recs if not r.get("_is_canon")}),
                    evidence=ev,
                    confidence=conf,
                    needs_review=conf < 1.0,
                    merge_reason="强证据(共享手机号/规范化一致)" if conf >= 1.0 else "规范化名称一致(待复核)",
                )
                cluster._recs = recs   # 供跨组合并精确取记录（拆簇后按名回捞会串簇）
                self._clusters.append(cluster)
            else:
                self._clusters.extend(_build_split_clusters(recs, subs, self.STRONG_KEYS))

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
    ev.common_id_cards = sorted({r.get("id_card", "") for r in recs if r.get("id_card")})
    ev.common_source_rows = [r["source_row_id"] for r in recs if r["source_row_id"]]
    return ev


def _split_group_by_strong_keys(recs: List[dict],
                                strong_keys) -> "List[List[dict]] | None":
    """同名组内按强证据连通分量划分（红线 R-1 核心）。

    两条记录共享任一非空强证据值（同电话/同身份证）→ 同分量。
    返回 None：强证据全连通 / 带强证据记录不足 2 条 → 维持既有整组合并；
    返回分量列表（len>=2）：红线触发，同名多簇。无强证据的记录各自成单例簇
    （归属无法自动判定，交人工裁决）。_is_canon 注入记录不参与分区。
    """
    keyed = [(i, r) for i, r in enumerate(recs)
             if not r.get("_is_canon") and any(r.get(k) for k in strong_keys)]
    if len(keyed) < 2:
        return None
    parent = list(range(len(keyed)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first: Dict[tuple, int] = {}
    for pos in range(len(keyed)):
        for k in strong_keys:
            v = keyed[pos][1].get(k)
            if not v:
                continue
            j = first.setdefault((k, v), pos)
            ri, rj = find(pos), find(j)
            if ri != rj:
                parent[rj] = ri

    comps: Dict[int, List[dict]] = {}
    for pos in range(len(keyed)):
        comps.setdefault(find(pos), []).append(keyed[pos][1])
    if len(comps) < 2:
        return None
    keyed_set = {id(r) for _, r in keyed}
    subs = list(comps.values())
    subs.extend([[r] for r in recs
                 if not r.get("_is_canon") and id(r) not in keyed_set])
    return subs


def _split_entity_id(canon: str, recs: List[dict], idx: int, strong_keys) -> str:
    """拆簇实体 id：同名多簇不能共享 _make_id(canon)（md5 同名碰撞）。"""
    keys = sorted({v for r in recs for k in strong_keys if (v := r.get(k))})
    if keys:
        return _make_id(f"{canon}|{'|'.join(keys)}")
    rows = sorted({r.get("source_row_id") or "" for r in recs if r.get("source_row_id")})
    if rows:
        return _make_id(f"{canon}|{';'.join(rows)}")
    return _make_id(f"{canon}|#{idx}")


def _build_split_clusters(recs: List[dict], subs: List[List[dict]],
                          strong_keys) -> List["EntityCluster"]:
    """红线 R-1：同名组拆为多个独立簇，全部 needs_review（禁止同名无强证据自动合并）。"""
    canon_rec = next((r for r in recs if r.get("_is_canon")), None)
    conflict = sorted({v for r in recs for k in strong_keys if (v := r.get(k))})
    hint = ";".join(conflict[:4]) + ("…" if len(conflict) > 4 else "")
    out: List[EntityCluster] = []
    for i, sub in enumerate(subs):
        sub_recs = sub + [canon_rec] if canon_rec is not None else list(sub)
        canon = _pick_canonical(sub_recs)
        cluster = EntityCluster(
            entity_id=_split_entity_id(canon, sub, i, strong_keys),
            canonical_name=canon,
            variants=sorted({r["name"] for r in sub if not r.get("_is_canon")}),
            evidence=_collect_person_evidence(sub_recs),
            confidence=1.0 if len(sub) > 1 else 0.9,
            needs_review=True,   # 红线：同名拆簇一律人工裁决，禁止进自动映射
            merge_reason=(f"同名歧义拆簇(红线R-1)：组内 {len(subs)} 组互斥强证据[{hint}]，"
                          "归属待正兵裁决"),
        )
        cluster._recs = sub_recs
        cluster._split_flag = True
        out.append(cluster)
    return out


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
    """跨规范化组：共享手机号的簇做并查集强合并。

    记录取自簇上挂载的 _recs（resolve 时挂载）——同名拆簇后多个簇 variants 相同，
    按名字回捞会把两个李强的记录混在一起、误判共享手机号而重新合并（红线失效）。
    """
    def recs_of(c: EntityCluster) -> List[dict]:
        recs = getattr(c, "_recs", None)
        return recs if recs is not None else _records_for_cluster(c, all_records)

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
            if phones(recs_of(clusters[i])) & phones(recs_of(clusters[j])):
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
            recs.extend(recs_of(clusters[idx]))
        canon = _pick_canonical(recs)
        ev = PersonEvidence()
        for idx in idxs:
            oev = clusters[idx].evidence
            ev.common_phones = sorted(set(ev.common_phones) | set(oev.common_phones))
            ev.common_id_cards = sorted(set(ev.common_id_cards) | set(oev.common_id_cards))
            ev.common_source_rows = sorted(set(ev.common_source_rows) | set(oev.common_source_rows))
        # 同名拆簇成员被跨组合并后仍保留人工裁决标记（同名歧义不因强合并消失）
        split_members = any(getattr(clusters[idx], "_split_flag", False) for idx in idxs)
        member_ids = sorted({clusters[idx].entity_id for idx in idxs})
        merged.append(EntityCluster(
            entity_id=_make_id(canon + "|" + "|".join(member_ids)),
            canonical_name=canon,
            variants=sorted({v for idx in idxs for v in clusters[idx].variants}),
            evidence=ev, confidence=1.0, needs_review=split_members,
            merge_reason="跨组精确证据(共享手机号)强合并"
                         + ("；含同名拆簇成员，保留待裁决" if split_members else ""),
        ))
        merged[-1]._recs = recs
    return merged


def _make_id(name: str) -> str:
    import hashlib
    return "person_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10]
