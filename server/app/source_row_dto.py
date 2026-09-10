"""
server/app/source_row_dto.py
B1 溯源行适配器：把线索 source_rows 转为溯源面板字段表（含中文名、hit 标记、遮蔽）。

source_rows 实际有两种形态（兼容历史产物）：
  A. 纯数据 dict：{raw_name: "张卫国", legal_rep: "李志强", ...}
     —— 检测器产出的 pairs/rows（当前 demo_case 的形态），直接转字段表
  B. URI 字符串 / {row_uri: "通话记录@v3#p0/8f3a21"}
     —— 走 row_uri.py resolve_row_uri 取回归档行内容（未来 BUILD 产 URI 后生效）

遮蔽在服务端做（红线 FE-T-021：明文不入缓存）——调 PolicyEngine.apply_row_masks。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.access import AccessContext
from core.policy import PolicyEngine
from core.row_uri import parse_row_uri, resolve_row_uri, RowNotFoundError, MalformedUriError


def resolve_source_row(
    *, source_row: dict[str, Any],
    conn=None,
    pack_id: str = "default",
    base_dir: Path | None = None,
    access: AccessContext | None = None,
    hit_fields: list[str] | None = None,
) -> dict[str, Any]:
    """把单条 source_row 转为溯源面板字段表。

    参数：
      source_row  : 线索 source_rows 的一项（纯数据 dict 或含 row_uri 的 dict）
      conn        : DuckDB 只读连接（URI 取回需要；纯数据 dict 不需要）
      pack_id     : ontology 案件包名
      base_dir    : ontology 根目录（默认 d:/dev/inves_duckdb）
      access      : 访问上下文（角色/权限，决定遮蔽策略）
      hit_fields  : 命中判据的字段名列表（前端高亮琥珀）

    返回：
      {"dataset": str|None, "fields": [{name, raw, value, hit, policy}]}
      URI 取回失败（跨版本/归档缺失）时 dataset=None、fields 从 source_row 本身取。
    """
    pe = PolicyEngine(pack_id) if access else None

    # ---- 提取行数据 + dataset ----
    dataset = None
    row_data: dict[str, Any] = {}
    obj_type = None

    uri = source_row.get("row_uri") if isinstance(source_row, dict) else None
    if uri and conn:
        # 路径 B：URI 取回归档行
        try:
            resolved = resolve_row_uri(conn, uri)
            row_data = resolved.get("data", {})
            dataset = resolved.get("dataset")
            # 按 dataset 找 binding → obj_type（用于策略引擎）
            obj_type = _dataset_to_obj_type(dataset, pack_id, base_dir)
        except (RowNotFoundError, MalformedUriError):
            row_data = {k: v for k, v in source_row.items() if k != "row_uri"}
    else:
        # 路径 A：纯数据 dict 直接用
        row_data = dict(source_row)

    # obj_type 未知时，按字段名反查（属性集匹配度最高的对象）
    if not obj_type:
        obj_type = _infer_obj_type(row_data.keys(), pe, base_dir)

    # ---- 属性名 → 中文名映射 ----
    name_map = _build_name_map(obj_type, pack_id, base_dir)

    # ---- 遮蔽 ----
    masked_row = row_data
    if pe and obj_type and access:
        masked_row = pe.apply_row_masks(access, obj_type, [row_data])[0]

    # ---- 组装字段表 ----
    hit_set = set(hit_fields or [])
    fields = []
    for raw_name, value in row_data.items():
        # 跳过内部字段
        if raw_name in _INTERNAL_FIELDS:
            continue
        display_name = name_map.get(raw_name, raw_name)
        policy = _field_policy(pe, obj_type, raw_name, access)
        fields.append({
            "name": display_name,
            "raw": raw_name,
            "value": _stringify(masked_row.get(raw_name, value)),
            "hit": raw_name in hit_set,
            "policy": policy,
        })

    return {"dataset": dataset, "fields": fields}


def resolve_source_rows(
    *, source_rows: list[dict[str, Any]],
    conn=None, pack_id: str = "default",
    base_dir: Path | None = None,
    access: AccessContext | None = None,
    hit_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """批量转换 source_rows → 溯源面板字段表列表。"""
    return [
        resolve_source_row(
            source_row=sr, conn=conn, pack_id=pack_id,
            base_dir=base_dir, access=access, hit_fields=hit_fields,
        )
        for sr in source_rows
    ]


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
_INTERNAL_FIELDS = frozenset({
    "row_uri", "knowledge_sources", "knowledge_version",
    "matched_person", "source_row_id",
})


def _infer_obj_type(field_names, pe: PolicyEngine | None,
                    base_dir: Path | None) -> str | None:
    """按字段名集反查对象类型。

    策略：先查 policies.json property_policies（敏感属性如 id_card 有 (person, id_card)），
    再查 objects.json 属性集匹配度。
    """
    fields = set(field_names)

    # 1. property_policies 反查：行含 id_card → person
    if pe:
        for (obj, prop) in pe.property_policies:
            if prop in fields:
                return obj

    # 2. objects.json 属性集匹配度
    objects = _load_objects(pe.pack if pe else "default", base_dir)
    best, best_score = None, 0
    for obj in objects.get("objects", []):
        props = set(obj.get("properties", {}).keys())
        overlap = len(fields & props)
        if overlap > best_score:
            best, best_score = obj.get("name"), overlap
    return best


def _dataset_to_obj_type(dataset: str | None, pack_id: str,
                          base_dir: Path | None) -> str | None:
    """按 dataset（源表名）从 bindings.json 找对应的 object name。"""
    if not dataset:
        return None
    bindings = _load_bindings(pack_id, base_dir)
    for ob in bindings.get("object_bindings", []):
        st = ob.get("source_table") or _table_from_source(ob.get("source", {}))
        if st == dataset:
            return ob.get("object")
    return None


def _table_from_source(source: dict) -> str | None:
    """从 source.table 取源表名。"""
    return source.get("table") if isinstance(source, dict) else None


def _build_name_map(obj_type: str | None, pack_id: str,
                    base_dir: Path | None) -> dict[str, str]:
    """属性名 → 中文名（objects.json title + data_elements.json）。

    回落：属性名本身就是中文（如 raw_name → "raw_name"）时原样返回。
    """
    if not obj_type:
        return {}
    objects = _load_objects(pack_id, base_dir)
    obj = next((o for o in objects.get("objects", []) if o.get("name") == obj_type), None)
    if not obj:
        return {}
    # objects.json 属性名通常是英文 raw_name，中文名在 data_elements
    # 但当前 objects.json 属性名直接是列别名（如 caller_raw），无 data_element 引用
    # 回落到列名本身（已是中文别名如 "主体"→"caller_raw" 映射在 bindings）
    return {}  # 属性名直接用作展示名（英文 raw 名 → hover 显示）


def _field_policy(pe: PolicyEngine | None, obj_type: str | None,
                  prop: str, access: AccessContext | None) -> str:
    """字段策略：visible / masked / denied。"""
    if pe is None or obj_type is None or access is None:
        return "visible"
    if access.is_system:
        return "visible"
    rule = pe.property_rule(obj_type, prop)
    if rule is None:
        return "visible"
    if pe.can_read_property(access, obj_type, prop):
        return "visible"
    return "masked" if rule.get("mask") == "partial" else "denied"


def _stringify(value: Any) -> str:
    """值统一转字符串（list/dict 转 JSON）。"""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


# ----------------------------------------------------------------------
# ontology 文件缓存（轻量，不入 core/ontology_loader 避免循环）
# ----------------------------------------------------------------------
_cache: dict[str, dict] = {}


def _load_json(filename: str, pack_id: str, base_dir: Path | None) -> dict:
    key = f"{pack_id}/{filename}"
    if key in _cache:
        return _cache[key]
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent.parent
    p = root / "ontology" / pack_id / filename
    if not p.exists():
        _cache[key] = {}
        return {}
    _cache[key] = json.loads(p.read_text(encoding="utf-8"))
    return _cache[key]


def _load_bindings(pack_id: str, base_dir: Path | None) -> dict:
    return _load_json("bindings.json", pack_id, base_dir)


def _load_objects(pack_id: str, base_dir: Path | None) -> dict:
    return _load_json("objects.json", pack_id, base_dir)
