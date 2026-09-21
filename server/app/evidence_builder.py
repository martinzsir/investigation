"""
server/app/evidence_builder.py
B2 证据三栏产出器：把线索 raw artifact → EvidenceItem[]。

前端 partitionEvidence（clue.ts L196-212）直接消费产出：
  - fact：按行聚合（每条 source_row → 一条事实）
  - inference：detail.依据 作为推断文本，挂全部 source_rows
  - pending：降级标记 + 假设匹配

红线：
  - FE-T-004：推断无 source_rows → 前端丢弃 + droppedInferences 计数
  - FE-T-005：三栏物理分隔，推断内容不混入事实栏
  - #7：推断文本不含定性措辞（依据是 Function 确定性产出，中性透传）
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from core.hypotheses import MiaoSuan
from server.app.source_row_dto import (
    dataset_of_row, display_labels_for_row, format_field_value,
)


def build_evidence(
    *, raw_clue: dict[str, Any],
    conn=None,
    pack_id: str = "default",
    base_dir=None,
    access=None,
) -> list[dict[str, Any]]:
    """产出 EvidenceItem 列表（fact/inference/pending）。

    参数：
      raw_clue  : 线索 artifact 的一项（LineageClue.to_dict() 或 rules finding dict）
      conn      : DuckDB 只读连接（URI 取回需要，当前纯数据模式不用）
      pack_id   : ontology 案件包名
      base_dir  : ontology 根目录
      access    : AccessContext（遮蔽用，B1 source_row_dto 内部处理）

    返回：list[EvidenceItem dict]，前端 partitionEvidence 直接消费。
    """
    detail = raw_clue.get("detail") or {}
    source_rows = raw_clue.get("source_rows") or []
    clue_id = raw_clue.get("clue_id", "")
    is_degraded = raw_clue.get("is_degraded", False)

    items: list[dict[str, Any]] = []

    refs = [_make_source_ref(sr, idx, clue_id, pack_id=pack_id,
                             base_dir=base_dir)
            for idx, sr in enumerate(source_rows)
            if isinstance(sr, dict)]

    # ---- 事实栏（fact）：按行聚合 ----
    for idx, sr in enumerate(source_rows):
        if not isinstance(sr, dict):
            continue
        items.append({
            "id": f"f{clue_id}-{idx}",
            "kind": "fact",
            "text": _fact_text(sr),
            "source_rows": [_make_source_ref(sr, idx, clue_id,
                                             pack_id=pack_id,
                                             base_dir=base_dir)],
        })

    # ---- 镜头线索的事实栏 ----
    # 镜头线索 source_rows=0、证据走 evidence_refs/事件明细。原实现只认
    # source_rows，导致详情页三栏全空。这里补上：有事件明细优先（人能读的
    # brief），否则退回证据引用（至少可定位）。同时产出可挂的 source_rows，
    # 使下面的推断卡不被前端按 FE-T-004 丢弃。
    _skill = str(raw_clue.get("skill_id") or "")
    if not source_rows:
        ev_items, ev_rows = _lens_event_items(detail, clue_id)
        if ev_items:
            items.extend(ev_items)
            refs = refs + ev_rows
        else:
            rf_items, rf_rows = _ref_items(
                raw_clue.get("evidence_refs") or [], clue_id)
            if rf_items:
                items.extend(rf_items)
                refs = refs + rf_rows

    # ---- 规则解析：多规则合并线索 detail.rules（回填）优先；否则回落单规则 ----
    rules = _extract_rules(detail, raw_clue)

    # ---- 推断栏（inference）：规则依据 → 一规则一卡（同文本去重）----
    seen_basis: set[str] = set()
    for ridx, rule in enumerate(rules):
        basis = rule["basis"]
        if not basis or basis in seen_basis:
            continue
        seen_basis.add(basis)
        suffix = "" if len(rules) == 1 else f"-{ridx}"
        items.append({
            "id": f"i{clue_id}{suffix}",
            "kind": "inference",
            "text": basis,
            "source_rows": refs if refs else None,
            # 无 source_rows → 前端 partitionEvidence 丢弃 + droppedInferences++
        })

    # 无规则回填的旧产物：top-level/detail 依据仍出单推断卡（向后兼容）
    legacy_basis = detail.get("依据") or raw_clue.get("依据") or ""
    if not rules and legacy_basis and legacy_basis not in seen_basis:
        if refs:
            # 方案 B：聚合线索（用间交叉）挂表级汇总行后，推断回到推断栏——
            # 表级 COUNT 也是确定性溯源，行集口径在事实卡文本/URI 中明示。
            items.append({
                "id": f"i{clue_id}",
                "kind": "inference",
                "text": legacy_basis,
                "source_rows": refs,
            })
        else:
            # 方案 A：非规则聚合线索（用间交叉/双向盘点）生产端只有表级摘要、
            # 无行级证据，不允许产出无溯源推断（FE-T-004 会被前端拒绝并报警告）。
            # 降级为待核实聚合卡：id 前缀 'a' → provision 不映射为核查项、
            # 前端不挂点击核查；行集口径补齐后（方案 B，适配器/读侧回填挂
            # source_rows）自动回到推断栏。
            items.append({
                "id": f"a{clue_id}",
                "kind": "pending",
                "text": f"案件级聚合（无行级溯源）：{legacy_basis}",
                "source_rows": [],
            })

    # ---- 待核实栏（pending）----
    # 1. 降级标记
    if is_degraded:
        degrade_reason = detail.get("degrade_reason", "判据已降级")
        items.append({
            "id": f"d{clue_id}",
            "kind": "pending",
            "text": f"判据已降级：{degrade_reason}",
            "source_rows": [],
        })

    # ---- 镜头线索的推断栏（判据）----
    # 镜头此前只出标题（如"张卫国 在…公示日前后 7 天跨类型碰撞"），
    # 详情页**没有一句"这说明什么"**——正兵要么去画布数节点，要么放弃。
    # 判据由 core.lens_basis 生成：陈述观测 + 与常态比，**不作定性**。
    # 优先用生产时写入的 detail.basis；存量产物从 detail 重建兜底。
    if not rules and not legacy_basis:
        try:
            from core.lens_basis import basis_from_detail
            lb = basis_from_detail(_skill, detail)
        except Exception:
            lb = {"basis": "", "falsification": "", "claims": []}
        if lb.get("basis"):
            if refs:
                items.append({
                    "id": f"i{clue_id}",
                    "kind": "inference",
                    "text": lb["basis"],
                    "source_rows": refs,
                })
            else:
                # 无溯源支撑的判据不许进推断栏（FE-T-004）：降级为待核实，
                # 不静默丢弃也不冒充推断。
                items.append({
                    "id": f"a{clue_id}",
                    "kind": "pending",
                    "text": f"镜头判据（无行级溯源）：{lb['basis']}",
                    "source_rows": [],
                })
            # 证伪条件：回答"什么情况下这不成立"，帮助正兵反驳而非只接受
            if lb.get("falsification"):
                items.append({
                    "id": f"z{clue_id}",
                    "kind": "pending",
                    "text": f"证伪条件：{lb['falsification']}",
                    "source_rows": [],
                })

    # 2. 假设匹配：多规则逐条按 rule_id 直映 + 关键词兜底（同假设去重）
    seen_h: set[str] = set()
    if rules:
        for ridx, rule in enumerate(rules):
            hypothesis = _match_hypothesis(
                rule["rule_id"], rule["basis"], rule["rule_text"])
            if not hypothesis or hypothesis["id"] in seen_h:
                continue
            seen_h.add(hypothesis["id"])
            suffix = "" if len(rules) == 1 else f"-{ridx}"
            items.append({
                "id": f"h{clue_id}{suffix}",
                "kind": "pending",
                "text": f"待验证假设：{hypothesis['id']}"
                        f"（{hypothesis['description']}）",
                "source_rows": [],
            })
    else:
        hypothesis = _match_hypothesis(
            detail.get("rule_id", ""), legacy_basis,
            detail.get("rule_text") or "")
        # 非规则聚合线索（用间交叉等）：依据只是数据源名称列表，关键词匹配会把
        # "银行流水(过桥)"误判成 H4——假设链由规则驱动，聚合线索（含方案 B
        # 补了表级汇总行的）永不产出假设卡。
        if hypothesis and refs and detail.get("rule_id"):
            items.append({
                "id": f"h{clue_id}",
                "kind": "pending",
                "text": f"待验证假设：{hypothesis['id']}"
                        f"（{hypothesis['description']}）",
                "source_rows": [],
            })

    # 3. rule_text 留痕：多规则逐条（同文本去重）；审计可追溯，不展示长原文
    seen_rt: set[str] = set()
    for ridx, rule in enumerate(rules):
        rule_text = rule["rule_text"]
        if not rule_text or rule_text in seen_rt:
            continue
        seen_rt.add(rule_text)
        suffix = "" if len(rules) == 1 else f"-{ridx}"
        items.append({
            "id": f"r{clue_id}{suffix}",
            "kind": "pending",
            "text": f"规则判据：{rule_text[:60]}..."
                    if len(rule_text) > 60 else f"规则判据：{rule_text}",
            "source_rows": [],
        })
    if not rules:
        rule_text = detail.get("rule_text") or ""
        if rule_text:
            items.append({
                "id": f"r{clue_id}",
                "kind": "pending",
                "text": f"规则判据：{rule_text[:60]}..."
                        if len(rule_text) > 60 else f"规则判据：{rule_text}",
                "source_rows": [],
            })

    return items


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _extract_rules(detail: dict[str, Any],
                   raw_clue: dict[str, Any]) -> list[dict[str, str]]:
    """从线索产物解析命中规则列表（声明序）。

    - detail.rules（backfill 多规则回填 / 未来 merge 保留）：
      [{rule_id, 依据, rule_text}]
    - 单规则旧产物：detail.rule_id + detail.依据/rule_text（或 top-level 依据）
    - 都没有：[]（调用方走 legacy 依据单卡路径）
    """
    multi = detail.get("rules")
    if isinstance(multi, list) and multi:
        out: list[dict[str, str]] = []
        for r in multi:
            if not isinstance(r, dict):
                continue
            out.append({
                "rule_id": str(r.get("rule_id") or ""),
                "basis": str(r.get("依据") or r.get("basis") or ""),
                "rule_text": str(r.get("rule_text") or ""),
            })
        if out:
            return out
    rid = detail.get("rule_id")
    if rid:
        return [{
            "rule_id": str(rid),
            "basis": str(detail.get("依据") or raw_clue.get("依据") or ""),
            "rule_text": str(detail.get("rule_text") or ""),
        }]
    return []


def _lens_event_items(detail: dict[str, Any], clue_id: str,
                      limit: int = 30) -> tuple[list[dict], list[dict]]:
    """镜头线索的**事件明细** → 事实卡 + 可挂溯源行。

    为什么需要
    ----------
    镜头线索 `source_rows` 恒为 0（证据走 evidence_refs 语义层引用），
    而原事实栏只认 source_rows —— 结果**详情页三栏全空**：正兵点开一条
    "跨类型时间碰撞"线索，看不到任何一行支撑，只能去画布数节点。

    但镜头的 detail 里其实带着完整事件明细（timeline / events / bursts
    内嵌 events），每条带 date、role、brief（如"转出→财政局 18533 元"）。
    这才是人能读的事实，比干巴巴的 `obj_call#call_xxx` 引用有用得多。

    返回 (items, rows)：items 为 fact 卡，rows 为对应 SourceRef
    （推断栏需要挂 source_rows，否则前端按 FE-T-004 丢弃 + 计数）。
    """
    events: list[dict] = []
    for e in detail.get("timeline") or []:
        if isinstance(e, dict):
            events.append(e)
    for e in detail.get("events") or []:
        if isinstance(e, dict):
            events.append(e)
    for b in detail.get("bursts") or []:
        if isinstance(b, dict):
            for e in b.get("events") or []:
                if isinstance(e, dict):
                    events.append(e)

    # 同一事件可能在 timeline 与 bursts 中重复出现（节奏镜头），按 event_pk 去重
    seen: set[str] = set()
    uniq: list[dict] = []
    for e in events:
        key = str(e.get("event_pk") or "") or json.dumps(
            e, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)

    total = len(uniq)
    items: list[dict] = []
    rows: list[dict] = []
    for idx, e in enumerate(uniq[:limit]):
        parts = []
        d = e.get("date")
        if d:
            parts.append(str(d))
        if e.get("offset_days") is not None:
            parts.append(f"偏移 {e['offset_days']:+d} 天")
        if e.get("type"):
            parts.append(str(e["type"]))
        if e.get("role"):
            parts.append(str(e["role"]))
        brief = e.get("brief")
        text = " · ".join(parts)
        if brief:
            text = f"{text}｜{brief}" if text else str(brief)
        dataset = f"obj_{e['src_object']}" if e.get("src_object") else "语义层"
        pk = str(e.get("event_pk") or f"e{idx}")
        rows.append({"row_uri": f"{dataset}@local#row/{pk}",
                     "source": dataset})
        items.append({"id": f"f{clue_id}-ev{idx}", "kind": "fact",
                      "text": text or "（事件无摘要）",
                      "source_rows": [rows[-1]]})
    # 超出上限：如实声明，不静默截断（与画布规模保护同口径）
    if total > limit:
        items.append({"id": f"f{clue_id}-ev-more", "kind": "fact",
                      "text": f"…另有 {total - limit} 起事件未逐条展开"
                              f"（共 {total} 起，完整明细见画布）",
                      "source_rows": []})
    return items, rows


def _ref_items(refs_raw: list[dict], clue_id: str,
               limit: int = 30) -> tuple[list[dict], list[dict]]:
    """语义层证据引用（evidence_refs）→ 事实卡 + 可挂溯源行。

    事件明细缺失时的兜底：至少把「引用了哪些语义对象/聚合量」摆出来，
    而不是三栏全空。文本是引用本身（可定位），不编造行内容。
    """
    items: list[dict] = []
    rows: list[dict] = []
    total = len(refs_raw or [])
    for idx, r in enumerate((refs_raw or [])[:limit]):
        if not isinstance(r, dict):
            continue
        kind = str(r.get("kind") or "")
        if kind == "aggregate":
            text = f"聚合量 {r.get('metric')}：{r.get('value')}"
            dataset = "聚合"
            uri = f"aggregate@local#row/{r.get('metric')}-{r.get('value')}"
        else:
            ref = str(r.get("ref") or "")
            table, _, key = ref.partition("#")
            text = f"{kind}：{ref}"
            dataset = table or "语义层"
            uri = f"{dataset}@local#row/{key or idx}"
        rows.append({"row_uri": uri, "source": dataset})
        items.append({"id": f"f{clue_id}-ref{idx}", "kind": "fact",
                      "text": text, "source_rows": [rows[-1]]})
    if total > limit:
        items.append({"id": f"f{clue_id}-ref-more", "kind": "fact",
                      "text": f"…另有 {total - limit} 条证据引用未展开"
                              f"（共 {total} 条）",
                      "source_rows": []})
    return items, rows


def _fact_text(sr: dict[str, Any]) -> str:
    """把数据行转人类可读的事实文本。

    提取关键业务字段（跳过内部字段），用「字段: 值」拼接。
    表级汇总行（方案 B：用间交叉 COUNT 口径）显式标注粒度，
    不与行级证据混淆。
    """
    if isinstance(sr, dict) and sr.get("粒度") == "表级汇总":
        return (f"表级汇总｜{sr.get('数据源') or '未知数据源'}"
                f"（语义表 {sr.get('语义表') or '?'}，"
                f"{sr.get('行数', '?')} 行非空支撑）")
    _INTERNAL = {"row_uri", "knowledge_sources", "knowledge_version",
                 "matched_person", "source_row_id", "数据源"}
    # 链接投影行（如时间窗碰撞）字段名经 links.json field_labels 中文化，
    # 值做语义格式化（偏移天数带正负、整万金额带万元口径）。
    labels = display_labels_for_row(sr)
    parts = []
    for k, v in sr.items():
        if k in _INTERNAL:
            continue
        if v is None or v == "":
            continue
        v = format_field_value(k, v)
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False, default=str)
        parts.append(f"{labels.get(k, k)}: {v}")
    return " · ".join(parts) if parts else "（空行）"


def _make_source_ref(sr: dict[str, Any], idx: int,
                      clue_id: str, *, pack_id: str = "default",
                      base_dir=None) -> dict[str, str]:
    """把数据行转 SourceRef（含伪 row_uri）。

    当前 source_rows 是纯数据 dict 无 URI，用内容哈希生成稳定标识。
    哈希排除「数据源」图章：图章是展示用溯源标注，不改行身份——
    生成期/读侧补图章前后 URI 保持一致（画布节点不失效）。
    未来 BUILD 产 URI 后，如果 sr 含 row_uri 字段则直接用。
    """
    if isinstance(sr, dict) and sr.get("row_uri"):
        return {"row_uri": sr["row_uri"],
                "source": dataset_of_row(sr, pack_id=pack_id,
                                         base_dir=base_dir)}
    # 表级汇总行（方案 B）：URI 明示 #table/ 段 + COUNT 快照，区别于行级 #row/
    if isinstance(sr, dict) and sr.get("粒度") == "表级汇总" \
            and sr.get("语义表"):
        return {
            "row_uri": f"{sr['语义表']}@local#table/n{sr.get('行数', 'x')}",
            "source": dataset_of_row(sr, pack_id=pack_id, base_dir=base_dir),
        }
    # 伪 URI：用内容哈希生成稳定 rowid（排除「数据源」图章）
    content = json.dumps(
        {k: v for k, v in sr.items() if k != "数据源"},
        sort_keys=True, ensure_ascii=False, default=str)
    rowid = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
    dataset = dataset_of_row(sr, pack_id=pack_id, base_dir=base_dir)
    return {
        "row_uri": f"{dataset}@local#row/{rowid}",
        "source": dataset,
    }


def _match_hypothesis(rule_id: str, basis: str,
                      rule_text: str) -> dict | None:
    """按 rule_id / 关键词匹配假设（H1~H4）。

    hypotheses.py 的 MiaoSuan.FINDING_PATTERNS 是类属性，
    按关键词命中 findings 的「候选虚处+依据」文本即映射：
      - R1（整数现金存入）→ H1 收受财物
      - R3（单一对端通话高频）→ H3 密切关系
      - R5（利益关联）→ H2 行贿获中标
    """
    text = f"{rule_id} {basis} {rule_text}"

    # rule_id 直接映射
    rule_h_map = {
        "R1": "H1",
        "R3": "H3",
        "R5": "H2",
    }
    target_h = rule_h_map.get(rule_id)
    if not target_h:
        # 关键词匹配
        if any(kw in text for kw in ("整数", "现金", "存入")):
            target_h = "H1"
        elif any(kw in text for kw in ("频次", "突增", "通讯")):
            target_h = "H3"
        elif any(kw in text for kw in ("利益", "关联", "中标")):
            target_h = "H2"
        elif any(kw in text for kw in ("过桥", "第三方")):
            target_h = "H4"

    if not target_h:
        return None

    # 从 MiaoSuan.FINDING_PATTERNS 类属性查找
    for pattern in MiaoSuan.FINDING_PATTERNS:
        h = pattern.get("hypothesis")
        if h and h.id == target_h:
            return {"id": h.id, "description": h.description}
    return None
