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
    load_pack,
    load_scoring,
    load_semantic_roles,
    load_states,
)
from core.wujian import load_wujian

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

    # P6：兵法五间/交叉等级来自全局五间词汇（packs/wujian），无包则下发空骨架，
    # 前端据此把五间页渲染为"词汇未安装"缺口（配置端点恒可用，不报错）。
    wj = load_wujian(pack)
    jians_out = [{
        "name": jd.name,
        "default_clearance": jd.default_clearance,
        "weight": jd.weight,
        "source_object_types": list(jd.source_object_types),
    } for jd in wj.jians] if wj is not None else []
    levels_out = [{
        "min_independent_sources": lv.min_independent_sources,
        "name": lv.name,
    } for lv in wj.cross_levels] if wj is not None else []

    return {
        "pack": pack,
        # 兵法五间（线索 jian_types；内间权限过滤口径）
        "wujian_available": wj is not None,
        "jians": jians_out,
        "cross_levels": levels_out,
        # 侦查五维/数据通道（线索 detail.dimension；雷达/房间色板口径）
        "dimensions": load_dimension_declarations(pack, base),
        "states": states_decl["states"],
        "transitions": {frm: sorted(tos)
                        for frm, tos in states_decl["transitions"].items()},
        "actions": actions,
        "scoring": load_scoring(pack, base),
        # 业务事件时间字段（本体 semantic:event_time 声明）。
        # 前端据此把时间轴从「研判过程时间」切到「业务发生时间」——
        # 声明缺失下发空结构，前端回落过程时间（不静默算错）。
        "time_fields": _assemble_time_fields(pack, base),
    }


def _assemble_time_fields(pack: str, base) -> dict[str, Any]:
    """业务时间字段索引（按对象分组 + 全量去重）。

    返回语义属性名（英文），与规范化后的 Function 输出列名对齐。
    源表中文列名 → 语义名的映射在 bindings.json，由接入层完成，
    这里只给画布提供「哪个属性是业务时间」。
    """
    try:
        roles = load_semantic_roles(pack, base)
    except Exception:
        return {"objects": {}, "event_time": []}
    objects = roles.get("objects") or {}
    return {
        "objects": {
            name: list((r or {}).get("event_time") or [])
            for name, r in objects.items()
            if (r or {}).get("event_time")
        },
        "event_time": list(roles.get("event_time") or []),
    }
