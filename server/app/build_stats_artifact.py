"""
server/app/build_stats_artifact.py
BUILD 诊断原料持久化（手动运行诊断的前置产物）。

build_ontology 的 stats 里 dirty/degraded/quarantine/clean_stats/dedup_conflicts
五项是 run_diagnostic 的落账原料，但只存在于构建期内存。BUILD 成功后把这五项
快照到 cases/{cid}/artifacts/build_stats_v{N}.json（随版本不可变，原子写），
供用户**手动发起** DIAGNOSE 任务时经 core.run_health.record_build_* 补落
run_diagnostic——BUILD 本身不写 run_diagnostic（不自动留痕）。

旧版本（升级前 BUILD）无此文件，DIAGNOSE 时 load 返回 None，跳过 BUILD 五类留痕。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARTIFACT_DIR = "artifacts"
ARTIFACT_PREFIX = "build_stats_v"

# 仅持久化诊断相关五项；row_snapshot/objects/links 等大对象不入库
BUILD_STATS_KEYS = (
    "dirty", "degraded", "quarantine", "clean_stats", "dedup_conflicts",
)


def build_stats_path(case_dir: str | Path, version: int) -> Path:
    return Path(case_dir) / ARTIFACT_DIR / f"{ARTIFACT_PREFIX}{version}.json"


def save_build_stats(case_dir: str | Path, version: int,
                     stats: dict[str, Any] | None) -> Path | None:
    """BUILD 成功后落诊断原料快照；无任何条目时不产生文件（返回 None）。"""
    if not stats:
        return None
    payload = {k: stats.get(k) or [] for k in BUILD_STATS_KEYS}
    if not any(payload.values()):
        return None
    path = build_stats_path(case_dir, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": version, **payload},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    tmp.replace(path)
    return path


def load_build_stats(case_dir: str | Path, version: int) -> dict[str, Any] | None:
    """读取诊断原料；文件不存在（旧版本/干净 BUILD）返回 None。"""
    path = build_stats_path(case_dir, version)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: data.get(k) or [] for k in BUILD_STATS_KEYS}
