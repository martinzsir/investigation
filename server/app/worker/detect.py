"""
server/app/worker/detect.py
BUILD/RESCAN 成功后的线索检测编排（D-M3-2：线索报告随版本不可变）。

  语义层新版本库 → 规则手册全量（案件快照 rules.json——规则工坊编辑/启停/
  调参对它生效，base_dir 指向案件快照）→ findings
  → LineageClue（复用 skills 注册适配层，转换逻辑单点维护）
  → 血缘去重/优先级（与 run_all 同路径）
  → cases/{cid}/artifacts/clues_v{N}.json。

core 既有产出路径不变（run_all 写 output/ JSON）；本模块只在 Web 任务侧编排，
不向 core 加业务逻辑（base_dir 为可选注入，缺省即 CLI/MCP 现状）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import lineage
from core.registry import get_registry, skill_invoke
from core.rules import run_rules
from core.store import Store as CoreStore

from server.app.clues_artifact import save_case_clues

# 导入即注册五技能到 DEFAULT_REGISTRY（register_all 幂等）
from skills import registry_bootstrap  # noqa: F401


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

        # 2) 奇正/用间：Function 编排（functions 声明随包快照一致；
        #    miao=None：前置校验对非庙算/知己阶段跳过，Web 无庙算沙盘输入）
        for sid in ("qi_zheng", "yong_jian"):
            all_clues.extend(skill_invoke(reg, sid, store=det, ctx={}))

        # 3) 血缘去重 + 优先级排序（与 run_all 第 7-8 步同路径）
        merged = lineage.dedupe_and_merge(all_clues, threshold=0.5)
        merged = lineage.prioritize_clues(merged)
        artifact = save_case_clues(case_dir, version, merged)
    finally:
        det.close()
    return {"clues": len(merged), "raw_findings": len(findings),
            "artifact": str(artifact)}
