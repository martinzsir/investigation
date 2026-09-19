"""
core/wujian.py
五间研判词汇运行时（P6 解耦）：packs/wujian 词汇包的装载、自校验与全局注册。

设计（见 .trae/documents/研判能力插件化_实施方案_v3.md §3.2 / P6）：
  P6 前 jians.json 寄生在 ontology/<pack>/，ontology_loader 装载主链路硬依赖
  load_jians → allowed_jian → _parse_jian；底座无法脱离五间独立装载。
  P6 把五间词汇（间类定义 / 对象类型→间类映射 / cross_levels 升格名 /
  source_independence 同源对 / 数据源展示名）整体搬到 packs/wujian，
  本模块是它唯一的装载器与校验器——**不再寄生 ontology_loader**。

  - WujianPack  : 一份校验通过的五间词汇（不可变），供融合层反查注入、
                  计分/升格/画像/图谱等消费；
  - 全局注册表  : pack_loader.discover() 扫描到 provides.vocabulary.type="wujian"
                  的包时注册/刷新；包拔出时对账注销。
  - 无包        : load_wujian() 返回 None；线索 jian_types 留空，交叉页以
                  缺口形式展示，不报错（底座与关系/时间镜头照常工作）。

红线（与旧 loader 口径一致）：
  cross_levels 的 min_independent_sources 仅允许 1/2/3（映射不可配置，名称可配）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACKS_DIR = ROOT / "packs"


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class JianDecl:
    """单个间类声明。"""
    name: str
    default_clearance: int
    weight: int
    source_object_types: tuple[str, ...]


@dataclass(frozen=True)
class CrossLevel:
    """交叉升格等级：min_independent_sources（红线 1/2/3）→ 展示名。"""
    min_independent_sources: int
    name: str


@dataclass(frozen=True)
class WujianPack:
    """一份校验通过的五间词汇，绑定到某个 ontology 案件包（如 default）。"""
    ontology_pack: str
    jians: tuple[JianDecl, ...]
    cross_levels: tuple[CrossLevel, ...]
    related_pairs: tuple[tuple[str, str], ...]
    source_names: dict[str, str]      # 对象/链接类型 → 数据源展示名

    # ---- 间类顺序/集合 ----
    @property
    def jian_order(self) -> list[str]:
        return [j.name for j in self.jians]

    @property
    def jian_set(self) -> frozenset[str]:
        return frozenset(self.jian_order)

    # ---- 反向映射：对象/链接类型 → 间类列表 ----
    @property
    def type_to_jians(self) -> dict[str, list[str]]:
        m: dict[str, list[str]] = {}
        for j in self.jians:
            for t in j.source_object_types:
                m.setdefault(t, []).append(j.name)
        return m

    def jians_for_types(self, types: Iterable[str]) -> list[str]:
        """按 evidence_refs 源对象类型集合反查间类，结果按 jian_order 排序去重。"""
        rev = self.type_to_jians
        hit: set[str] = set()
        for t in types:
            hit.update(rev.get(t, ()))
        return [name for name in self.jian_order if name in hit]

    # ---- 升格 ----
    def cross_level_name(self, n: int) -> str | None:
        """独立源数 → 升格名（n 非 1/2/3 返回 None）。"""
        for lv in self.cross_levels:
            if lv.min_independent_sources == n:
                return lv.name
        return None

    # ---- 权重 / 密级 ----
    @property
    def jian_weights(self) -> dict[str, int]:
        return {j.name: j.weight for j in self.jians}

    @property
    def jian_clearances(self) -> dict[str, int]:
        return {j.name: j.default_clearance for j in self.jians}

    def source_name_for(self, type_name: str, fallback: str = "") -> str:
        return self.source_names.get(type_name, fallback)

    # ---- rules jian 标签校验（P6 ⑤：下沉到本包）----
    def validate_rule_jian_types(self, jian_types: Iterable[str]) -> None:
        """规则 jian_types 必须全部在本包间类内；非法抛 ValueError。空列表放行。"""
        bad = [j for j in jian_types if j not in self.jian_set]
        if bad:
            raise ValueError(
                f"jian_types 非法：{bad}，五间包 {self.ontology_pack} 仅允许 "
                f"{self.jian_order}")


# ----------------------------------------------------------------------
# 全局注册表（pack_loader 自枚举时维护）
# ----------------------------------------------------------------------

_REGISTRY: dict[str, WujianPack] = {}


def register_wujian(wp: WujianPack) -> None:
    """注册/刷新某 ontology 包的五间词汇（幂等替换）。"""
    _REGISTRY[wp.ontology_pack] = wp


def unregister_wujian(ontology_pack: str) -> None:
    """注销（五间包拔出）；不存在静默忽略。"""
    _REGISTRY.pop(ontology_pack, None)


def load_wujian(ontology_pack: str = "default") -> WujianPack | None:
    """取已挂载的五间词汇；未安装返回 None（调用方降级为缺口）。"""
    return _REGISTRY.get(ontology_pack)


def offered_packs() -> set[str]:
    """当前已注册五间词汇的 ontology 包名集合。"""
    return set(_REGISTRY)


def reset_wujian() -> None:
    """测试/重置用。"""
    _REGISTRY.clear()


# ----------------------------------------------------------------------
# 装载 + 自校验（注册期硬失败，只拒该包）
# ----------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_wujian(pack_dir: Path | str,
                 ontology_pack: str = "default",
                 jians_file: str = "jians.json") -> WujianPack:
    """从五间包目录读取并校验词汇；声明非法一律 ValueError（注册期硬失败）。

    校验（移植自旧 ontology_loader）：
      - jians 非空、name 非空且不重复；
      - cross_levels：min_independent_sources ∈ {1,2,3}（红线）、name 非空；
      - related_pairs：a/b 为非空字符串、成对不相同，且引用已声明的
        source_object_types；
      - source_names（可选）：键必须是已声明的对象/链接类型。
    """
    pdir = Path(pack_dir)
    data = _read_json(pdir / jians_file)

    # ---- jians ----
    raw_jians = data.get("jians", [])
    jians: list[JianDecl] = []
    seen: set[str] = set()
    for i, j in enumerate(raw_jians):
        if not isinstance(j, dict):
            raise ValueError(f"jians[{i}] 必须为对象")
        name = j.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"jians[{i}] 缺非空 name")
        name = name.strip()
        if name in seen:
            raise ValueError(f"间类名重复：{name}")
        seen.add(name)
        types = j.get("source_object_types", []) or []
        if not isinstance(types, list):
            raise ValueError(f"间类 {name} source_object_types 必须为数组")
        jians.append(JianDecl(
            name=name,
            default_clearance=int(j.get("default_clearance", 1)),
            weight=int(j.get("weight", 1)),
            source_object_types=tuple(types),
        ))
    if not jians:
        raise ValueError("五间声明为空：至少需要一个间类")

    # ---- cross_levels（红线 1/2/3）----
    raw_levels = data.get("cross_levels", []) or []
    levels: list[CrossLevel] = []
    allowed_sources = {1, 2, 3}
    for i, lv in enumerate(raw_levels):
        if not isinstance(lv, dict):
            raise ValueError(f"cross_levels[{i}] 必须为对象")
        src = lv.get("min_independent_sources")
        lname = lv.get("name")
        if src not in allowed_sources:
            raise ValueError(
                f"cross_levels[{i}] min_independent_sources={src} 非法，"
                f"仅允许 {sorted(allowed_sources)}（红线：映射不可配置）")
        if not isinstance(lname, str) or not lname.strip():
            raise ValueError(f"cross_levels[{i}] 缺非空 name")
        levels.append(CrossLevel(src, lname.strip()))

    # ---- source_independence.related_pairs ----
    universe = {t for j in jians for t in j.source_object_types}
    sec = data.get("source_independence") or {}
    if not isinstance(sec, dict):
        raise ValueError("source_independence 必须为对象")
    raw_pairs = sec.get("related_pairs", [])
    if not isinstance(raw_pairs, list):
        raise ValueError("source_independence.related_pairs 必须为数组")
    pairs: list[tuple[str, str]] = []
    for i, pr in enumerate(raw_pairs):
        if not isinstance(pr, dict):
            raise ValueError(f"related_pairs[{i}] 必须为对象")
        a, b = pr.get("a"), pr.get("b")
        if not isinstance(a, str) or not a.strip():
            raise ValueError(f"related_pairs[{i}] 缺非空字符串 a")
        if not isinstance(b, str) or not b.strip():
            raise ValueError(f"related_pairs[{i}] 缺非空字符串 b")
        a, b = a.strip(), b.strip()
        if a == b:
            raise ValueError(f"related_pairs[{i}] a/b 不得相同：{a}")
        missing = [x for x in (a, b) if universe and x not in universe]
        if missing:
            raise ValueError(
                f"related_pairs[{i}] 引用了未声明的 source_object_types："
                f"{missing}（全集 {sorted(universe)}）")
        pairs.append((a, b))

    # ---- source_names（可选：类型 → 数据源展示名）----
    raw_names = data.get("source_names", {}) or {}
    if not isinstance(raw_names, dict):
        raise ValueError("source_names 必须为对象")
    source_names: dict[str, str] = {}
    for t, disp in raw_names.items():
        if t not in universe:
            raise ValueError(
                f"source_names 键 {t!r} 不是已声明的 source_object_types"
                f"（全集 {sorted(universe)}）")
        source_names[t] = str(disp)

    return WujianPack(
        ontology_pack=ontology_pack,
        jians=tuple(jians),
        cross_levels=tuple(levels),
        related_pairs=tuple(pairs),
        source_names=source_names,
    )
