"""
server/app/worker/ingest.py
W-010/012：TASK_IMPORT 数据导入处理器 + BUILD 冷层挂载。

流程（Worker 侧，上传与幂等判定在路由阶段已完成）：
  1. 取暂存上传件（case_sources 注册表 staged 行）；
  2. 解析五格式 → 套列映射 → 写冷层临时 parquet → 原子 rename 进
     cases/<cid>/cold/<目标表>.parquet（失败零残留，AC-5）；
  3. 清洗声明（clean，如 strip/exclude_org_tokens）合并进案件快照
     bindings.json 对应 object binding（临时副本过 loader 校验后落盘）；
  4. 注册表置 imported；链式入队 BUILD。

BUILD 挂载（mount_case_cold）：handle_build 在编译前把 imported 源
CTAS 为同名表（read_parquet 绝对路径），bindings.source.table 即可命中。
表名经白名单正则约束（注册时已校验为声明源表名），防注入。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from core.ontology_loader import load_pack

from server.app import ingest_io
from server.app.worker.tasks import TaskExecError, TASK_BUILD, enqueue_task

# 目标表名安全集（中文/字母/下划线/数字）；同时须在快照 bindings 声明内
_TABLE_NAME_RE = re.compile(r"^[\w一-鿿]+$", re.UNICODE)


def declared_source_tables(*, pack: str, base_dir: Path) -> dict[str, list[str]]:
    """快照 bindings 声明的源表 → 该表声明的原始列名（向导目标集）。"""
    spec = load_pack(pack, base_dir=base_dir)
    out: dict[str, list[str]] = {}
    for _otype, b in spec.object_bindings.items():
        tbl = b.source_table
        if not tbl:
            continue
        cols = [raw for _alias, raw, _t in getattr(b, "projections", ()) or ()]
        out.setdefault(tbl, [])
        for c in cols:
            if c not in out[tbl]:
                out[tbl].append(c)
    return out


def validate_mapping(target_table: str, column_map: dict,
                     declared: dict[str, list[str]]) -> None:
    """映射校验：目标表必须声明过；映射到的原始列必须在该表声明列内。"""
    if not _TABLE_NAME_RE.match(target_table or ""):
        raise TaskExecError("MAPPING_REJECTED",
                            f"目标表名非法：{target_table!r}")
    if target_table not in declared:
        raise TaskExecError("MAPPING_REJECTED",
                            f"目标源表 {target_table!r} 未在案件快照 bindings 声明，"
                            f"可用 {sorted(declared)}")
    allowed = set(declared[target_table])
    bad = [v for v in (column_map or {}).values() if v not in allowed]
    if bad:
        raise TaskExecError("MAPPING_REJECTED",
                            f"列映射目标 {sorted(set(bad))} 不在表 {target_table} "
                            f"声明列 {sorted(allowed)} 内")


def mount_case_cold(conn, repo, factory, case_id: str) -> list[str]:
    """BUILD 前置：把 imported 冷层 parquet CTAS 为同名源表。返回挂载表名。"""
    mounted: list[str] = []
    for src in repo.imported_sources(case_id):
        table = src["table_name"]
        path = Path(src["parquet_path"])
        if not _TABLE_NAME_RE.match(table) or not path.exists():
            continue
        posix = path.as_posix().replace("'", "''")
        conn.execute(
            f'CREATE OR REPLACE TABLE "{table}" AS '
            f"SELECT * FROM read_parquet('{posix}')")
        mounted.append(table)
    return mounted


def _apply_clean_to_bindings(snap_dir: Path, pack_id: str,
                             target_table: str, clean: list[str]) -> bool:
    """把清洗声明合并进引用该源表的 object binding（loader 校验后落盘）。"""
    if not clean:
        return False
    bp = snap_dir / "bindings.json"
    data = json.loads(bp.read_text(encoding="utf-8"))
    changed = False
    for b in data.get("object_bindings", []):
        src = b.get("source") or {}
        if src.get("table") != target_table and b.get("source_table") != target_table:
            continue
        existing = list(b.get("clean") or [])
        for c in clean:
            if c not in existing:
                existing.append(c)
                changed = True
        b["clean"] = existing
    if not changed:
        return False
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        _atomic_write(tmp_root / pack_id / "bindings.json", data)
        load_pack(pack_id, base_dir=tmp_root)  # 校验失败抛 ValueError
    _atomic_write(bp, data)
    return True


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def handle_import(task, *, repo, factory, snapshot_base_for, **_: Any) -> dict:
    """TASK_IMPORT：暂存件 → 冷层 parquet → bindings 清洗 → 链式 BUILD。"""
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    params = task.params or {}
    upload_id = params.get("upload_id", "")
    target_table = (params.get("target_table") or "").strip()
    column_map = params.get("column_map") or {}
    clean = params.get("clean") or []
    sqlite_table = params.get("sqlite_table")

    src = repo.get_source(task.case_id, upload_id)
    if src is None:
        raise TaskExecError("UPLOAD_NOT_FOUND",
                           f"上传件不存在：{upload_id}")

    # W-P-003：请求未显式带目标表/映射时，回落向导草稿（PUT /sources/{uid}
    # 保存的 mapping_json）；显式参数优先（不改既有导入契约）。
    if not target_table or not column_map or not sqlite_table:
        try:
            saved = json.loads(src.get("mapping_json") or "{}")
        except (ValueError, TypeError):
            saved = {}
        if not target_table:
            target_table = (saved.get("target_table") or "").strip()
        if not column_map and isinstance(saved.get("column_map"), dict):
            column_map = saved["column_map"]
        if not clean and isinstance(saved.get("clean"), list):
            clean = saved["clean"]
        if not sqlite_table and isinstance(saved.get("sqlite_table"), str):
            sqlite_table = saved["sqlite_table"]

    stage_dir = factory.case_dir(task.case_id) / "uploads"
    staged = stage_dir / f"{upload_id}.{src['fmt']}"
    if not staged.exists():
        raise TaskExecError("UPLOAD_NOT_FOUND",
                           f"暂存文件已丢失：{staged.name}")

    base_dir = snapshot_base_for(task.case_id)
    snap_dir = base_dir / case.pack_id
    declared = declared_source_tables(pack=case.pack_id, base_dir=base_dir)
    validate_mapping(target_table, column_map, declared)

    repo.update_progress(task.id, pct=20.0, stage="parse",
                         stage_label="解析数据", detail=src["filename"])
    try:
        df = ingest_io.read_table(staged, src["fmt"], table=sqlite_table)
    except Exception as e:
        raise TaskExecError("PARSE_FAILED", f"解析失败：{e}")

    # 塌缩硬闸：1 行 1 列且单元格疑似结构化串（[ / { 开头）→ 几乎必然是嵌套
    # JSON 未展平（worker 未随代码更新/双重编码串）或分隔符不匹配。直接快速
    # 失败并给可操作诊断，不把垃圾 parquet 落冷层、不入队 BUILD——否则用户
    # 只能在 BUILD 任务里看到晦涩的 BinderException（缺列 Candidate bindings）。
    warn = ingest_io.collapse_warning(df)
    if warn:
        raise TaskExecError("SUSPECT_COLLAPSE", warn)

    repo.update_progress(task.id, pct=60.0, stage="cold",
                         stage_label="写冷层 parquet", detail=target_table)
    cold_dir = factory.case_dir(task.case_id) / "cold"
    try:
        parquet_path, n_rows = ingest_io.write_cold_parquet(
            df, column_map=column_map, cold_dir=cold_dir,
            table_name=target_table)
    except Exception as e:
        raise TaskExecError("COLD_WRITE_FAILED", f"冷层写入失败：{e}")

    # 清洗声明落案件快照 bindings（过 loader 强校验）
    try:
        _apply_clean_to_bindings(snap_dir, case.pack_id, target_table, clean)
    except Exception as e:
        raise TaskExecError("BINDING_REJECTED",
                            f"清洗声明校验失败，已回滚冷层：{e}")

    repo.mark_source_imported(
        task.case_id, upload_id, table_name=target_table,
        parquet_path=str(parquet_path),
        mapping={"column_map": column_map, "clean": clean})
    repo.record_ops("data_import", task.case_id,
                    {"upload_id": upload_id, "table": target_table,
                     "rows": n_rows, "by": task.created_by})

    # B5：导入后补录审计链生命周期事件（失败只 ops 留痕，不回滚导入）
    try:
        from server.app.worker.lifecycle_audit import on_source_imported
        on_source_imported(
            case_dir=factory.case_dir(task.case_id),
            case_id=task.case_id, operator=task.created_by,
            version=repo.current_version(task.case_id),
            upload_id=upload_id, table=target_table, rows=n_rows)
    except Exception as e:  # noqa: BLE001
        repo.record_ops("lifecycle_audit_failed", task.case_id,
                        {"event": "source_imported",
                         "error": f"{type(e).__name__}: {e}"})

    # 链式入队 BUILD（新源表随下次编译生效）
    build = enqueue_task(repo, case_id=task.case_id, task_type=TASK_BUILD,
                         params={"triggered_by": "import", "upload_id": upload_id},
                         idem_key=f"build:import:{upload_id}",
                         created_by=task.created_by)
    repo.update_progress(task.id, pct=100.0, stage="done",
                         stage_label="导入完成",
                         detail=f"{target_table} {n_rows} 行 → BUILD 已入队")
    return {"table": target_table, "rows": n_rows,
            "parquet": str(parquet_path), "build_task": build.id}
