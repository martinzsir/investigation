"""
server/app/routers/ontology_overview.py
S1/F1 本体总览 API：GET /cases/{case_id}/ontology/overview

本体散在 4 个导航组 8 个编辑页（S1 PRD §2.1），本接口一次给出
19 个本体声明文件的结构化清单，供本体管理器壳的左栏文件树消费：

  name / group / writable / has_schema / source_layer / item_count / updated_at
  + status（UC-S1-1 七字段之外的状态位，E1-3/E1-4 局部降级用）

设计决策（S1 PRD §13）：
  - D5 writable/group 用静态映射表（0.7 节实测 8 可写/11 只读），
    不做运行时反射探测；
  - D6 单文件缺失/解析失败局部降级（status=missing/parse_error），
    不阻断整体加载；
  - D8 来源层缺失不报错：source_layer 只列实际存在的层
    （shared/_shared → industry/_industry/<行业> → case/案件快照），
    E3-1 无行业层时只显示全域/案件两层。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends

from server.app.deps import get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["ontology-overview"])

# 语义六组（S1 PRD §5.2，D4：不按文件名平铺）
ONTOLOGY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("对象模型", ("objects", "links", "views")),
    ("数据", ("data_elements", "bindings")),
    ("研判", ("rules", "functions", "verify_playbooks", "dimensions")),
    ("治理", ("policies", "actions", "states", "llm_policy")),
    ("知识", ("case_knowledge", "enum_space", "thresholds")),
    ("计分", ("scoring", "jians", "derived_properties")),
)

# writable 静态映射（实测有后端写路由的文件；S4 起 actions/states 开放案件域可视化编辑；
# S5 起 7 个通用文件走「提案→影响面→人工发布」。llm_policy 永不开放写——
# S5 仅有只读 JSON 展示，无写路由、无表单，R4/R6）
_WRITABLE_FILES = frozenset({
    "objects", "links", "data_elements", "bindings",
    "rules", "policies", "views", "case_knowledge",
    "actions", "states",
    # S5：通用 schema 驱动表单（提案→影响面→人工发布）承载的文件
    "derived_properties", "dimensions", "enum_space", "jians",
    "scoring", "thresholds", "verify_playbooks",
})

# 条目计数时跳过的元信息键（不算"条目"）
_META_KEYS = frozenset({
    "schema_version", "_note", "pack", "description",
    "network", "llm_enabled", "knowledge_version",
})


def _schema_dir() -> Path:
    """仓库根 schemas/（本文件位于 server/app/routers/ 下，向上三级）。"""
    return Path(__file__).resolve().parents[3] / "schemas"


def _has_schema(name: str) -> bool:
    return (_schema_dir() / f"{name}.schema.json").is_file()


def _item_count(data) -> int:
    """条目数：list 直取；dict 累加集合型值（list/dict）长度，跳过元信息键。"""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return sum(len(v) for k, v in data.items()
                   if k not in _META_KEYS and isinstance(v, (list, dict)))
    return 0


def _industry_of(snap_dir: Path) -> str | None:
    """案件快照 pack_meta.json 的 industry 字段（与建案拷贝同源）。"""
    meta_path = snap_dir / "pack_meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")).get("industry")
    except (json.JSONDecodeError, OSError, AttributeError):
        return None


def _file_entry(name: str, group: str, snap_dir: Path, base_dir: Path,
                industry: str | None) -> dict:
    """单文件总览项（E1-3 缺失 / E1-4 解析失败局部降级，不抛出）。"""
    layers: list[str] = []
    if (base_dir / "_shared" / f"{name}.json").is_file():
        layers.append("shared")
    if industry and (base_dir / "_industry" / industry
                     / f"{name}.json").is_file():
        layers.append("industry")
    path = snap_dir / f"{name}.json"
    entry: dict = {
        "name": name,
        "group": group,
        "writable": name in _WRITABLE_FILES,
        "has_schema": _has_schema(name),
        "source_layer": layers + (["case"] if path.is_file() else []),
        "item_count": None,
        "updated_at": None,
        "status": "ok",
    }
    if not path.is_file():
        entry["status"] = "missing"
        return entry
    try:
        entry["item_count"] = _item_count(
            json.loads(path.read_text(encoding="utf-8")))
        entry["updated_at"] = datetime.fromtimestamp(
            path.stat().st_mtime).isoformat(timespec="seconds")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        entry["status"] = "parse_error"
    return entry


@router.get("/cases/{case_id}/ontology/overview")
def ontology_overview(case_id: str,
                      p: Principal = Depends(get_principal),
                      ctx: WebContext = Depends(get_ctx)):
    """本体文件总览（19 项；权限=案件可访问，S1 PRD §11 不新增门禁）。

    - 快照目录缺失 → files 空列表（E1-2 空态，前端给导入引导）；
    - 单文件缺失/解析失败 → 该项 status 降级，其余正常（E1-3/E1-4）；
    - ontology_version：case_pack_snapshots 存的声明指纹，缺行时现算兜底。
    """
    case = _get_owned_case(case_id, p, ctx.cases)
    pack_id = case.pack_id or "default"
    snap_dir = ctx.cases.snapshot_dir(case_id, pack_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)

    if not snap_dir.is_dir():
        return ok({
            "case_id": case_id, "case_name": case.name, "pack_id": pack_id,
            "industry": None, "ontology_version": "", "files": [],
        })

    industry = _industry_of(snap_dir)
    files = [
        _file_entry(name, group, snap_dir, base_dir, industry)
        for group, names in ONTOLOGY_GROUPS for name in names
    ]

    snap = ctx.repo.get_pack_snapshot(case_id)
    version = snap.version if snap is not None else ""
    if not version:
        try:
            version = ctx.cases.fingerprint(snap_dir)
        except OSError:
            version = ""
    return ok({
        "case_id": case_id,
        "case_name": case.name,
        "pack_id": pack_id,
        "industry": industry,
        "ontology_version": version,
        "files": files,
    })
