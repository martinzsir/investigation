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
