"""
server/app/source_analyze.py
W-P-002 上传件列分析（纯只读计算：不产任务、不写映射、不改状态）。

  - 列画像：inferred_type/null_rate/distinct/samples（复用 ingest_io 推断）；
  - declared_tables：快照 bindings 声明的源表 + 必选/可选原始列；
  - suggestion：声明列 ↔ 上传列匹配（exact > normalized > fuzzy > none），
    自动选表按必选列平均置信；missing_required/low_confidence 清单；
  - element_hints：core.de_recommend 数据元推荐（只读 data_elements，
    推荐永不自动生效）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.de_recommend import recommend_for_table
from core.ontology_loader import load_data_elements, load_pack

from server.app import ingest_io

LOW_CONFIDENCE = 0.70
_CONF = {"exact": 1.0, "normalized": 0.85, "fuzzy": 0.6, "none": 0.0}
_DE_CONF = {"high": 0.9, "medium": 0.6}


def _norm(s: Any) -> str:
    """归一：去下划线/空白（含全角）、转小写。"""
    return re.sub(r"[_\s　]+", "", str(s or "")).lower()


def declared_tables_view(spec) -> list[dict]:
    """object_bindings → 向导目标表（title/必选列/可选列）。

    title 归属：只有结构化源绑定（source+投影列）才代表"这张表是什么"；
    source_sql 借道绑定（如 person 用 UNION 从通话记录/轨迹/流水等多表
    取人名，source_table 仅作挂载提示）不占有源表命名权——否则"通话记录"
    表会被先遍历到的 person 显示成"自然人（通话记录）"。
    """
    obj_title = {o.name: o.title for o in spec.objects}
    tables: dict[str, dict] = {}
    for _otype, b in spec.object_bindings.items():
        tbl = b.source_table
        if not tbl:
            continue
        optional = set(getattr(b, "optional_raw", ()) or ())
        projs = tuple(getattr(b, "projections", ()) or ())
        entry = tables.get(tbl)
        if entry is None:
            entry = tables.setdefault(tbl, {
                "name": tbl,
                "title": obj_title.get(b.object, tbl) if projs else tbl,
                "object": b.object,
                "required_columns": [], "optional_columns": []})
        elif projs and entry["title"] == tbl:
            # 先前被 source_sql 借道绑定用表名占位，结构化源补正式标题
            entry["title"] = obj_title.get(b.object, tbl)
        for _alias, raw, _t in projs:
            if raw in entry["required_columns"] or raw in entry["optional_columns"]:
                continue
            bucket = "optional_columns" if raw in optional else "required_columns"
            entry[bucket].append(raw)
    return list(tables.values())


def _best_match(target: str, source_cols: list[str]) -> dict:
    """单声明列 → 最佳上传列（精确 > 归一 > 包含模糊）。"""
    tn = _norm(target)
    best: dict | None = None
    for sc in source_cols:
        if str(sc) == str(target):
            mtype, conf = "exact", _CONF["exact"]
        elif _norm(sc) == tn:
            mtype, conf = "normalized", _CONF["normalized"]
        else:
            sn = _norm(sc)
            if tn and sn and (tn in sn or sn in tn):
                mtype, conf = "fuzzy", _CONF["fuzzy"]
            else:
                continue
        if best is None or conf > best["confidence"]:
            best = {"source_col": str(sc), "target_prop": target,
                    "match_type": mtype, "confidence": conf}
    if best is None:
        return {"source_col": None, "target_prop": target,
                "match_type": "none", "confidence": _CONF["none"]}
    return best


def _suggest_for(table: str, decl: dict, source_cols: list[str]) -> dict:
    required = list(decl["required_columns"])
    optional = list(decl["optional_columns"])
    matches = [_best_match(t, source_cols) for t in required + optional]
    by_prop = {m["target_prop"]: m for m in matches}
    missing_required = [t for t in required
                        if by_prop[t]["match_type"] == "none"]
    low_confidence = [m["target_prop"] for m in matches
                      if 0 < m["confidence"] < LOW_CONFIDENCE]
    denom = len(required) or len(matches)
    conf_vals = [by_prop[t]["confidence"] for t in required] \
        if required else [m["confidence"] for m in matches]
    confidence = round(sum(conf_vals) / denom, 2) if denom else 0.0
    return {"target_table": table, "confidence": confidence,
            "matches": matches, "missing_required": missing_required,
            "low_confidence": low_confidence}


def _auto_pick(tables: dict[str, dict], source_cols: list[str]) -> str | None:
    """无预选表：按全部声明列（必填+可选）平均置信选最高（全 0 → None）。

    optional 列只表达"导入时缺了可降级"，不参与表识别降权——否则单必填列
    的精简绑定会被任意含同名列的数据虚高命中（如含"主体"列的流水被误推荐
    到工商表：必填只剩"主体"一列时平均置信恒为 1.0）。
    """
    best_name, best_conf = None, 0.0
    for name, decl in tables.items():
        cols = decl["required_columns"] + decl["optional_columns"]
        if not cols:
            continue
        conf = sum(_best_match(t, source_cols)["confidence"] for t in cols) / len(cols)
        if conf > best_conf:
            best_name, best_conf = name, conf
    return best_name


def _column_profile(df) -> tuple[list[dict], dict[str, list[str]]]:
    n = len(df)
    out: list[dict] = []
    col_values: dict[str, list[str]] = {}
    for col in df.columns:
        series = df[col].astype(str)
        non_empty = series[series.str.len() > 0]
        vals = non_empty.head(200).tolist()
        col_values[str(col)] = vals
        out.append({
            "name": str(col),
            "inferred_type": ingest_io._infer_kind(series),
            "null_rate": round(1.0 - len(non_empty) / n, 3) if n else 0.0,
            "distinct": int(non_empty.nunique()),
            "samples": non_empty.head(3).tolist(),
        })
    return out, col_values


def analyze_source(*, df, pack: str, base_dir: str | Path,
                   target_table: str | None = None) -> dict[str, Any]:
    """列分析主入口（df 为五格式解析后的 DataFrame）。"""
    spec = load_pack(pack, base_dir=Path(base_dir))
    tables = declared_tables_view(spec)
    tables_map = {t["name"]: t for t in tables}
    source_cols = [str(c) for c in df.columns]

    columns, col_values = _column_profile(df)

    chosen = target_table if target_table in tables_map \
        else _auto_pick(tables_map, source_cols)
    if chosen is None:
        suggestion = {"target_table": None, "confidence": 0.0, "matches": [],
                      "missing_required": [], "low_confidence": []}
    else:
        suggestion = _suggest_for(chosen, tables_map[chosen], source_cols)

    elements = load_data_elements(pack, Path(base_dir))
    element_hints: list[dict] = []
    for r in recommend_for_table(source_cols, col_values, elements):
        recs = r.get("recommendations") or []
        top = next((x for x in recs if x.get("data_element")), None)
        if top is None:
            continue
        eid = top["data_element"]
        de_spec = elements.get(eid) or {}
        cr = de_spec.get("clean_rule")
        clean_rule_list = [cr] if isinstance(cr, str) else (cr or [])
        element_hints.append({
            "col": r["col"],
            "element_id": eid,
            "element_name": top.get("de_name"),
            "confidence": _DE_CONF.get(top.get("confidence"), 0.6),
            "evidence": {"match_values": (col_values.get(r["col"]) or [])[:3]},
            "clean_rule": clean_rule_list,
            "format": de_spec.get("format"),
        })

    return {"columns": columns, "declared_tables": tables,
            "suggestion": suggestion, "element_hints": element_hints}
