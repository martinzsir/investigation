"""
server/app/ontology_config_view.py
Ontology 配置下发（六项解耦·共性基建 P0）：把案件包声明（兵法五间 / 侦查五维 /
交叉等级 / 状态机 / 动作 / 计分）组装成前端可直接消费的一份只读配置。

纯读 core 加载器，不查 DuckDB、不读产物；案件快照 ontology 根作 base_dir，
与 research/disposal 路由同一取数口径。间类名称全量下发（前端要画热力/雷达
骨架），受护间的命中数据仍由线索读面按角色过滤——本端点不按角色删配置节。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.ontology_loader import (
    load_dimension_declarations,
    load_cross_levels,
    load_jians,
    load_pack,
    load_scoring,
    load_states,
)

from server.app.ontology_meta import resolve_base

# 仅把线索处置类动作下发给状态机 UI（side_effects 含 set_clue_status）；
# review_merge/review_reject 等实体裁决动作有独立工作台，不进线索状态机。
_DISPOSE_SIDE_EFFECT = "set_clue_status"


def assemble_ontology_config(*, pack: str,
                             base_dir: str | Path) -> dict[str, Any]:
    # 案件快照包未随 BUILD 落盘时（未 BUILD 案件）整体回落内置包根，
    # 保证配置端点恒可用；快照存在则全节同源读快照，避免跨源拼接。
    base = resolve_base(pack, base_dir)
    states_decl = load_states(pack, base)
    spec = load_pack(pack, base)

    actions = []
    for spec_action in spec.actions.values():
        if _DISPOSE_SIDE_EFFECT not in spec_action.side_effects:
            continue
        action = {
            "name": spec_action.name,
            "title": spec_action.title or spec_action.name,
            "target_status": spec_action.target_status,
            "allowed_from": list(spec_action.allowed_from),
            "requires_role": spec_action.requires_role,
            "terminal": spec_action.terminal,
            "parameters": [
                {"name": p.name, "type": p.type, "required": p.required,
                 "description": p.description}
                for p in spec_action.parameters
            ],
        }
        if spec_action.only_from:
            action["only_from"] = list(spec_action.only_from)
        actions.append(action)

    return {
        "pack": pack,
        # 兵法五间（线索 jian_types；内间权限过滤口径）
        "jians": load_jians(pack, base),
        "cross_levels": load_cross_levels(pack, base),
        # 侦查五维/数据通道（线索 detail.dimension；雷达/房间色板口径）
        "dimensions": load_dimension_declarations(pack, base),
        "states": states_decl["states"],
        "transitions": {frm: sorted(tos)
                        for frm, tos in states_decl["transitions"].items()},
        "actions": actions,
        "scoring": load_scoring(pack, base),
    }
