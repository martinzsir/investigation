"""
server/app/canvas_citation_guard.py
RC-302 引用强制校验（防幻觉硬门槛）。

LLM 输出按句切分，事实句必须挂有效引用（节点 ref 或 row_uri，
存在于当刻文档/语义层）；无法挂引用的句子进入"待核实"段，
假引用被剔除并记 warning。软校验：不中断生成，无据内容强制归位。

引用标记约定（前后端共享）：[cite:<ref>]
  - ref 为画布节点 ref（rule_id / obj:pk / item_id ...）或 row_uri；
  - LLM 输出中事实句用此标记锚定证据；
  - 解析不到的引用（假引用）从句子中剔除并记 warning；
  - 无引用或引用全假的句子 → pending（待核实/模型推测）。

纯函数、不开库、不读 Parquet、无 LLM 依赖、可离线单测。
"""
from __future__ import annotations

import re
from typing import Any

# 引用标记：[cite:ref_value]，ref 不含方括号
_CITE_PATTERN = re.compile(r"\[cite:([^\[\]]+)\]")

# 句子切分：中文句号/问号/感叹号/分号 + 换行/段落
# 保留分隔符在句尾（split 后拼接时还原）
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])\s*")


def _split_sentences(text: str) -> list[str]:
    """按句切分，过滤空串，保留原句标点。"""
    parts = _SENTENCE_SPLIT.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def _extract_cites(sentence: str,
                   pat: re.Pattern = _CITE_PATTERN) -> list[str]:
    """提取句子中的所有引用 ref。"""
    return [m.group(1).strip() for m in pat.finditer(sentence)]


def _strip_cites(sentence: str, pat: re.Pattern = _CITE_PATTERN) -> str:
    """剔除句子中的引用标记，返回干净文本。"""
    return pat.sub("", sentence).strip()


def validate_citations(
    text: str,
    valid_refs: set[str] | frozenset[str],
    *,
    cite_pattern: re.Pattern | None = None,
) -> dict[str, Any]:
    """校验 LLM 输出的引用有效性，按句分流事实/待核实。

    Args:
        text: LLM 原始输出文本
        valid_refs: 当刻文档中有效的引用集合（节点 ref ∪ row_uri）

    Returns:
        {
            "facts":     [{"sentence": str, "citations": [str]}],  # 有据句
            "pending":   [str],                                      # 无据句
            "warnings":  [str],                                      # 警告
            "citation_count": int,   # 有效引用总数
            "fact_count":     int,   # 有据句数
            "pending_count":  int,   # 无据句数
            "all_facts_cited": bool, # 事实段是否 100% 有引用（覆盖率）
        }
    """
    pat = cite_pattern or _CITE_PATTERN
    refs = set(valid_refs) if not isinstance(valid_refs, (set, frozenset)) \
        else valid_refs

    facts: list[dict[str, Any]] = []
    pending: list[str] = []
    warnings: list[str] = []
    total_valid_cites = 0

    sentences = _split_sentences(text or "")

    for sent in sentences:
        cites = _extract_cites(sent, pat)

        if not cites:
            # 无引用 → 待核实
            pending.append(sent)
            warnings.append(
                f"句子缺乏引用，已转入待核实：{sent[:40]}"
                f"{'…' if len(sent) > 40 else ''}")
            continue

        # 校验引用有效性
        valid_cites: list[str] = []
        for c in cites:
            if c in refs:
                valid_cites.append(c)
            else:
                warnings.append(f"假引用已剔除：[cite:{c}]（不在当刻文档中）")

        if valid_cites:
            clean = _strip_cites(sent, pat)
            facts.append({"sentence": clean, "citations": valid_cites})
            total_valid_cites += len(valid_cites)
        else:
            # 引用全假 → 待核实
            pending.append(sent)
            warnings.append(
                f"句子引用全部无效，已转入待核实：{sent[:40]}"
                f"{'…' if len(sent) > 40 else ''}")

    return {
        "facts": facts,
        "pending": pending,
        "warnings": warnings,
        "citation_count": total_valid_cites,
        "fact_count": len(facts),
        "pending_count": len(pending),
        "all_facts_cited": len(facts) > 0 and len(pending) == 0,
    }


def build_valid_refs_from_doc(doc: dict[str, Any]) -> set[str]:
    """从画布文档提取所有有效引用（节点 ref + row_uri 形态）。

    用于问答时构建当刻文档的引用白名单。
    """
    refs: set[str] = set()
    for node in doc.get("nodes", []) or []:
        ref = node.get("ref")
        if ref:
            refs.add(str(ref))
        # row_uri 形态的节点（source_row 类）也纳入
        if isinstance(ref, str) and ref.startswith("row:"):
            refs.add(ref)
    return refs
