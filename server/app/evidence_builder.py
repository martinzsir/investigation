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

    refs = [_make_source_ref(sr, idx, clue_id)
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
            "source_rows": [_make_source_ref(sr, idx, clue_id)],
        })

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
                 "matched_person", "source_row_id"}
    parts = []
    for k, v in sr.items():
        if k in _INTERNAL:
            continue
        if v is None or v == "":
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False, default=str)
        parts.append(f"{k}: {v}")
    return " · ".join(parts) if parts else "（空行）"


def _make_source_ref(sr: dict[str, Any], idx: int,
                      clue_id: str) -> dict[str, str]:
    """把数据行转 SourceRef（含伪 row_uri）。

    当前 source_rows 是纯数据 dict 无 URI，用内容哈希生成稳定标识。
    未来 BUILD 产 URI 后，如果 sr 含 row_uri 字段则直接用。
    """
    if isinstance(sr, dict) and sr.get("row_uri"):
        return {"row_uri": sr["row_uri"], "source": _dataset_of(sr)}
    # 表级汇总行（方案 B）：URI 明示 #table/ 段 + COUNT 快照，区别于行级 #row/
    if isinstance(sr, dict) and sr.get("粒度") == "表级汇总" \
            and sr.get("语义表"):
        return {
            "row_uri": f"{sr['语义表']}@local#table/n{sr.get('行数', 'x')}",
            "source": _dataset_of(sr),
        }
    # 伪 URI：用内容哈希生成稳定 rowid
    content = json.dumps(sr, sort_keys=True, ensure_ascii=False, default=str)
    rowid = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
    dataset = _dataset_of(sr)
    return {
        "row_uri": f"{dataset}@local#row/{rowid}",
        "source": dataset,
    }


def _dataset_of(sr: dict[str, Any]) -> str:
    """推断行所属数据源（用于 SourceRef.source 展示）。"""
    if not isinstance(sr, dict):
        return "未知数据源"
    # 表级汇总行（方案 B）：行内自带数据源展示名
    if isinstance(sr.get("数据源"), str) and sr["数据源"].strip():
        return sr["数据源"].strip()
    # 有 knowledge_sources 直接用
    ks = sr.get("knowledge_sources")
    if isinstance(ks, list) and ks:
        return ks[0]
    # 按字段名启发式推断
    fields = set(sr.keys())
    if {"from_raw", "to_raw", "amount"} & fields:
        return "银行流水"
    if {"caller_raw", "callee_raw", "times"} & fields:
        return "通话记录"
    if {"person_raw", "location"} & fields:
        return "轨迹出行"
    if {"legal_rep", "relation"} & fields:
        return "工商信息"
    if {"content_raw", "reporter_raw"} & fields:
        return "举报材料"
    return "数据行"


def _match_hypothesis(rule_id: str, basis: str,
                      rule_text: str) -> dict | None:
    """按 rule_id / 关键词匹配假设（H1~H4）。

    hypotheses.py 的 MiaoSuan.FINDING_PATTERNS 是类属性，
    按关键词命中 findings 的「候选虚处+依据」文本即映射：
      - R1（整数现金存入）→ H1 收受财物
      - R3（通话频次突增）→ H3 密切关系
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
