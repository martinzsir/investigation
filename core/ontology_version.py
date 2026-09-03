"""
core/ontology_version.py
语义层版本时钟与依赖图（REQ-001）。

建立"源权威"与"语义权威"的双 watermark，让 stale 状态可被查询。
每次 build_ontology() 写入一条 OntologyVersion 记录；freshness() 比对
当前源端 watermark 与已记录的语义层 watermark，差异 → STALE + affected_objects。

落地：DuckDB 表 meta_ontology_state（同 pack 只有一条 is_current=true）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class OntologyVersion:
    """一次 build_ontology() 的版本快照。"""
    pack: str
    schema_version: int          # 来自 objects.json
    ontology_version: str        # 语义声明 semver（暂取 "2.<obj数>.<link数>"）
    build_id: str                # uuid4 hex
    built_at: str                # ISO8601
    source_watermark: str        # 源端已读取并校验的最大事件时间
    ontology_watermark: str      # 语义层已物化的最大事件时间
    input_hashes: dict[str, str] = field(default_factory=dict)  # {object_name: sha256(物化结果)}
    params_hash: str = ""        # sha256(rules.json + functions.json params 拼接)


@dataclass
class FreshnessResult:
    """freshness() 返回。"""
    state: str                   # "FRESH" | "STALE" | "UNBUILT"
    affected_objects: list[str] = field(default_factory=list)
    last_built_at: str | None = None
    source_watermark: str | None = None
    ontology_watermark: str | None = None


# ----------------------------------------------------------------------
# 表 DDL
# ----------------------------------------------------------------------
_META_DDL = """
CREATE TABLE IF NOT EXISTS meta_ontology_state (
    pack VARCHAR NOT NULL,
    build_id VARCHAR PRIMARY KEY,
    built_at VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    ontology_version VARCHAR NOT NULL,
    source_watermark VARCHAR,
    ontology_watermark VARCHAR,
    input_hashes VARCHAR,
    params_hash VARCHAR,
    is_current BOOLEAN NOT NULL
)
"""


def _ensure_meta_table(conn) -> None:
    conn.execute(_META_DDL)


# ----------------------------------------------------------------------
# 版本计算
# ----------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _compute_source_watermark(conn, spec) -> str:
    """取所有 source_table 的 max(日期列)，无日期表取 NULL（空串）。

    遍历 spec.object_bindings，按 source_table 用 PRAGMA 获取列名，
    匹配名称含 日期/date/time 的列，取 MAX 作为 watermark。
    注意：源表列名可能是中文（如"日期"），与 obj_* 物化后的属性名（如"date"）不同，
    不能直接用属性名查源表。
    """
    candidates: list[str] = []
    for otype in spec.objects:
        if otype.runtime:
            continue
        b = spec.object_bindings.get(otype.name)
        if not b or not b.source_table:
            continue
        # 该对象必须有 date 类型属性（否则不参与 watermark 计算）
        has_date = any(t == "date" for t in otype.properties.values())
        if not has_date:
            continue
        # 用 PRAGMA 获取源表所有列名，匹配日期类列名
        try:
            cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info('{b.source_table}')").fetchall()]
        except Exception:
            continue  # 表不存在 → 跳过
        date_cols = [c for c in cols if any(
            p in c.lower() for p in ("日期", "date", "time"))]
        for col in date_cols:
            try:
                row = conn.execute(
                    f'SELECT MAX(CAST("{col}" AS VARCHAR)) FROM {b.source_table}'
                ).fetchone()
                if row and row[0]:
                    candidates.append(str(row[0]))
            except Exception:
                continue  # 列不存在/类型不兼容 → 跳过
    if not candidates:
        return ""
    return max(candidates)  # 字符串 ISO 日期可直接比较


def _compute_input_hashes(conn, spec) -> dict[str, str]:
    """对每个已物化 obj_* 计算 sha256(按 pk 排序后的行 JSON)。

    性能权衡：10 万行场景下全行 hash 可能慢；当前简化版直接全表，
    若后续遇性能瓶颈可改为 row_count + 首尾 hash。
    """
    hashes: dict[str, str] = {}
    for otype in spec.objects:
        if otype.runtime:
            continue
        tbl = f"obj_{otype.name}"
        try:
            rows = conn.execute(
                f'SELECT * FROM {tbl} ORDER BY "{otype.pk}"'
            ).fetchall()
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {tbl} LIMIT 0").description]
            serialized = json.dumps(
                [{c: r[i] for i, c in enumerate(cols)} for r in rows],
                ensure_ascii=False, default=str, sort_keys=True)
            hashes[otype.name] = _sha256(serialized)
        except Exception:
            continue  # 表不存在（optional 跳过场景）
    return hashes


def _compute_params_hash(spec) -> str:
    """sha256(rules.json 的 params + functions.json 的 parameters 拼接)。"""
    parts: list[str] = []
    for rid, r in sorted(spec.rules.items()):
        parts.append(f"{rid}:{json.dumps(r.params, sort_keys=True, ensure_ascii=False)}")
    for fname, f in sorted(spec.functions.items()):
        parts.append(f"{fname}:{json.dumps(f.parameters, sort_keys=True, ensure_ascii=False)}")
    return _sha256("|".join(parts))


def _ontology_version_str(spec) -> str:
    """语义声明版本号（暂取 schema_version.obj数.link数，便于人工识别）。"""
    return f"2.{len(spec.objects)}.{len(spec.links)}"


def compute_version(conn, pack: str, spec) -> OntologyVersion:
    """从 spec + conn 当前状态计算版本快照（不写表）。"""
    source_wm = _compute_source_watermark(conn, spec)
    input_hashes = _compute_input_hashes(conn, spec)
    params_hash = _compute_params_hash(spec)
    return OntologyVersion(
        pack=pack,
        schema_version=2,
        ontology_version=_ontology_version_str(spec),
        build_id=uuid.uuid4().hex,
        built_at=_now_iso(),
        source_watermark=source_wm,
        ontology_watermark=source_wm,  # build 后两者相等（freshness 检测源端变化）
        input_hashes=input_hashes,
        params_hash=params_hash,
    )


# ----------------------------------------------------------------------
# 版本记录
# ----------------------------------------------------------------------
def record_version(conn, ver: OntologyVersion) -> None:
    """把旧版 is_current=false，写入新版 is_current=true。"""
    _ensure_meta_table(conn)
    conn.execute(
        "UPDATE meta_ontology_state SET is_current=false WHERE pack=?",
        [ver.pack])
    conn.execute(
        """INSERT INTO meta_ontology_state
           (pack, build_id, built_at, schema_version, ontology_version,
            source_watermark, ontology_watermark, input_hashes, params_hash, is_current)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, true)""",
        [ver.pack, ver.build_id, ver.built_at, ver.schema_version,
         ver.ontology_version, ver.source_watermark, ver.ontology_watermark,
         json.dumps(ver.input_hashes, ensure_ascii=False), ver.params_hash])


def current_version(conn, pack: str = "default") -> OntologyVersion | None:
    """读取 is_current=true 的版本，无则 None。"""
    _ensure_meta_table(conn)
    row = conn.execute(
        """SELECT pack, build_id, built_at, schema_version, ontology_version,
                  source_watermark, ontology_watermark, input_hashes, params_hash
           FROM meta_ontology_state WHERE pack=? AND is_current=true""",
        [pack]
    ).fetchone()
    if not row:
        return None
    try:
        input_hashes = json.loads(row[7]) if row[7] else {}
    except json.JSONDecodeError:
        input_hashes = {}
    return OntologyVersion(
        pack=row[0], build_id=row[1], built_at=row[2],
        schema_version=row[3], ontology_version=row[4],
        source_watermark=row[5] or "", ontology_watermark=row[6] or "",
        input_hashes=input_hashes, params_hash=row[8] or "")


# ----------------------------------------------------------------------
# freshness 检测
# ----------------------------------------------------------------------
def freshness(conn, pack: str = "default") -> FreshnessResult:
    """比对当前源端 watermark + input_hashes vs 已记录版本。

    - 无 current_version → UNBUILT
    - 源端 watermark 变化或 input_hashes 不一致 → STALE + affected_objects
    - 一致 → FRESH
    """
    from core.ontology_loader import load_pack

    ver = current_version(conn, pack)
    if ver is None:
        return FreshnessResult(state="UNBUILT")

    spec = load_pack(pack)
    cur_source_wm = _compute_source_watermark(conn, spec)
    cur_input_hashes = _compute_input_hashes(conn, spec)

    affected: list[str] = []
    # 1. watermark 差异 → 所有对象受影响
    if cur_source_wm != ver.source_watermark:
        affected = [o.name for o in spec.objects if not o.runtime]
    # 2. input_hashes 差异 → 仅不一致对象受影响
    for name, h in cur_input_hashes.items():
        if ver.input_hashes.get(name) != h and name not in affected:
            affected.append(name)

    state = "STALE" if affected else "FRESH"
    return FreshnessResult(
        state=state, affected_objects=affected,
        last_built_at=ver.built_at,
        source_watermark=cur_source_wm or None,
        ontology_watermark=ver.ontology_watermark or None)


# ----------------------------------------------------------------------
# 依赖图
# ----------------------------------------------------------------------
def dependency_graph(pack: str = "default") -> dict[str, list[str]]:
    """object -> [source tables]；link -> [from_obj, to_obj]。"""
    from core.ontology_loader import load_pack
    spec = load_pack(pack)
    graph: dict[str, list[str]] = {}
    for otype in spec.objects:
        if otype.runtime:
            continue
        b = spec.object_bindings.get(otype.name)
        graph[f"obj_{otype.name}"] = [b.source_table] if b and b.source_table else []
    for ltype in spec.links:
        if ltype.runtime:
            continue
        graph[f"lnk_{ltype.name}"] = [f"obj_{ltype.from_obj}", f"obj_{ltype.to_obj}"]
    return graph
