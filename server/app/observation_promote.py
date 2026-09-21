"""观察 → 线索：提升动作（人工认领，强制指定假设）。

为什么必须显式提升
------------------
观察是**已摆出的事实**，不是命题：镜头无常态基线（规则才有），只回答
"这个结构存不存在"。没有假设的条目进处置流程只能空转——此前 10 条镜头
线索全"查证中"而假设链全空，正兵无从下手。

提升 = 正兵断言"这批观察构成疑点，且我要验证的是某个假设"。
那一刻它才成为命题，才有假设链、才进处置流程。

为什么必须指定假设
------------------
线索是待证明的命题，`assumption_chain` 决定它在验证什么。不指定假设的
线索仍然无法处置——等于没解决问题。故提升时强制选一个本体已声明的假设。

落盘位置：案件级，**不挂版本**
------------------------------
提升是人工认领的证据链，不是自动重算的产物。挂版本会导致 RESCAN 后
凭空消失（处置记录成孤儿），违反"已认领的东西不因配置变更而蒸发"。
故落 artifacts/promoted_clues/{clue_id}.json，读面跨版本并线。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROMOTED_DIRNAME = "promoted_clues"


def promoted_dir(case_dir: str | Path) -> Path:
    """提升线索目录（案件级，不挂版本 → 跨版本持久）。"""
    return Path(case_dir) / "artifacts" / PROMOTED_DIRNAME


def save_promoted_clue(case_dir: str | Path, clue: dict) -> Path:
    """落盘一条提升线索（原子写；一个线索一个文件，便于增量与回溯）。"""
    d = promoted_dir(case_dir)
    d.mkdir(parents=True, exist_ok=True)
    cid = str(clue.get("clue_id") or "").strip()
    if not cid:
        raise ValueError("提升线索缺少 clue_id")
    path = d / f"{cid}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clue, ensure_ascii=False, indent=1,
                              default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def load_promoted_clues(case_dir: str | Path) -> list[dict]:
    """读全部提升线索（损坏文件跳过，不拖垮读面）。"""
    d = promoted_dir(case_dir)
    if not d.exists():
        return []
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("clue_id"):
            out.append(data)
    return out


# ----------------------------------------------------------------------
# 观察 → 线索构造
# ----------------------------------------------------------------------
def build_promoted_clue(*, observation: Any, hypothesis_id: str,
                        hypothesis_desc: str = "",
                        operator: str = "", note: str = "",
                        parent_clue_id: str = "") -> dict:
    """从观察构造一条线索（LineageClue dict）。

    关键：assumption_chain = [hypothesis_id] —— 提升的那一刻它才成为
    **待证明的命题**。此前它是无命题的观察，无从证伪。

    证据不复制内容、只搬引用与明细：观察档案本体仍在（随版本），
    线索只持有可定位的证据引用 + 事实摘要，避免两处真值不一致。
    """
    from core.registry import LineageClue

    obs_id = str(getattr(observation, "observation_id", "") or "")
    title = str(getattr(observation, "title", "") or "").strip()
    basis = str(getattr(observation, "basis", "") or "").strip()
    falsification = str(getattr(observation, "falsification", "") or "").strip()
    detail = dict(getattr(observation, "detail", {}) or {})
    subject = str(getattr(observation, "subject", "") or "")
    project = str(getattr(observation, "project", "") or "")

    # 线索标题 = 观察标题（正兵认领的就是这条观察），不改措辞以免对不上
    clue = LineageClue(
        skill_id=str(getattr(observation, "skill_id", "") or ""),
        title=title or f"由观察提升的线索（{obs_id}）",
        # 提升即断言：这条线索要验证的假设（强制，不指定不提升）
        assumption_chain=[hypothesis_id],
        evidence_refs=list(getattr(observation, "evidence_refs", []) or []),
        detail={
            **detail,
            # 保留判据三件套：判据即本线索的"依据"，证伪条件供核查项引用
            "依据": basis,
            "basis": basis,
            "falsification": falsification,
            # 溯源：由哪条观察提升而来（可回跳，审计可复算）
            "observation_id": obs_id,
            "promoted_by": operator,
            "promoted_note": note,
            "parent_clue_id": parent_clue_id,
            "hypothesis_desc": hypothesis_desc,
            "subject": subject or detail.get("subject"),
            "project": project or detail.get("project"),
            "promoted": True,
        },
    )
    d = clue.to_dict()
    # 读面徽标：列表可区分"由观察提升"
    d["promoted_from_observation"] = obs_id
    return d


def hypothesis_choices(*, pack: str = "default", base_dir=None) -> list[dict]:
    """本体已声明的假设列表（提升时的下拉选项）。

    去重：假设模式库里同一假设可能有多条 pattern（不同规则/关键词命中），
    按 id 去重后返回，避免下拉出现重复项。
    """
    try:
        from core.ontology_loader import load_hypothesis_patterns
        raw = load_hypothesis_patterns(pack, base_dir=base_dir)
    except Exception:
        return []
    items = raw if isinstance(raw, list) else (raw or {}).get("hypotheses", [])
    seen: dict[str, dict] = {}
    for it in items or []:
        h = it.get("hypothesis") if isinstance(it, dict) else None
        if not isinstance(h, dict):
            continue
        hid = str(h.get("id") or "").strip()
        if not hid or hid in seen:
            continue
        seen[hid] = {
            "id": hid,
            "description": str(h.get("description") or ""),
            "falsification": str(h.get("falsification") or ""),
            "dimension": list(h.get("dimension") or []),
        }
    return sorted(seen.values(), key=lambda x: x["id"])
