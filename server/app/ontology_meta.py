"""
server/app/ontology_meta.py
Server 侧 ontology 声明读助手（六项解耦 R5/R6 共用）。

- resolve_base：案件快照包存在则读快照，否则整体回落内置包（与
  ontology-config 端点同一规则，避免跨源拼接）；
- jian/level 元数据：供线索读面、候补池、看板做声明驱动的密级/等级判定。
"""
from __future__ import annotations

from pathlib import Path

from core.ontology_loader import (
    load_cross_levels,
    load_dimension_declarations,
    load_jians,
    load_pack,
    load_states,
)


def resolve_base(pack: str, base_dir: str | Path | None) -> Path | None:
    """快照包目录存在 → 返回快照根；否则 None（loader 回落内置 PACK_ROOT）。"""
    if base_dir is None:
        return None
    snap = Path(base_dir)
    return snap if (snap / pack).is_dir() else None


def jian_clearances(pack: str = "default",
                    base_dir: str | Path | None = None) -> dict[str, int]:
    """{间类全名: default_clearance}。"""
    base = resolve_base(pack, base_dir)
    return {j["name"]: int(j.get("default_clearance", 0))
            for j in load_jians(pack, base)}


def jian_names(pack: str = "default",
               base_dir: str | Path | None = None) -> list[str]:
    base = resolve_base(pack, base_dir)
    return [j["name"] for j in load_jians(pack, base)]


def cross_level_names(pack: str = "default",
                      base_dir: str | Path | None = None) -> list[str]:
    """交叉等级名（按 min_independent_sources 升序，红线 1/2/3 由 loader 钉死）。"""
    base = resolve_base(pack, base_dir)
    levels = load_cross_levels(pack, base)
    return [lv["name"] for lv in
            sorted(levels, key=lambda x: x["min_independent_sources"])]


def dimension_names(pack: str = "default",
                    base_dir: str | Path | None = None) -> list[str]:
    base = resolve_base(pack, base_dir)
    return [d["name"] for d in load_dimension_declarations(pack, base)]


def dispose_action_names(pack: str = "default",
                         base_dir: str | Path | None = None) -> set[str]:
    """线索处置类动作名（side_effects 含 set_clue_status；R6 替硬编码白名单）。"""
    base = resolve_base(pack, base_dir)
    spec = load_pack(pack, base)
    return {name for name, a in spec.actions.items()
            if "set_clue_status" in a.side_effects}


def states_decl(pack: str = "default",
                base_dir: str | Path | None = None) -> dict:
    """load_states 透传（自动快照/内置回落）。"""
    base = resolve_base(pack, base_dir)
    return load_states(pack, base)
