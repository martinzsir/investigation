"""
server/app/worker/detect.py
BUILD/RESCAN 成功后的线索检测编排（D-M3-2：线索报告随版本不可变）。

  语义层新版本库 → 规则手册全量（案件快照 rules.json——规则工坊编辑/启停/
  调参对它生效，base_dir 指向案件快照）→ findings
  → LineageClue（复用 skills 注册适配层，转换逻辑单点维护）
  → 案件级镜头启停过滤（案件快照 lenses.json——启停面板写入，detect 按它
    过滤批量镜头；定向镜头缺必填参数仍跳过留痕）
  → 血缘去重/优先级（与 run_all 同路径）
  → cases/{cid}/artifacts/clues_v{N}.json。

core 既有产出路径不变（run_all 写 output/ JSON）；本模块只在 Web 任务侧编排，
不向 core 加业务逻辑（base_dir 为可选注入，缺省即 CLI/MCP 现状）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import lineage
from core.observation import observation_from_clue
from core.pack_loader import case_batch_lens_tasks
from core.registry import get_registry, skill_invoke
from core.rules import run_rules
from core.store import Store as CoreStore

from server.app.clues_artifact import save_case_clues, save_case_observations
from server.app.snapshot_config import load_lens_overrides

# 导入即注册五技能到 DEFAULT_REGISTRY（register_all 幂等）
from skills import registry_bootstrap  # noqa: F401


def _lens_name_labels(pack: str, base_dir) -> dict[str, str]:
    """镜头 skill_id → 中文名（pack.json 声明，换包自动跟随）。

    取不到就回落 skill_id：宁可显示英文标识，也不硬编码中文（换本体即失效）。
    """
    try:
        from core.pack_loader import discover
        from core.registry import get_registry
        discover()
        return {s.skill_id: s.name
                for s in get_registry().all_specs()
                if getattr(s, "name", "")}
    except Exception:
        return {}


def run_detection(*, version_file: Path, case_dir: Path, version: int,
                  pack: str, snapshot_base: Path) -> dict[str, Any]:
    """在新版本库上跑检测并落线索报告产物。

    返回 {"clues": 线索数, "raw_findings": 规则命中数, "artifact": 产物路径}。
    抛异常由调用方转 TaskExecError（检测失败 = BUILD 失败，版本指针不前进）。
    """
    det = CoreStore(db_path=str(version_file))
    try:
        reg = get_registry()
        all_clues: list = []

        # 1) 虚实：规则手册全量阶段（base_dir=案件快照；停用规则不执行/不产线索）
        #    快照目录缺失（如测试用 stub builder 不编译 ontology）→ 无规则可跑，
        #    降级为空产物；生产中 build_ontology 成功即包快照必在。
        try:
            findings = run_rules(det, stage=None, pack=pack,
                                 base_dir=snapshot_base)
        except FileNotFoundError:
            findings = []
        xs_spec = reg.skill("xu_shi")
        all_clues.extend(
            registry_bootstrap._clue_from_xu_shi(
                xs_spec, {"虚实扫描": {"findings": findings}}))

        # 观察档案收集器（镜头 + 用间单源行共用；线索与观察分开落盘）
        observations: list = []

        # 1b) 庙算沙盘实例化（此前 Web 侧 miao=None，庙算形同不存在：
        #     假设页只能从线索产物反推，_infer_assumptions 兜底恒返回空）。
        #     知己从案件数据派生（零行语义表=证据缺口；授权边界不编造），
        #     人工假设从 cases/<cid>/hypotheses.json 合并（跨版本持久）。
        try:
            from server.app import miao_ji, hypotheses_store
            from server.app.clues_artifact import save_case_hypotheses
            manual = hypotheses_store.load_manual(case_dir)
            miao, _miao_meta = miao_ji.build_miaosuan(
                case_dir=case_dir, conn=det.conn, pack=pack,
                base_dir=snapshot_base, findings=findings, manual=manual)
        except Exception:
            miao, _miao_meta = None, {}

        # 2) 奇正/用间：Function 编排（functions 声明随包快照一致）
        #
        #    用间产出按**交叉等级**分流：五间方法论自己声明了
        #    「单源=观察 → 双源=线索 → 三源=可立案依据候选」，但此前
        #    adapter 只看"命中"，单源也产线索，规则从未兑现。
        #    判据用**本间独立源数**：全局独立源数是全案汇总，数据接全了
        #    恒为 3 级，对单间没有区分力。
        for sid in ("qi_zheng", "yong_jian"):
            produced = skill_invoke(reg, sid, store=det, ctx={}, miao=miao)
            if sid == "yong_jian":
                kept, obs = registry_bootstrap.split_yong_jian(produced)
                all_clues.extend(kept)
                observations.extend(obs)
            else:
                all_clues.extend(produced)

        # 2b) P4/P5 镜头包批量接线 + 案件级启停 → **观察档案**，不是线索。
        #
        #     关键转向：镜头产出**不进线索清单**。理由不是"不确定"（规则
        #     同样 deterministic、同样可复现），而是**没有常态基线**——
        #     规则回答"跟常态比算不算异常"（判定），镜头只回答"这个结构
        #     存不存在"（观测）。观测不是命题，无假设可证伪，进处置清单
        #     只能空转（实测此前 10 条镜头线索全"查证中"而假设链全空）。
        #
        #     调度判据仍是「能不能自动确定靶心」（auto_from 推导），
        #     推得出就跑、推不出才跳过留痕 —— 与 CLI 同口径，避免 Web 建案
        #     与命令行两套线索集。ctx["clues"] 前置入：靶心三级源含
        #     「前序线索反推」，与 run_all 同路径。
        ctx_lens: dict[str, Any] = {"clues": list(all_clues)}
        lens_tasks, lens_unresolved, case_disabled = case_batch_lens_tasks(
            reg, load_lens_overrides(case_dir),
            store=det, ctx=ctx_lens, pack=pack, base_dir=snapshot_base)
        for sid, prm in lens_tasks:
            _src = prm.pop("_param_source", "")
            clues = skill_invoke(reg, sid, store=det, ctx=ctx_lens,
                                 params=prm)
            for c in clues:
                c.detail.setdefault("param_source", _src)
                observations.append(observation_from_clue(c))
        requires_params = lens_unresolved

        # 观察档案独立落盘（不混进 clues 产物 → 不进处置清单/看板计数）。
        # 本体随版本可复现；正兵的认领/提升落 state.sqlite 跨版本持久。
        # 镜头中文名在此翻译（本体声明，换本体自动跟随）。
        obs_labels = _lens_name_labels(pack, snapshot_base)
        for o in observations:
            o.lens_name = obs_labels.get(o.skill_id, o.skill_id)
        obs_artifact = save_case_observations(case_dir, version, observations)

        # 3) 血缘去重 + 优先级排序（与 run_all 第 7-8 步同路径）
        merged = lineage.dedupe_and_merge(all_clues, threshold=0.5)
        # 实证轨全量 findings：虚实 + 本轮全部产线已落 dimension 的 finding。
        # 建沙盘时只有虚实阶段，不补全会把奇正（R6/R7 时间维度）已命中的
        # 证据误算成"实证缺口"。
        _all_det_findings: list = list(findings)
        try:
            for c in all_clues:
                _d = (c.detail or {}).get("dimension")
                if _d:
                    _all_det_findings.append({"dimension": _d})
        except Exception:
            pass
        merged = lineage.prioritize_clues(merged)
        artifact = save_case_clues(case_dir, version, merged)

        # 4) 假设产物（随版本可复现）：供假设页读真实假设清单，
        #    不再只从线索反推。知己派生标记一并落盘，UI 需明示。
        hyp_payload: dict[str, Any] = {"source": "derived+manual",
                                       "miao_meta": _miao_meta}
        if miao is not None:
            try:
                hyp_payload["hypotheses"] = [h.to_dict()
                                             for h in miao.hypotheses]
                hyp_payload["coverage"] = miao.report(
                    None, findings=_all_det_findings).get("dimension_coverage")
            except Exception:
                pass
        hyp_artifact = save_case_hypotheses(case_dir, version, hyp_payload)
    finally:
        det.close()
    return {"clues": len(merged), "raw_findings": len(findings),
            "artifact": str(artifact), "lens_batch_skipped": requires_params,
            "lens_case_disabled": case_disabled,
            "observations": len(observations),
            "observation_artifact": str(obs_artifact),
            "hypotheses": len(hyp_payload.get("hypotheses") or []),
            "hypotheses_artifact": str(hyp_artifact),
            "ji_configured": bool(_miao_meta.get("ji_configured"))}
