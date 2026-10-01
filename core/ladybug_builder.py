"""
core/ladybug_builder.py
通用语义层 → LadybugDB 属性图构建器（声明驱动；Ladybug 主轨的建图唯一入口）。

输入：已编译的语义层（obj_*/lnk_*）+ OntologyPack 声明。
产物：
  out_path                     LadybugDB 单文件库
  out_path + .manifest.json    行数清单（GraphGateway 判陈旧用）

图模型：
  Entity(pk, type, name)       统一节点表——代理键前缀天然全局唯一，
                               跨类型 MATCH 无需多标签；
  <LINK 大写>(FROM Entity TO Entity, edge_pk STRING, 边属性…)
                               每条 lnk_* 一张 REL 表；edge_pk 取
                               endpoints.edge_key 行键，与证据引用
                               lnk_<name>#<edge_pk> 一一对应，缺省端点对合成。

纪律：
  - 先 COPY 节点、后 COPY 边（边引用不存在的节点 COPY 硬失败的既有坑）；
  - 每次构建写暂存文件，成功后原子替换，失败不污染旧图；
  - ladybug 不可用 / 任何 COPY 失败 → 返回 {skipped: True, reason}，不抛
    （无图环境全链路回落语义轨，构建只降级不失败）；
  - runtime link（decision_for/image_for_*）读到表才建 REL，读不到跳过；
    绝不因建图触发任何写动作。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.semantic_graph_export import (
    edge_decls, edge_sql, graph_object_types, node_sql,
)

MANIFEST_SUFFIX = ".manifest.json"


def ladybug_available() -> bool:
    try:
        import ladybug  # noqa: F401
        return True
    except ImportError:
        return False


def _table_exists(store, table: str) -> bool:
    rows = store.query(
        "SELECT COUNT(*) AS n FROM information_schema.tables "
        "WHERE table_name = ?", (table,))
    return bool(rows) and int(rows[0]["n"]) > 0


def _table_count(store, table: str) -> int | None:
    """表行数；表不存在返回 None（optional 源缺表 / runtime 未建表）。"""
    if not _table_exists(store, table):
        return None
    return int(store.query(f'SELECT COUNT(*) AS n FROM "{table}"')[0]["n"])


def current_table_counts(store, pack) -> dict[str, int]:
    """当前版本库 obj_*/lnk_* 行数清单（缺表不给键）——Gateway 陈旧对账用。"""
    counts: dict[str, int] = {}
    for o in pack.objects:
        n = _table_count(store, f"obj_{o.name}")
        if n is not None:
            counts[f"obj_{o.name}"] = n
    for l in pack.links:
        n = _table_count(store, f"lnk_{l.name}")
        if n is not None:
            counts[f"lnk_{l.name}"] = n
    return counts


def _copy_csv(store, sql: str, path: Path) -> int:
    """DuckDB COPY 导 CSV（正斜杠路径），返回导出行数。"""
    store.execute(
        f"COPY ({sql}) TO '{path.as_posix()}' (HEADER, DELIMITER ',')")
    return int(store.query(f"SELECT COUNT(*) AS n FROM ({sql})")[0]["n"])


def build_case_graph(store, pack, out_path) -> dict:
    """重建案件版本级属性图。

    返回：
      成功 {skipped: False, graph_path, manifest_path, nodes, edges,
            node_types, links}；
      跳过/失败 {skipped: True, reason}（不抛）。
    """
    out_path = Path(out_path)
    if not ladybug_available():
        return {"skipped": True,
                "reason": "ladybug 未安装（pip install ladybug，仅 WSL/Linux 可用）"}

    decls = edge_decls(pack)
    # 边：只建语义层中实际存在的表（编译失败跳过 / runtime 无表 → 不建 REL）
    active: list[tuple] = []
    edge_counts: dict[str, int] = {}
    for d in decls:
        n = _table_count(store, d.table)
        if n is None:
            continue
        active.append((d, n))
        edge_counts[d.link] = n
    # 节点：边端点引用到的对象类型，且 obj_ 表存在（optional 对象无源表时跳过）
    node_types = [t for t in graph_object_types(pack)
                  if _table_exists(store, f"obj_{t}")]
    node_counts = {t: _table_count(store, f"obj_{t}") for t in node_types}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="lbug_build_", dir=str(out_path.parent)))
    staging = tmp_dir / "graph.lbug"
    try:
        # ---- 1) DuckDB → CSV（节点先行）----
        node_csv = tmp_dir / "nodes.csv"
        union_sql = " UNION ALL ".join(node_sql(t, pack) for t in node_types)
        n_nodes = _copy_csv(store, union_sql, node_csv) if node_types else 0
        if not node_types:
            # 无节点仍导出空表头 CSV，供 COPY 建空节点表
            store.execute(
                f"COPY (SELECT NULL::VARCHAR AS pk, NULL::VARCHAR AS type, "
                f"NULL::VARCHAR AS name WHERE FALSE) TO '{node_csv.as_posix()}' "
                f"(HEADER, DELIMITER ',')")

        edge_csvs: dict[str, Path] = {}
        for d, _n in active:
            p = tmp_dir / f"edge_{d.link}.csv"
            _copy_csv(store, edge_sql(d), p)
            edge_csvs[d.link] = p

        # ---- 2) 暂存库 DDL + COPY（先节点后边）----
        import ladybug as lb
        db = lb.Database(str(staging))
        conn = lb.Connection(db)
        conn.execute(
            "CREATE NODE TABLE Entity(pk STRING, type STRING, name STRING, "
            "PRIMARY KEY(pk))")
        for d, _n in active:
            # Kùzu 标识符不接受双引号界定（双引号是字符串字面量）；
            # 统一用反引号转义（EscapedSymbolicName），兼容 date 等保留字。
            cols = ["edge_pk STRING"]
            cols += [f'`{c}` {d.attr_types.get(c, "STRING")}'
                     for c in d.attr_cols]
            conn.execute(
                f"CREATE REL TABLE {d.rel_table}"
                f"(FROM Entity TO Entity, {', '.join(cols)})")
        conn.execute(f"COPY Entity FROM '{node_csv.as_posix()}' (HEADER=true)")
        for d, _n in active:
            conn.execute(
                f"COPY {d.rel_table} FROM '{edge_csvs[d.link].as_posix()}' "
                f"(HEADER=true)")
        try:
            conn.close()
        except Exception:
            pass

        # ---- 3) 原子替换旧图 + 写 manifest ----
        if out_path.exists():
            out_path.unlink()
        staging.replace(out_path)

        table_counts = {f"obj_{t}": n for t, n in node_counts.items()}
        table_counts.update({f"lnk_{d.link}": n for d, n in active})
        manifest = {
            "schema": 1,
            "graph_file": out_path.name,
            "pack": getattr(pack, "name", "default"),
            "built_at": datetime.now(timezone.utc).isoformat(),
            "nodes": node_counts,
            "edges": edge_counts,
            "table_counts": table_counts,
            "rel_tables": [d.rel_table for d, _n in active],
        }
        manifest_path = out_path.with_name(out_path.name + MANIFEST_SUFFIX)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "skipped": False,
            "graph_path": str(out_path),
            "manifest_path": str(manifest_path),
            "nodes": n_nodes,
            "edges": sum(edge_counts.values()),
            "node_types": node_types,
            "node_counts": node_counts,
            "links": edge_counts,
        }
    except Exception as e:
        # 失败不污染旧图：暂存整体丢弃，调用方按语义轨继续
        return {"skipped": True, "reason": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def load_manifest(graph_path) -> dict | None:
    """读图旁 manifest；不存在/损坏返回 None。"""
    p = Path(str(graph_path) + MANIFEST_SUFFIX)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def is_graph_current(store, pack, graph_path) -> tuple[bool, str]:
    """manifest 行数清单与当前版本库对账。

    返回 (是否一致, 原因)。图/manifest 缺失或任一 obj_/lnk_ 行数变化即判陈旧。
    """
    graph_path = Path(graph_path)
    if not graph_path.exists():
        return False, "graph_missing"
    manifest = load_manifest(graph_path)
    if manifest is None:
        return False, "manifest_missing"
    current = current_table_counts(store, pack)
    expected = manifest.get("table_counts") or {}
    if current != expected:
        diff = {k: {"manifest": expected.get(k), "current": current.get(k)}
                for k in sorted(set(current) | set(expected))
                if current.get(k) != expected.get(k)}
        return False, f"table_counts_mismatch: {json.dumps(diff, ensure_ascii=False)}"
    return True, ""
