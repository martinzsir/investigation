"""OBS_PROMOTE 任务：把观察提升为线索（人工认领，强制指定假设）。

写两处，顺序有讲究：
  1. 先落提升线索产物（artifacts/promoted_clues/，案件级、跨版本）
  2. 再写 state 的观察处置记录（disposition=已提升 + promoted_clue_id）

反过来的话，若产物写失败而 state 已标记"已提升"，正兵会看到一个
跳不过去的线索链接——宁可 state 没记，也不能记了却打不开。
"""
from __future__ import annotations

from typing import Any

from server.app import observation_promote as promote_mod
from server.app.clues_artifact import load_case_observations
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError


def handle_obs_disposition(task, *, repo, factory, **_: Any) -> dict[str, Any]:
    """观察认领/归档：只写 state（跨版本持久），不产线索。

    与提升（handle_obs_promote）分开：提升必须带假设、会产线索；
    认领/归档只是正兵的"我看过了/先放着"标记，不改变观察的性质。
    """
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    p = task.params or {}
    obs_id = str(p.get("observation_id") or "").strip()
    disp = str(p.get("disposition") or "").strip()
    operator = str(p.get("operator") or "").strip()
    note = str(p.get("note") or "").strip()

    if not obs_id:
        raise TaskExecError("OBS_REQUIRED", "缺少 observation_id")
    if disp not in ("已认领", "已归档", "未认领"):
        raise TaskExecError("DISPOSITION_INVALID",
                            f"非法处置态：{disp!r}")

    st = StateStore(case_id=case.id,
                    path=factory.case_dir(case.id) / "state.sqlite")
    try:
        rec = st.set_observation_disposition(
            obs_id, case.id, disp, note=note, operator=operator)
    finally:
        try:
            st.close()
        except Exception:
            pass
    return {"observation_id": obs_id, "disposition": rec.get("disposition", disp),
            "operator": operator}


def handle_obs_promote(task, *, repo, factory, **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")

    p = task.params or {}
    obs_id = str(p.get("observation_id") or "").strip()
    hypothesis = str(p.get("hypothesis") or "").strip()
    operator = str(p.get("operator") or "").strip()
    role = str(p.get("role") or "正兵")
    note = str(p.get("note") or "").strip()
    parent_clue_id = str(p.get("parent_clue_id") or "").strip()

    if not obs_id:
        raise TaskExecError("OBS_REQUIRED", "缺少 observation_id")
    if not hypothesis:
        # 强制假设：不指定假设的线索无法处置，等于没解决问题
        raise TaskExecError("HYPOTHESIS_REQUIRED",
                            "提升为线索必须指定待验证的假设（hypothesis）")

    ver = repo.current_version(case.id)
    if ver < 1:
        raise TaskExecError("CLUES_NOT_READY", "案件尚无分析版本，无法提升")

    case_dir = factory.case_dir(case.id)
    obs = load_case_observations(case_dir, ver)
    target = next((o for o in obs if o.observation_id == obs_id), None)
    if target is None:
        raise TaskExecError("OBS_NOT_FOUND",
                            f"观察不存在或已不在当前版本：{obs_id}")

    # 假设必须是本体已声明的（换本体自动跟随，不硬编码 H1..Hn）
    try:
        choices = promote_mod.hypothesis_choices(
            pack=case.pack_id,
            base_dir=factory.case_dir(case.id) / "ontology")
    except Exception:
        choices = []
    allowed = {c["id"] for c in choices}
    desc = next((c["description"] for c in choices if c["id"] == hypothesis), "")
    if allowed and hypothesis not in allowed:
        raise TaskExecError(
            "HYPOTHESIS_INVALID",
            f"假设 {hypothesis!r} 未在本体声明（可用："
            f"{', '.join(sorted(allowed))}）")

    # 1) 落提升线索产物（先产物，后 state）
    clue = promote_mod.build_promoted_clue(
        observation=target, hypothesis_id=hypothesis,
        hypothesis_desc=desc, operator=operator, note=note,
        parent_clue_id=parent_clue_id)
    promote_mod.save_promoted_clue(case_dir, clue)

    # 2) 写观察处置记录（跨版本持久）
    try:
        st = StateStore(case_id=case.id,
                        path=case_dir / "state.sqlite")
    except Exception:
        st = None
    if st is not None:
        try:
            st.set_observation_disposition(
                obs_id, case.id, "已提升", note=note, operator=operator,
                promoted_clue_id=str(clue.get("clue_id") or ""),
                promoted_hypothesis=hypothesis)
        except Exception as e:
            raise TaskExecError(
                "STATE_WRITE_FAILED",
                f"观察处置记录写入失败：{e}（线索产物已落盘，请重试）")
        finally:
            try:
                st.close()
            except Exception:
                pass

    return {
        "observation_id": obs_id,
        "clue_id": clue.get("clue_id", ""),
        "hypothesis": hypothesis,
        "title": clue.get("title", ""),
        "operator": operator,
        "role": role,
    }
