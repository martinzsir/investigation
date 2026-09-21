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
from datetime import datetime
from pathlib import Path

from core.observation import Observation
from core.registry import LineageClue

ARTIFACT_DIR = "artifacts"
ARTIFACT_PREFIX = "clues_v"
# 观察档案（镜头产出）——与线索分离：不进处置清单，随版本可复现
OBSERVATION_PREFIX = "observations_v"
LENS_RUN_DIRNAME = "lens_runs"
# 庙算假设（自动派生 + 人工补充）——随版本可复现，与线索同目录不同前缀
HYPOTHESES_PREFIX = "hypotheses_v"


def artifact_path(case_dir: str | Path, version: int) -> Path:
    return Path(case_dir) / ARTIFACT_DIR / f"{ARTIFACT_PREFIX}{version}.json"


# ----------------------------------------------------------------------
# 观察档案产物（镜头产出；D-M3-2 同口径：随版本不可变）
# ----------------------------------------------------------------------
# 镜头产出不是线索——它是**观察**：摆出数据的某种结构，不替正兵下
# "这是异常"的判断（镜头无常态基线，规则才有）。故单独落盘，不混进
# clues_v{N}.json，也就不进处置清单/看板计数。
#
# 生灭：档案本体随版本（重扫重算，可复现）；正兵对观察的**操作**
# （认领/归档/提升为线索）落 state.sqlite 跨版本持久，不因重扫丢失。

def observation_path(case_dir: str | Path, version: int) -> Path:
    return Path(case_dir) / ARTIFACT_DIR / f"{OBSERVATION_PREFIX}{version}.json"


def save_case_observations(case_dir: str | Path, version: int,
                           observations: list) -> Path:
    """镜头观察档案落盘（原子写）。observations 为 Observation 或 dict 列表。"""
    path = observation_path(case_dir, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    items = [o.to_dict() if hasattr(o, "to_dict") else dict(o)
             for o in observations]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": version, "observations": items},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    tmp.replace(path)
    return path


def hypotheses_path(case_dir: str | Path, version: int) -> Path:
    return Path(case_dir) / ARTIFACT_DIR / f"{HYPOTHESES_PREFIX}{version}.json"


def save_case_hypotheses(case_dir: str | Path, version: int,
                         payload: dict) -> Path:
    """庙算假设产物落盘（原子写）。payload 由调用方组装。

    随版本可复现（自动假设每次 BUILD 重算）；人工假设本体在
    cases/<cid>/hypotheses.json 跨版本持久，此处只做**合并快照**。
    """
    path = hypotheses_path(case_dir, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": version, **payload},
                              ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    tmp.replace(path)
    return path


def load_case_hypotheses(case_dir: str | Path,
                         version: int) -> dict:
    """读某版本假设产物；无产物 → {}（未 BUILD 时假设页降级为空结构）。"""
    path = hypotheses_path(case_dir, version)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_case_observations(case_dir: str | Path,
                           version: int) -> list[Observation]:
    """读某版本的观察档案；无产物 → []（观察为空是合法状态，不抛错）。"""
    path = observation_path(case_dir, version)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [Observation.from_dict(o) for o in data.get("observations", [])]


def latest_observation_version(case_dir: str | Path) -> int | None:
    """取 artifacts/ 下最大 observations_vN 版本（无则 None）。"""
    art = Path(case_dir) / ARTIFACT_DIR
    if not art.exists():
        return None
    versions: list[int] = []
    for f in art.glob(f"{OBSERVATION_PREFIX}*.json"):
        try:
            versions.append(int(f.stem[len(OBSERVATION_PREFIX):]))
        except ValueError:
            continue
    return max(versions) if versions else None


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


# ----------------------------------------------------------------------
# 定向镜头运行补充产物（TASK_LENS_RUN；画布定向带参调度批次）
# ----------------------------------------------------------------------
# 主产物 clues_v{N}.json 随版本不可变（D-M3-2）：定向镜头对当前版本带参
# 运行不产新版本文件，其线索落独立补充产物 artifacts/lens_runs/v{N}/
# {run_id}.json，挂产生它的版本——版本前进（RESCAN）后随旧版本自然失效，
# 语义与"线索报告随版本不可变"一致。线索读面（clues_view._load_raw）
# 统一并线，列表/详情/抑制自动可见。

def lens_run_dir(case_dir: str | Path, version: int) -> Path:
    """定向镜头运行产物目录：artifacts/lens_runs/v{version}/。"""
    return Path(case_dir) / ARTIFACT_DIR / LENS_RUN_DIRNAME / f"v{version}"


def load_origin_lens_runs(case_dir: str | Path, version: int,
                          clue_id: str) -> list[dict]:
    """读某版本下**由指定线索发起**的定向镜头运行（origin.clue_id 匹配）。

    用于画布原地并入深挖结果：正兵在 A 线索画布跑的镜头，其结果回到 A 的
    画布上可见，不必跳去线索列表找。无 origin 的运行（如从启停面板/线索
    列表发起）不匹配——它们本来就没有"发起画布"。
    """
    if not clue_id:
        return []
    return [r for r in load_lens_runs(case_dir, version)
            if str((r.get("origin") or {}).get("clue_id") or "") == str(clue_id)]


def save_lens_run(case_dir: str | Path, version: int, *, run_id: str,
                  skill_id: str, params: dict, operator: str,
                  clues: list, origin: dict | None = None) -> Path:
    """定向镜头运行线索落盘（原子写）。clues 为 LineageClue 或 dict 列表。

    origin（发起来源，可选）：{clue_id, node_id, subject, surface}。
    解决的问题：镜头产出的线索此前与发起它的线索**零关联**——正兵在 A 线索
    画布上深挖张卫国，结果变成一条独立线索 B，既打断了研判（要跳去列表找），
    也看不出 B 与 A 的关系。记录 origin 后：
      - A 的画布可原地并入 B 的结果（不打断）；
      - B 仍是可独立处置的线索（保留处置语义）。
    """
    d = lens_run_dir(case_dir, version)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{run_id}.json"
    payload = {
        "schema_version": 1, "run_id": run_id, "version": version,
        "skill_id": skill_id, "params": params, "operator": operator,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "origin": dict(origin) if isinstance(origin, dict) else None,
        "clues": [c.to_dict() if hasattr(c, "to_dict") else dict(c)
                  for c in clues],
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    tmp.replace(path)
    return path


def load_lens_runs(case_dir: str | Path, version: int) -> list[dict]:
    """读某版本全部定向镜头运行产物（run_id 排序；损坏文件跳过不拖垮读面）。"""
    d = lens_run_dir(case_dir, version)
    if not d.is_dir():
        return []
    runs: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            runs.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return runs
