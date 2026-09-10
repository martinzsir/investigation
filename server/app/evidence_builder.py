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
    rule_id = detail.get("rule_id", "")
    basis = detail.get("依据") or raw_clue.get("依据") or ""
    rule_text = detail.get("rule_text") or ""
    is_degraded = raw_clue.get("is_degraded", False)

    items: list[dict[str, Any]] = []

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

    # ---- 推断栏（inference）：依据文本 + 全部 source_rows ----
    if basis:
        refs = [_make_source_ref(sr, idx, clue_id)
                for idx, sr in enumerate(source_rows)
                if isinstance(sr, dict)]
        items.append({
            "id": f"i{clue_id}",
            "kind": "inference",
            "text": basis,
            "source_rows": refs if refs else None,
            # 无 source_rows → 前端 partitionEvidence 丢弃 + droppedInferences++
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

    # 2. 假设匹配（rule_id 关联的假设）
    hypothesis = _match_hypothesis(rule_id, basis, rule_text)
    if hypothesis:
        items.append({
            "id": f"h{clue_id}",
            "kind": "pending",
            "text": f"待验证假设：{hypothesis['id']}"
                    f"（{hypothesis['description']}）",
            "source_rows": [],
        })

    # 3. rule_text 留痕（审计可追溯，不展示原文——太长）
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
def _fact_text(sr: dict[str, Any]) -> str:
    """把数据行转人类可读的事实文本。

    提取关键业务字段（跳过内部字段），用「字段: 值」拼接。
    """
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
