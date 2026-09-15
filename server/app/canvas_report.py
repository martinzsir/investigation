"""
server/app/canvas_report.py
RC-304/305/306 研判报告生成器（纯函数 + 导出）。

流程：
  1. 从快照 doc 构建业务视图上下文（复用 canvas_chat.build_canvas_context）；
  2. 组装 8 段结构化报告 prompt，走 call_llm 闸门；
  3. LLM 输出过 citation_guard：有据句入事实段、无据句入待核实段、
     假引用剔除 + warning（复用 canvas_citation_guard.validate_citations）；
  4. 组装 sections_json / content_md / citations_json / warnings_json；
  5. 导出：render_markdown（纯文本）/ render_docx（python-docx）。

纪律：
  - 报告内容不可变（版本递增、无更新通道，state_store 保证）；
  - 报告生成时先冻结快照，事后修改画布不影响历史报告；
  - 事实段引用覆盖率 100%（无据句强制归入"待核实事项"段）；
  - 纯函数、不开库、不读 Parquet、无 LLM 依赖、可离线单测（fake_invoke）；
  - 导出走 python-docx（新依赖，WSL venv 国内镜像安装）。
"""
from __future__ import annotations

import json
from typing import Any

from server.app.canvas_citation_guard import (
    build_valid_refs_from_doc,
    validate_citations,
)
from server.app.canvas_chat import build_canvas_context

# 报告 8 段固定章节键
REPORT_SECTIONS = [
    "overview",    # 一、线索概况
    "rules",       # 二、命中规则与判据
    "facts",       # 三、事实与依据（逐句引用）
    "inferences",  # 四、研判推断
    "pending",     # 五、待核实事项
    "evidence",    # 六、书证清单
    "sources",     # 七、数据源清单
    "appendix",    # 附录：引用索引
]

SYSTEM_PROMPT = (
    "你是线索研判报告撰写助手。基于当前画布快照的业务视图与确定性证据，"
    "生成一份结构化研判报告。\n"
    "严格约束：\n"
    "1. 报告分十段，其中「二、证据充分性」和「五、关联核验」"
    "由确定性脚本产出，你不需要撰写这两段。\n"
    "你只负责撰写以下七段，每段用 ## 标题开头：\n"
    "   一、线索概况\n"
    "   三、命中规则与判据\n"
    "   四、事实与依据\n"
    "   六、研判推断\n"
    "   七、待核实事项\n"
    "   八、书证清单\n"
    "   九、数据源清单\n"
    "2. 「四、事实与依据」段中每条事实性陈述必须紧跟引用标记 [cite:<ref>]，"
    "ref 取自下方「可用引用」清单；不得编造引用。\n"
    "3. 无法从画布证据支撑的表述归入「七、待核实事项」段，不得伪装成事实。\n"
    "4. 「六、研判推断」段须区分「已有证据支持」与「分析推测」。\n"
    "5. 全文用中文，事实句简洁，避免推测性语气。\n"
    "6. 不输出任何状态变更指令或写操作建议。"
)


def build_report_prompt(
    doc: dict[str, Any],
    extra_request: str = "",
    evidence: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """从快照 doc 构建报告生成 prompt。

    Args:
        doc: 画布快照 doc。
        extra_request: 补充要求（透传）。
        evidence: gather_evidence 产出的确定性证据（可选）。
            传入时 LLM 可参考确定性块撰写叙述段。

    Returns:
        (system_prompt, user_prompt)
    """
    context = build_canvas_context(doc)
    user_payload = {
        "画布业务视图": context,
        "输出要求": (
            "按十段结构生成报告，你只写七段叙述段"
            "（确定性段已由脚本产出）；"
            "事实句必须带 [cite:<ref>]；无据内容归入待核实段。"
        ),
    }
    if evidence:
        user_payload["确定性证据"] = evidence
    if extra_request:
        user_payload["补充要求"] = extra_request
    return SYSTEM_PROMPT, json.dumps(
        user_payload, ensure_ascii=False, indent=2)


def parse_llm_to_sections(
    llm_raw_text: str,
    valid_refs: set[str] | frozenset[str],
) -> dict[str, Any]:
    """解析 LLM 输出为 sections dict（供 render_report 使用）+ 引用校验。

    与 parse_report_sections 的区别：
      - 返回的 sections 只含 LLM 撰写的叙述段（不含确定性段）；
      - 不组装 content_md（由 render_report.build_markdown 完成）；
      - citations 和 warnings 作为 sections 的附加键返回。

    Returns:
        {
            "sections": {overview/rules/facts/inferences/pending/evidence/sources},
            "citations": [{cite_id, ref, summary}],
            "warnings": [str],
        }
    """
    sections = _split_by_heading(llm_raw_text)

    # 对"事实与依据"段做引用校验
    facts_text = sections.get("facts", "")
    guarded = validate_citations(facts_text, valid_refs)

    # 重组事实段（有据句保留引用、无据句移入待核实段）
    if guarded["facts"]:
        fact_lines = []
        for f in guarded["facts"]:
            cites_str = "".join(
                f"[cite:{c}]" for c in f["citations"])
            fact_lines.append(f"{f['sentence']}{cites_str}")
        sections["facts"] = "\n".join(fact_lines)

    pending_extra = guarded["pending"]
    if pending_extra:
        existing_pending = sections.get("pending", "")
        extra_text = "\n".join(
            f"- {s}" for s in pending_extra)
        if existing_pending:
            sections["pending"] = (
                f"{existing_pending}\n\n"
                f"以下表述因缺乏引用已转入待核实：\n{extra_text}")
        else:
            sections["pending"] = (
                f"以下表述因缺乏引用已转入待核实：\n{extra_text}")

    # 组装 citations 索引
    citations: list[dict[str, str]] = []
    for i, f in enumerate(guarded["facts"], 1):
        for ref in f["citations"]:
            citations.append({
                "cite_id": i,
                "ref": ref,
                "summary": f["sentence"][:80],
            })

    # 确保叙述段键存在（空段兜底）
    for key in ("overview", "rules", "facts", "inferences",
                "pending", "evidence", "sources"):
        if key not in sections:
            sections[key] = ""

    return {
        "sections": sections,
        "citations": citations,
        "warnings": guarded["warnings"],
    }


def parse_report_sections(
    llm_raw_text: str,
    valid_refs: set[str] | frozenset[str],
) -> dict[str, Any]:
    """解析 LLM 输出为结构化报告段 + 校验引用。

    Returns:
        {
            "sections": {8段},
            "content_md": str,
            "citations": [{cite_id, ref, summary}],
            "warnings": [str],
        }
    """
    # 1. 按 ## 标题切分段落
    sections = _split_by_heading(llm_raw_text)

    # 2. 对"事实与依据"段做引用校验
    facts_text = sections.get("facts", "")
    guarded = validate_citations(facts_text, valid_refs)

    # 3. 重组事实段（有据句保留引用、无据句移入待核实段）
    if guarded["facts"]:
        fact_lines = []
        for f in guarded["facts"]:
            cites_str = "".join(
                f"[cite:{c}]" for c in f["citations"])
            fact_lines.append(f"{f['sentence']}{cites_str}")
        sections["facts"] = "\n".join(fact_lines)

    # 无据句追加到待核实段
    pending_extra = guarded["pending"]
    if pending_extra:
        existing_pending = sections.get("pending", "")
        extra_text = "\n".join(
            f"- {s}" for s in pending_extra)
        if existing_pending:
            sections["pending"] = (
                f"{existing_pending}\n\n"
                f"以下表述因缺乏引用已转入待核实：\n{extra_text}")
        else:
            sections["pending"] = (
                f"以下表述因缺乏引用已转入待核实：\n{extra_text}")

    # 4. 组装 content_md
    content_md = _assemble_markdown(sections)

    # 5. 组装 citations 索引
    citations: list[dict[str, str]] = []
    for i, f in enumerate(guarded["facts"], 1):
        for ref in f["citations"]:
            citations.append({
                "cite_id": i,
                "ref": ref,
                "summary": f["sentence"][:80],
            })

    # 6. 确保所有 8 段键存在（空段兜底）
    for key in REPORT_SECTIONS:
        if key not in sections:
            sections[key] = ""

    return {
        "sections": sections,
        "content_md": content_md,
        "citations": citations,
        "warnings": guarded["warnings"],
    }


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------

# 标题→段键映射（中文标题 → 英文段键）
_HEADING_MAP = {
    "线索概况": "overview",
    "证据充分性": "sufficiency",
    "命中规则与判据": "rules",
    "事实与依据": "facts",
    "关联核验": "correlation",
    "研判推断": "inferences",
    "待核实事项": "pending",
    "书证清单": "evidence",
    "数据源清单": "sources",
    "引用索引": "appendix",
}


def _split_by_heading(text: str) -> dict[str, str]:
    """按 ## 标题切分段落。"""
    sections: dict[str, str] = {}
    current_key = "overview"
    current_lines: list[str] = []

    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("##"):
            # 保存上一段
            if current_lines:
                sections[current_key] = "\n".join(
                    current_lines).strip()
            # 解析新段键：去掉 ## 前缀和中文序号前缀
            heading = stripped.lstrip("#").strip()
            # 去掉中文序号前缀（如"一、线索概况"→"线索概况"）
            # 前缀格式："序号、标题" 或 "附录：引用索引"
            import re as _re
            heading = _re.sub(
                r"^[一二三四五六七八九十]+、", "", heading)
            heading = heading.replace("附录：", "").replace("附录:", "")
            current_key = _HEADING_MAP.get(
                heading, heading.replace(" ", "_"))
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_key] = "\n".join(
            current_lines).strip()

    return sections


def _assemble_markdown(sections: dict[str, str]) -> str:
    """组装完整 Markdown 文本。"""
    titles = {
        "overview": "一、线索概况",
        "rules": "二、命中规则与判据",
        "facts": "三、事实与依据",
        "inferences": "四、研判推断",
        "pending": "五、待核实事项",
        "evidence": "六、书证清单",
        "sources": "七、数据源清单",
        "appendix": "附录：引用索引",
    }
    parts: list[str] = []
    for key in REPORT_SECTIONS:
        title = titles.get(key, key)
        content = sections.get(key, "")
        parts.append(f"## {title}\n")
        if content:
            parts.append(content)
        else:
            parts.append("（本段无内容）")
        parts.append("")
    return "\n".join(parts).strip()


# ======================================================================
# RC-306：导出
# ======================================================================

def render_markdown(sections: dict[str, str],
                    citations: list[dict] | None = None) -> str:
    """渲染完整 Markdown（含附录引用索引）。"""
    md = _assemble_markdown(sections)
    if citations:
        md += "\n\n## 附录：引用索引\n\n"
        md += "| 编号 | 引用 | 摘要 |\n|------|------|------|\n"
        for c in citations:
            md += f"| {c.get('cite_id', '')} | {c.get('ref', '')} | {c.get('summary', '')[:60]} |\n"
    return md


def render_docx(sections: dict[str, str],
                citations: list[dict] | None = None) -> bytes:
    """用 python-docx 生成 Word 文档。

    标题层级、事实句脚注式引用编号、附录表格。
    """
    from docx import Document
    from docx.shared import Pt
    import io

    doc = Document()

    # 文档标题
    doc.add_heading("研判报告", level=0)

    titles = {
        "overview": "一、线索概况",
        "rules": "二、命中规则与判据",
        "facts": "三、事实与依据",
        "inferences": "四、研判推断",
        "pending": "五、待核实事项",
        "evidence": "六、书证清单",
        "sources": "七、数据源清单",
        "appendix": "附录：引用索引",
    }

    for key in REPORT_SECTIONS:
        if key == "appendix":
            continue  # 附录单独处理
        title = titles.get(key, key)
        doc.add_heading(title, level=1)
        content = sections.get(key, "")
        if content:
            for para in content.split("\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
        else:
            doc.add_paragraph("（本段无内容）")

    # 附录：引用索引表格
    if citations:
        doc.add_heading("附录：引用索引", level=1)
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        hdr[0].text = "编号"
        hdr[1].text = "引用"
        hdr[2].text = "摘要"
        for c in citations:
            row = table.add_row().cells
            row[0].text = str(c.get("cite_id", ""))
            row[1].text = str(c.get("ref", ""))
            row[2].text = str(c.get("summary", "")[:80])

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
