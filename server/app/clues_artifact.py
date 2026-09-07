"""
server/app/clues_artifact.py
线索报告产物持久化（决策 D-M3-2）。

Worker BUILD/RESCAN 产出的线索报告落
cases/{cid}/artifacts/clues_v{N}.json——随分析版本不可变；处置状态真值
在 state.sqlite（D1），线索读面三源拼接：artifact（属性/溯源/合并/抑制）
+ obj_clue 语义字段（Gateway 策略遮蔽）+ state 状态覆盖。

原子写：临时文件 + rename，失败不留残品。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.registry import LineageClue

ARTIFACT_DIR = "artifacts"
ARTIFACT_PREFIX = "clues_v"


def artifact_path(case_dir: str | Path, version: int) -> Path:
    return Path(case_dir) / ARTIFACT_DIR / f"{ARTIFACT_PREFIX}{version}.json"


def save_case_clues(case_dir: str | Path, version: int,
                    clues: list) -> Path:
    """Worker 产线落线索报告（LineageClue 列表或 dict 列表均可）；原子写。"""
    path = artifact_path(case_dir, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    items = [c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in clues]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": version, "clues": items},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    tmp.replace(path)
    return path


def load_case_clues(case_dir: str | Path, version: int) -> list[LineageClue]:
    """从版本产物恢复线索对象（处置任务/读面用）。

    无产物抛 FileNotFoundError（调用方转 CLUES_NOT_READY 任务失败码）。
    只取 LineageClue 已知字段，产物多余键忽略（前向兼容）。
    """
    path = artifact_path(case_dir, version)
    if not path.exists():
        raise FileNotFoundError(
            f"案件线索产物不存在：{path}（先运行 BUILD/RESCAN 产出线索）")
    data = json.loads(path.read_text(encoding="utf-8"))
    known = set(LineageClue.__dataclass_fields__)
    clues: list[LineageClue] = []
    for c in data.get("clues", []):
        clues.append(LineageClue(
            **{k: v for k, v in c.items() if k in known}))
    return clues


def latest_artifact_version(case_dir: str | Path) -> int | None:
    """读面兜底：取 artifacts/ 下最大 vN 产物（无则 None）。"""
    art = Path(case_dir) / ARTIFACT_DIR
    if not art.exists():
        return None
    versions: list[int] = []
    for f in art.glob(f"{ARTIFACT_PREFIX}*.json"):
        try:
            versions.append(int(f.stem[len(ARTIFACT_PREFIX):]))
        except ValueError:
            continue
    return max(versions) if versions else None
