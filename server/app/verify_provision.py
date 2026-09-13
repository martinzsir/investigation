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

import logging
import re
from collections import Counter
from typing import Any

from core.ontology_loader import load_pack, load_verify_playbooks

#: pending 卡 id 前缀（evidence_builder 产出约定）→ 核查项 kind。
#: r（规则判据留痕）不在表中 → 不成项（REQ-V-002 AC6）。
_PENDING_KIND_BY_PREFIX = {
    "d": "pending_degrade",
    "h": "pending_hypothesis",
}

#: 聚合型技能：推断口径为表级覆盖度（Function COUNT），行集=表级汇总行。
#: 双向盘点（zhi_ji_zhi_bi）等技能无确定性 COUNT 口径，不在此列。
_AGGREGATE_SKILLS = frozenset({"yong_jian"})
#: 表级汇总行口径标记（与 skills.registry_bootstrap.AGGREGATE_GRANULARITY 同值）
_AGGREGATE_GRANULARITY = "表级汇总"


def backfill_aggregate_rows(raw_clue: dict[str, Any],
                            cross_rows: list[dict] | None) -> dict[str, Any]:
    """方案 B：旧版聚合线索产物无行集时，按 Function 当前 COUNT 结果补
    表级汇总行（只渲染、不回写 artifact；与 backfill_rule_fields 同纪律）。

    cross_rows : jian_cross_level Function result["rows"]（调用方持只读
                 连接执行后注入；本函数不开库）。
    匹配口径（fail-safe，对不上原样返回 → evidence_builder 落 a 卡降级）：
      - 仅处理 skill_id ∈ _AGGREGATE_SKILLS 且 source_rows 为空的线索；
      - Function 行的「间」∈ jian_types，且命中明细 source ∈ detail.数据源。
    """
    if raw_clue.get("skill_id") not in _AGGREGATE_SKILLS:
        return raw_clue
    if raw_clue.get("source_rows"):
        return raw_clue
    det = raw_clue.get("detail") or {}
    jian_types = set(str(j) for j in raw_clue.get("jian_types") or [])
    declared = set(str(s) for s in det.get("数据源") or [])
    for row in cross_rows or []:
        if row.get("间") not in jian_types:
            continue
        details = row.get("命中明细")
        if not isinstance(details, list):
            continue
        matched = [d for d in details
                   if isinstance(d, dict) and d.get("table")
                   and str(d.get("source") or "") in declared]
        if not matched:
            continue
        new_rows = [{
            "数据源": str(d.get("source") or ""),
            "语义表": str(d.get("table") or ""),
            "对象类型": str(d.get("obj_name") or ""),
            "行数": int(d.get("n") or 0),
            "粒度": _AGGREGATE_GRANULARITY,
        } for d in matched]
        new_detail = dict(det)
        new_detail.setdefault("行集口径", _AGGREGATE_GRANULARITY)
        new_raw = dict(raw_clue)
        new_raw["detail"] = new_detail
        new_raw["source_rows"] = new_rows
        return new_raw
    return raw_clue


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
    if not candidates:
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


def stamp_row_datasets(raw_clue: dict[str, Any], *,
                       pack_id: str = "default",
                       base_dir=None) -> dict[str, Any]:
    """数据源图章读侧回填（历史产物兼容；只渲染、不回写 artifact）。

    历史产物的规则聚合行不带「数据源」（如 q/cnt/amt、主体/对端/次数 等
    Function 短列名行），展示与「查看所属文件」无法命中 ingest 登记件。
    本回填按声明链补图章：rule_id → Function.inputs → bindings.source_table
    （ingest 登记表名），与 core.rules.run_rules 生成期图章同源——
    新产物生成期已带图章，此处为空操作。

    行级归属：声明 subject_column 且该列在行内 → 该规则的登记源
    （R6=资金主体/R2=from_raw/R4=person_1，与 backfill_rule_fields 同语义）；
    其余行按规则声明序取首个有声明源的规则。无规则可依（用间表级汇总行
    自带数据源）原样返回。
    """
    det = raw_clue.get("detail") or {}
    rule_ids: list[str] = []
    if det.get("rule_id"):
        rule_ids.append(str(det["rule_id"]))
    for r in det.get("rules") or []:
        if isinstance(r, dict) and r.get("rule_id") \
                and str(r["rule_id"]) not in rule_ids:
            rule_ids.append(str(r["rule_id"]))
    rows = raw_clue.get("source_rows")
    if not rule_ids or not isinstance(rows, list) or not rows:
        return raw_clue

    spec = load_pack(pack_id, base_dir=base_dir)
    datasets_by_rule: dict[str, list[str]] = {}
    ordered: list[list[str]] = []
    for rid in rule_ids:
        rule = spec.rules.get(rid)
        if rule is None:
            continue
        from core.rules import declared_datasets
        ds = declared_datasets(spec, rule.function)
        if ds:
            datasets_by_rule[rid] = ds
            ordered.append(ds)
    if not ordered:
        return raw_clue
    fallback = ordered[0]

    changed = False
    new_rows = []
    for sr in rows:
        if not isinstance(sr, dict) or str(sr.get("数据源") or "").strip():
            new_rows.append(sr)
            continue
        stamp = fallback
        for rid in rule_ids:
            rule = spec.rules.get(rid)
            sc = getattr(rule, "subject_column", "") if rule else ""
            if sc and sc in sr and datasets_by_rule.get(rid):
                stamp = datasets_by_rule[rid]
                break
        new_rows.append({**sr, "数据源": stamp[0]})
        changed = True
    if not changed:
        return raw_clue
    new_raw = dict(raw_clue)
    new_raw["source_rows"] = new_rows
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
                       base_dir=None, access=None,
                       cross_rows: list[dict] | None = None
                       ) -> list[dict[str, str]]:
    """线索 artifact raw → auto 核查项清单（backfill + build_evidence 组合入口）。

    供测试与其它读面复用；clues_view.assemble_detail 需要同时回显三栏证据时，
    直接组合 backfill_rule_fields + backfill_aggregate_rows + build_evidence
    + provision_from_evidence，保证证据响应与核查项同源且只构建一次。

    cross_rows：jian_cross_level Function rows（方案 B 聚合行集回填注入）。
    """
    from server.app.evidence_builder import build_evidence

    raw_for_view = backfill_rule_fields(
        raw_clue, pack_id=pack_id, base_dir=base_dir)
    raw_for_view = backfill_aggregate_rows(raw_for_view, cross_rows)
    raw_for_view = stamp_row_datasets(raw_for_view, pack_id=pack_id,
                                      base_dir=base_dir)
    evidence = build_evidence(
        raw_clue=raw_for_view, conn=None, pack_id=pack_id,
        base_dir=base_dir, access=access)
    return provision_from_evidence(evidence)


# ----------------------------------------------------------------------
# REQ-V-018：核查手册建议项确定性渲染
# ----------------------------------------------------------------------
logger = logging.getLogger(__name__)

_HYP_RE = re.compile(r"H\d+")
#: 金额列候选（首个存在的列生效；demoF R6=金额，合成行集兼容 amount）
_AMOUNT_COLUMNS = ("金额", "amount")


def _assumptions_of(raw_for_view: dict[str, Any],
                    evidence: list[dict[str, Any]] | None) -> set[str]:
    """有效假设并集：detail/raw 的 assumption_chain ∪ 待核实 h 卡文本中的 H\\d+。

    h 卡文本由 evidence_builder 逐字产出（`待验证假设：H1（…）`），此处只做
    正则解析、不二次跑关键词匹配（与三栏同源，REQ-V-018 步骤 3）。
    """
    hyps: set[str] = set()
    for src in ((raw_for_view or {}).get("detail") or {},
                raw_for_view or {}):
        for v in (src.get("assumption_chain") or []):
            s = str(v)
            if _HYP_RE.fullmatch(s):
                hyps.add(s)
    for ev in evidence or []:
        if (ev.get("kind") == "pending"
                and str(ev.get("id") or "").startswith("h")):
            hyps.update(_HYP_RE.findall(ev.get("text") or ""))
    return hyps


def _subject_stats(raw_for_view: dict[str, Any], rule_id: str, *,
                   pack_id: str, base_dir=None) -> tuple[str, int] | None:
    """槽位统计：行集按主体列过滤计数 → (最多主体, 计数)；无有效主体 → None。

    过滤口径（可声明复现，D1=8）：
      - 主体列（rule.subject_column）值非空字符串、不含
        rule.params.exclude_org_suffix（R6="公司"，与检测 SQL NOT LIKE 同口径）；
      - 金额列（金额/amount，首个存在的列生效）可解析为数值且 > 0 且
        % round_unit == 0（round_unit 取 rule.params，缺省 1）；
      - 窗口判据由行集本身保证（行集即检测函数 ±20 天产物），不再另造阈值；
      - 并列取名称排序首者（确定性，D1）。
    """
    rule = load_pack(pack_id, base_dir=base_dir).rules.get(rule_id)
    if rule is None or not rule.subject_column:
        return None
    params = rule.params or {}
    try:
        round_unit = int(params.get("round_unit", 1))
    except (TypeError, ValueError):
        round_unit = 1
    exclude_suffix = str(params.get("exclude_org_suffix", "") or "")

    counter: Counter[str] = Counter()
    for r in raw_for_view.get("source_rows") or []:
        if not isinstance(r, dict):
            continue
        subj = r.get(rule.subject_column)
        if not isinstance(subj, str) or not subj.strip():
            continue
        if exclude_suffix and exclude_suffix in subj:
            continue
        ok = True
        for col in _AMOUNT_COLUMNS:
            if col not in r:
                continue
            try:
                amt = float(r[col])
            except (TypeError, ValueError):
                ok = False
                break
            if amt <= 0 or round_unit <= 0 or amt % round_unit != 0:
                ok = False
            break
        if ok:
            counter[subj.strip()] += 1
    if not counter:
        return None
    # 最多者；并列取名称排序首者（(-n, s) 字典序最小）
    top = min((-n, s) for s, n in counter.items())
    return top[1], -top[0]


def render_suggested(raw_for_view: dict[str, Any],
                     evidence: list[dict[str, Any]] | None, *,
                     pack_id: str = "default",
                     base_dir=None) -> tuple[list[dict[str, Any]],
                                             list[dict[str, str]]]:
    """核查手册建议项渲染（纯函数：不写库、不回写 artifact，AC7）。

    匹配口径：playbook.match.rule_id == detail.rule_id（合并线索经
    backfill_rule_fields 回填后），且（playbook 未声明 assumption，
    或与有效假设并集有交集）。fact/r 卡永不成为建议项（AC7）。

    返回 (items, skipped)：
      items=[{kind:'suggested', text, origin:'suggested', status:'建议',
              channel, ref_function, external?, falsification}]，
      item 稳定键由调用方 upsert_verify_items 按
      verify_item_key(clue_id, 'suggested', text) 生成（ADR-V-4）；
      skipped=[{playbook_id, reason}]（调用方留痕——读面无 run handle，
      不写 run_diagnostic，D3）。
    """
    det = (raw_for_view or {}).get("detail") or {}
    rule_id = det.get("rule_id")
    if not rule_id:
        return [], []
    playbooks = [pb for pb in load_verify_playbooks(pack_id, base_dir)
                 if pb["rule_id"] == rule_id]
    if not playbooks:
        return [], []

    hyps = _assumptions_of(raw_for_view, evidence)
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    stats: tuple[str, int] | None = None
    stats_done = False
    for pb in playbooks:
        # 假设门：playbook 未约束或与有效集有交集才命中（不命中=正常筛选，非 skip）
        if pb["assumptions"] and not (hyps & set(pb["assumptions"])):
            continue
        text = pb["text"]
        if "{subject}" in text or "{project_count}" in text:
            if not stats_done:
                stats = _subject_stats(raw_for_view, rule_id,
                                       pack_id=pack_id, base_dir=base_dir)
                stats_done = True
            if stats is None:
                skipped.append({"playbook_id": pb["id"],
                                "reason": "过滤后无有效主体"})
                continue
            text = (text.replace("{subject}", stats[0])
                        .replace("{project_count}", str(stats[1])))
        item: dict[str, Any] = {
            "kind": "suggested", "text": text,
            "origin": "suggested", "status": "建议",
            "channel": pb["channel"],
            "ref_function": pb["function"] or "",
            "falsification": pb["falsification"],
            # 手册条目溯源（M4 RC-105：画布建议节点 ref=playbook_id 由此挂钩；
            # upsert_verify_items 只读已知键，额外键对 state 零影响）
            "playbook_id": pb["id"],
        }
        if pb["channel"] == "external" and pb.get("external"):
            item["external"] = pb["external"]
        items.append(item)
    return items, skipped
