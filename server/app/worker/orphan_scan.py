"""
server/app/worker/orphan_scan.py
孤儿版本文件扫描（W-009）。

- 判定：cases/<cid>/ 顶层 vN.duckdb（N 为整数）中——
  白名单 = 版本历史记录为 active/pending_reclaim，或等于当前指针；
  其余（无历史记录的强杀残留 / 已 reclaimed 但未删成的崩溃残留）
  视为孤儿 → 移入 cases/<cid>/.quarantine/（隔离区，非直删，AC-2）；
- 扫描全程只读目录结构，不打开任何 DuckDB 文件（AC-4：不影响正常读写）；
- 隔离区文件超过 ttl_days（默认 7 天，按文件 mtime）删除（AC-3）；
- 扫描/隔离/清理动作记 ops_events（AC-5：结果进健康度）；
- dry_run：只记 ops_events 不移动文件（部署先跑一轮核对）。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from server.app.meta.models import (
    VER_ACTIVE,
    VER_PENDING_RECLAIM,
)

_FILE_RE = re.compile(r"^v(\d+)\.duckdb$")
QUARANTINE_DIR = ".quarantine"

# 隔离/清理动作的 ops_events kind
OPS_ORPHAN_SCAN = "orphan_scan"
OPS_ORPHAN_QUARANTINED = "orphan_quarantined"
OPS_ORPHAN_CLEANED = "orphan_cleaned"


def _is_whitelisted(repo, case_id: str, version: int,
                    current: int) -> bool:
    if version == current:
        return True
    status = repo.version_status(case_id, version)
    return status in (VER_ACTIVE, VER_PENDING_RECLAIM)


def scan_once(repo, factory, *, dry_run: bool = False,
              ttl_days: float = 7.0) -> dict:
    """扫描全部案件目录一轮；返回统计 {scanned_cases, orphans,
    quarantined, cleaned}。orphans 为 [(case_id, 路径, 原因)] 明细。"""
    stats = {"scanned_cases": 0, "orphans": [], "quarantined": 0,
             "cleaned": 0}
    root = factory.cases_root
    if not root.exists():
        return stats

    for case_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        case_id = case_dir.name
        stats["scanned_cases"] += 1
        current = repo.current_version(case_id)
        quarantine = case_dir / QUARANTINE_DIR

        # 1) 孤儿判定与隔离
        moved: list[dict] = []
        for f in sorted(case_dir.glob("v*.duckdb")):
            m = _FILE_RE.match(f.name)
            if m is None:
                continue
            version = int(m.group(1))
            if _is_whitelisted(repo, case_id, version, current):
                continue
            reason = ("version_history_reclaimed" if
                      repo.version_status(case_id, version) is not None
                      else "no_version_history")
            moved.append({"file": f.name, "version": version,
                          "reason": reason})
        if moved and not dry_run:
            quarantine.mkdir(parents=True, exist_ok=True)
        for item in moved:
            src = case_dir / item["file"]
            dst = quarantine / item["file"]
            if dry_run:
                continue
            # 目标同名（上次移动后又被强杀写回）→ 加时间戳后缀
            if dst.exists():
                dst = quarantine / (
                    f"{src.stem}-{time.strftime('%Y%m%d%H%M%S')}.duckdb")
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                continue  # 移动失败留下周期重试（幂等）
            stats["quarantined"] += 1
            repo.record_ops(OPS_ORPHAN_QUARANTINED, case_id,
                            {"file": item["file"], "to": str(dst),
                             "reason": item["reason"]})

        if moved:
            stats["orphans"].extend(
                {"case_id": case_id, **item} for item in moved)
        if moved or dry_run:
            repo.record_ops(OPS_ORPHAN_SCAN, case_id,
                            {"scanned": True, "orphans": len(moved),
                             "dry_run": dry_run})

        # 2) 隔离区 TTL 清理
        if not quarantine.exists() or dry_run:
            continue
        deadline = time.time() - ttl_days * 86400.0
        for f in sorted(quarantine.glob("v*.duckdb")):
            try:
                if f.stat().st_mtime > deadline:
                    continue
                f.unlink()
            except OSError:
                continue
            stats["cleaned"] += 1
            repo.record_ops(OPS_ORPHAN_CLEANED, case_id,
                            {"file": f.name,
                             "ttl_days": ttl_days})
    return stats
