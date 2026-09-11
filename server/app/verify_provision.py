"""
server/app/verify_provision.py
REQ-V-002 核查项惰性供给（读面组装；纯函数，不自己开库、不写 artifact）。

把 evidence_builder.build_evidence() 的三栏产出映射为核查项清单：
  inference            → kind=inference（推断待核实）
  pending id 前缀 d    → kind=pending_degrade（判据降级，仅 is_degraded 时存在）
  pending id 前缀 h    → kind=pending_hypothesis（待验证假设）
  pending id 前缀 r    → 不成项（规则判据留痕卡，非可裁决任务）
  fact                 → 不成项（事实卡本身不是核查任务）

唯一同源输入：build_evidence 的三栏产出——文本逐字透传，不二次渲染
（实施方案 REQ-V-002 AC4）。

合并线索规则字段回填（方案 b，2026-09-11 demoF v8 核对）：
  core/lineage.py 合并段只往 detail 写 merged_from，parts 的
  rule_id/rule_text/依据 全丢，evidence_builder 对合并线索只出事实栏。
  backfill_rule_fields() 在供给侧按"合并标题中保留的部件标题"反查规则目录
  （rules.json 经 ontology_loader.load_pack）：
    - finding 标题在 core/rules.py 形如 f"{subject} · {rule.title}"，
      合并标题以 " | " 拼接部件标题，故规则标题子串匹配即语义来源追溯；
    - 规则声明 subject_column 时，要求该列出现在线索 source_rows 列集内
      （R6=资金主体；R2=from_raw/R4=person_1 据此排除）；
    - 零命中不回填（fail-safe：宁可不供，不可错供）；多命中是多规则合并
      线索的真实语义（如 demoF v8 clue_365275f8 = R3+R4），双重校验下
      每一条都是真实命中 → 逐条回填：主字段取声明序第一条（兼容旧消费方），
      全列表写 detail["rules"]，evidence_builder 逐条渲染推断/假设卡。
  回填值只用于证据/供给渲染，绝不回写 artifact（ADR-V-5；方案 a 改合并段
  保留 parts 字段记入后续 core 治理）。
"""
from __future__ import annotations

from typing import Any

from core.ontology_loader import load_pack

#: pending 卡 id 前缀（evidence_builder 产出约定）→ 核查项 kind。
#: r（规则判据留痕）不在表中 → 不成项（REQ-V-002 AC6）。
_PENDING_KIND_BY_PREFIX = {
    "d": "pending_degrade",
    "h": "pending_hypothesis",
}


def backfill_rule_fields(raw_clue: dict[str, Any], *,
                         pack_id: str = "default",
                         base_dir=None) -> dict[str, Any]:
    """合并线索缺规则字段时，按合并标题反查规则目录回填（供渲染用的副本）。

    无需回填（非合并线索 / detail 已带 rule_id）或零命中时原对象返回；
    多命中按声明序逐条回填（主字段=第一条 + detail["rules"] 全列表）。
    回填永远不修改入参 raw_clue。
    """
    det = raw_clue.get("detail") or {}
    if det.get("rule_id"):
        return raw_clue
    if not (det.get("merged_from") or []):
        return raw_clue
    title = raw_clue.get("title") or ""
    if not title:
        return raw_clue

    row_keys: set[str] = set()
    for sr in raw_clue.get("source_rows") or []:
        if isinstance(sr, dict):
            row_keys.update(str(k) for k in sr.keys())

    spec = load_pack(pack_id, base_dir=base_dir)
    candidates = []
    for rule in spec.rules.values():
        if not rule.title or rule.title not in title:
            continue
        # 声明了主体列则必须在行集列内，否则不是该规则的产物
        if rule.subject_column and rule.subject_column not in row_keys:
            continue
        candidates.append(rule)
    if len(candidates) != 1:
        return raw_clue

    rule = candidates[0]
    new_detail = dict(det)
    new_detail["rule_id"] = rule.id
    if rule.rule_text:
        new_detail["rule_text"] = rule.rule_text
    if not new_detail.get("依据") and not raw_clue.get("依据") \
            and rule.basis_text:
        new_detail["依据"] = rule.basis_text
    if len(candidates) > 1:
        # 多规则合并线索（如 demoF v8 clue_365275f8 = R3+R4）：主字段只保留
        # 第一条（兼容旧消费方），全列表供 evidence_builder 逐条渲染
        new_detail["rules"] = [
            {"rule_id": r.id, "rule_text": r.rule_text or "",
             "依据": r.basis_text or ""}
            for r in candidates
        ]
    new_raw = dict(raw_clue)
    new_raw["detail"] = new_detail
    return new_raw


def provision_from_evidence(
        evidence: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """build_evidence 三栏产出 → auto 核查项 [{kind, text}]。

    文本与三栏逐字一致（同源透传）；fact/r 卡不成项。
    """
    items: list[dict[str, str]] = []
    for ev in evidence or []:
        kind = ev.get("kind")
        if kind == "inference":
            mapped = "inference"
        elif kind == "pending":
            prefix = str(ev.get("id") or "")[:1]
            mapped = _PENDING_KIND_BY_PREFIX.get(prefix)
            if mapped is None:
                continue  # r 卡：规则判据留痕，非可裁决任务
        else:
            continue  # fact 等不成项
        text = ev.get("text") or ""
        if not text:
            continue
        items.append({"kind": mapped, "text": text})
    return items


def provision_for_clue(raw_clue: dict[str, Any], *,
                       pack_id: str = "default",
                       base_dir=None, access=None) -> list[dict[str, str]]:
    """线索 artifact raw → auto 核查项清单（backfill + build_evidence 组合入口）。

    供测试与其它读面复用；clues_view.assemble_detail 需要同时回显三栏证据时，
    直接组合 backfill_rule_fields + build_evidence + provision_from_evidence，
    保证证据响应与核查项同源且只构建一次。
    """
    from server.app.evidence_builder import build_evidence

    raw_for_view = backfill_rule_fields(
        raw_clue, pack_id=pack_id, base_dir=base_dir)
    evidence = build_evidence(
        raw_clue=raw_for_view, conn=None, pack_id=pack_id,
        base_dir=base_dir, access=access)
    return provision_from_evidence(evidence)
