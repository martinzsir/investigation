"""
server/app/quality_view.py
W-P-009/010 隔离区 + 清洗留痕读面（纯读聚合，不新增存储）。

数据源：
  - build_quarantine 表（BUILD 期 CAST 失败/空值策略整行隔离，全行留存，
    样本已脱敏）→ reason=cast_error；
  - run_diagnostic：clean_drop_rate（null_policy:reject 剔除）→ null_value；
    dedup_key_conflict（业务键去重收敛）→ dedup；
    source_value_cast_failed/source_column_missing → other 留痕。

红线：sample_masked 只出脱敏样本，原始值不回传；零隔离必须给
empty_message（红线五，不留空）。
"""
from __future__ import annotations

from typing import Any

from core.run_health import RunHealth

EMPTY_MESSAGE = "本次装载无数据被丢弃"

REASON_CAST = "cast_error"
REASON_NULL = "null_value"
REASON_DEDUP = "dedup"
REASON_OTHER = "other"
_REASONS = (REASON_CAST, REASON_NULL, REASON_DEDUP, REASON_OTHER)


def _diag_rows(conn) -> list[dict]:
    try:
        return RunHealth.readonly(conn).rows()
    except Exception:
        return []


def _table_exists(conn, table: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table]).fetchall()
    return bool(rows)


def _src_label(source: str | None) -> str:
    s = str(source or "")
    if s.startswith("rescan") or "rescan" in s:
        return "rescan"
    return "build"


def collect_quarantine(conn) -> list[dict]:
    """聚合两类来源为统一隔离条目（未过滤/未分页）。"""
    items: list[dict] = []

    # 1) build_quarantine：CAST 失败/空值整行隔离（行级留存）
    if _table_exists(conn, "build_quarantine"):
        try:
            rows = conn.execute(
                "SELECT object, property, src_column, reason, sample_masked, "
                "name_value, source_table, quarantined_at "
                "FROM build_quarantine ORDER BY quarantined_at DESC"
            ).fetchall()
            for r in rows:
                items.append({
                    "object": r[0], "property": r[1], "rule": r[3],
                    "src_column": r[2], "reason": REASON_CAST,
                    "source_table": r[6], "sample_masked": [r[4]] if r[4] else [],
                    "name_value": r[5],
                    "quarantined_at": str(r[7]) if r[7] is not None else "",
                })
        except Exception:
            pass

    # 2) run_diagnostic：空值剔除 / 去重冲突（聚合留痕，样本前 3）
    for r in _diag_rows(conn):
        kind = r.get("kind")
        d = r.get("detail") or {}
        created = r.get("created_at") or ""
        if kind == "clean_drop_rate":
            items.append({
                "object": d.get("object"), "property": d.get("prop"),
                "rule": "、".join(map(str, d.get("rules") or [])),
                "src_column": None, "reason": REASON_NULL,
                "source_table": None,
                "sample_masked": d.get("sample_masked") or [],
                "name_value": None, "quarantined_at": created,
            })
        elif kind == "dedup_key_conflict":
            items.append({
                "object": d.get("object"),
                "property": "、".join(map(str, d.get("key_columns") or [])),
                "rule": f"dedup:{d.get('policy') or 'converged'}",
                "src_column": None, "reason": REASON_DEDUP,
                "source_table": None,
                "sample_masked": [],
                "name_value": None, "quarantined_at": created,
            })
    return items


def list_quarantine(conn, *, reason: str | None = None,
                    page: int = 1, page_size: int = 50) -> dict[str, Any]:
    items = collect_quarantine(conn)
    stats = {k: 0 for k in _REASONS}
    for it in items:
        stats[it["reason"]] = stats.get(it["reason"], 0) + 1
    if reason:
        items = [it for it in items if it["reason"] == reason]
    total = len(items)
    start = (max(1, page) - 1) * page_size
    out: dict[str, Any] = {
        "items": items[start:start + page_size],
        "total": total, "stats": stats,
        "page": page, "page_size": page_size,
    }
    if total == 0:
        out["empty_message"] = EMPTY_MESSAGE
    return out


def list_clean_trace(conn, *, obj: str | None = None,
                     page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """按 (object, property) 聚合清洗/降级留痕。"""
    agg: dict[tuple, dict] = {}

    def _entry(object_: str | None, prop: str | None) -> dict:
        key = (object_ or "", prop or "")
        e = agg.get(key)
        if e is None:
            e = {"object": key[0], "property": key[1], "rules": [],
                 "rows_before": 0, "rows_after": None, "dropped_rows": 0,
                 "rate": None, "samples_masked": [], "source": "build",
                 "created_at": ""}
            agg[key] = e
        return e

    for r in _diag_rows(conn):
        kind = r.get("kind")
        d = r.get("detail") or {}
        src = _src_label(r.get("source"))
        if kind == "clean_drop_rate":
            e = _entry(d.get("object"), d.get("prop"))
            for rule in (d.get("rules") or []):
                if rule not in e["rules"]:
                    e["rules"].append(rule)
            e["dropped_rows"] += int(d.get("dropped_rows") or 0)
            e["rows_before"] = max(e["rows_before"],
                                   int(d.get("rows_before") or 0))
            if d.get("rate") is not None:
                e["rate"] = d.get("rate")
            e["samples_masked"] = (e["samples_masked"]
                                   + (d.get("sample_masked") or []))[:3]
            e["source"] = src
            e["created_at"] = r.get("created_at") or e["created_at"]
        elif kind == "dedup_key_conflict":
            e = _entry(d.get("object"),
                       "、".join(map(str, d.get("key_columns") or [])))
            rule = f"dedup:{d.get('policy') or 'converged'}"
            if rule not in e["rules"]:
                e["rules"].append(rule)
            e["dropped_rows"] += int(d.get("duplicate_rows") or 0)
            e["source"] = src
            e["created_at"] = r.get("created_at") or e["created_at"]
        elif kind in ("source_value_cast_failed", "source_column_missing"):
            e = _entry(d.get("object"), d.get("prop"))
            if kind not in e["rules"]:
                e["rules"].append(kind)
            e["source"] = src
            e["created_at"] = r.get("created_at") or e["created_at"]

    items = list(agg.values())
    for e in items:
        if e["rows_before"]:
            e["rows_after"] = e["rows_before"] - e["dropped_rows"]
    if obj:
        items = [e for e in items if e["object"] == obj]
    items.sort(key=lambda e: e["created_at"], reverse=True)
    total = len(items)
    start = (max(1, page) - 1) * page_size
    return {"items": items[start:start + page_size], "total": total,
            "page": page, "page_size": page_size}
