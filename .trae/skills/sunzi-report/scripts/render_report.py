#!/usr/bin/env python3
"""渲染研判报告：十段契约 + 三类受众形态 + md/docx 导出。

关键约束：
  - 第二段（证据充分性）、第五段（关联核验）与第九段（数据源清单）
    **由 evidence.json 确定性渲染**，即便 sections.json 提供了同名段也忽略
    （防 LLM 改写数字与数据来源）；
  - 附录「引用索引」由渲染器从正文 [cite:ref] 标记提取生成（A/B/C 均保留），
    sections.citations 仅用于按 ref 补摘要；
  - 降级项非空时，在报告头部生成「降级声明」块，不静默；
  - C 类（移送）剔除推断与待核实段，AI 不做定性。

用法：
    python render_report.py --evidence evidence.json --sections sections.json \
        --type A --format md --out 研判报告.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

# 十段契约：key → 中文标题
SECTION_TITLES: list[tuple[str, str]] = [
    ("overview", "一、线索概况"),
    ("sufficiency", "二、证据充分性"),
    ("rules", "三、命中规则与判据"),
    ("facts", "四、事实与依据"),
    ("correlation", "五、关联核验"),
    ("inferences", "六、研判推断"),
    ("pending", "七、待核实事项"),
    ("evidence", "八、书证清单"),
    ("sources", "九、数据源清单"),
]

# 确定性段（只由 evidence 渲染，Agent 不得改写）
DETERMINISTIC = {"sufficiency", "correlation", "sources"}

# 各类型保留的段（空集 = 全部）
TYPE_KEEP: dict[str, set[str] | None] = {
    "A": None,  # 全量
    "B": {"overview", "sufficiency", "rules", "facts", "pending",
          "evidence", "sources"},  # 去推断、去关联核验细节
    "C": {"overview", "sufficiency", "rules", "facts",
          "evidence", "sources"},  # 只留已固证素材
}

TYPE_NAME = {"A": "研判报告", "B": "汇报报告", "C": "移送固证报告"}


def _coerce_section_body(value: Any) -> str:
    """把单个叙述段规范化为字符串（LLM 输出不可信，结构偏差不炸整篇）。

    契约要求叙述段是字符串，但模型偶发把多句写成 list / 单对象写成 dict：
    list[str] 按行拼接（自动补 "- " 列表前缀），其余类型 JSON 序列化兜底，
    任何情况都返回可直接渲染的 str。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, str):
                lines.append(item if item.startswith(("- ", "* "))
                             else f"- {item}")
            else:
                lines.append(f"- {json.dumps(item, ensure_ascii=False)}")
        return "\n".join(lines).strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value).strip()


def render_sufficiency(block: dict[str, Any]) -> str:
    cross = block.get("证据充分性", {})
    lines: list[str] = []
    level = cross.get("交叉等级")
    lines.append(f"**交叉等级**：{level or '未判定'}")
    lines.append("")
    lines.append(f"> 判定规则：{cross.get('等级规则', '单源=观察 / 双源=线索 / 三源以上=可立案依据候选')}")
    lines.append("")

    hit = cross.get("命中间类") or []
    lines.append(f"**已命中间类**：{'、'.join(hit) if hit else '（无）'}")

    gap = cross.get("覆盖缺口")
    if gap is None:
        lines.append("")
        lines.append("**覆盖缺口**：未知（内核未就绪，未能计算）")
        lines.append("")
        lines.append("> 缺口未知≠无缺口。本案五间覆盖情况未经内核复核，"
                     "不得据此认定证据链已闭合。")
    elif gap:
        lines.append("")
        lines.append(f"**覆盖缺口**：{'、'.join(gap)}")
        lines.append("")
        lines.append("> 缺口含义：上述间类在本案中尚无数据支撑，"
                     "相应方向的证据链未闭合（对抗痕迹未覆盖）。")
    else:
        lines.append("")
        lines.append("**覆盖缺口**：无（五间均已覆盖）")

    declared = cross.get("五间声明") or []
    if declared:
        lines.append("")
        lines.append("| 间类 | 权重 | 密级 | 数据源 |")
        lines.append("|------|------|------|--------|")
        for d in declared:
            lines.append(
                f"| {d.get('名称', '')} | {d.get('权重', '')} "
                f"| {d.get('密级', '')} | {'、'.join(d.get('数据源') or []) or '—'} |"
            )
    return "\n".join(lines)


def render_clue_refs(refs: dict[str, Any] | None) -> list[str]:
    """P7 线索证据引用确定性渲染：节点/边/时间窗/聚合量/书证。

    新镜头按契约产出 evidence_refs 即自动出现，零接线。
    refs=None（无线索产物）时给"未产出"提示而非空列表（防误读为无引用）。
    """
    lines: list[str] = []
    lines.append("**线索图谱/资金与书证引用**")
    lines.append("")
    if refs is None:
        lines.append("案件尚未产出线索产物（先运行 BUILD/RESCAN），无引用可列。")
        return lines

    nodes = refs.get("节点引用") or []
    lines.append(f"- 节点引用：{len(nodes)} 处")
    if nodes:
        for it in nodes[:30]:
            lines.append(
                f"    - {it.get('type', '')}#{it.get('key', '')}"
                f"（{it.get('线索标题', '')}）")

    edges = refs.get("边引用") or []
    lines.append(f"- 边引用：{len(edges)} 处")
    if edges:
        for it in edges[:30]:
            lines.append(
                f"    - lnk_{it.get('type', '')}#{it.get('edge_pk', '')}"
                f"（{it.get('线索标题', '')}）")

    windows = refs.get("时间窗") or []
    lines.append(f"- 时间窗引用：{len(windows)} 处")
    for it in windows[:20]:
        lines.append(
            f"    - lnk_{it.get('type', '')}#{it.get('edge_pk', '')}"
            f"：{it.get('time_from', '—')} ~ {it.get('time_to', '—')}"
            f"（{it.get('线索标题', '')}）")

    aggs = refs.get("聚合量") or []
    if aggs:
        lines.append(f"- 聚合量：{len(aggs)} 项")
        for it in aggs[:30]:
            lines.append(
                f"    - {it.get('metric', '')} = {it.get('value', '—')}"
                f"（{it.get('线索标题', '')}）")

    files = refs.get("书证引用") or []
    lines.append(f"- 书证引用：{len(files)} 处")
    for it in files[:30]:
        status = it.get("verify_status")
        lines.append(
            f"    - {it.get('file_uri', '')}"
            + (f"（核验状态：{status}）" if status else "")
            + f"（{it.get('线索标题', '')}）")

    lines.append("")
    lines.append("> 上述引用由 gather_evidence.py 从线索 evidence_refs "
                 "确定性聚合，任何研判镜头产出即自动上图进报告。")
    return lines


def render_image_evidence(block: dict[str, Any]) -> str:
    """P8 图像证据确定性小节：人验通过的图像核验记录（state.sqlite）。

    核验（人比对原件）后写入，此处自动入报告——文件/标题/等级/人验结论
    由确定性块产出，不依赖 LLM 转述；空列表返回空串（八段退回纯叙述，
    老 evidence 无此块同样不产生噪声）。
    """
    rows = block.get("图像证据") or []
    if not rows:
        return ""
    lines: list[str] = []
    lines.append(f"**图像证据（人验）**：{len(rows)} 条")
    lines.append("")
    lines.append("| 书证 | 文件 | 标题 | 等级 | 人验结论 | 核验人 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows[:30]:
        if not isinstance(r, dict):
            continue
        lines.append(
            f"| {r.get('image_evidence_id', '')} "
            f"| {Path(str(r.get('image_uri') or '')).name} "
            f"| {r.get('title', '')} "
            f"| {r.get('severity', '')} "
            f"| {r.get('verify_conclusion', '')} "
            f"| {r.get('verifier', '')} |")
    lines.append("")
    lines.append("> 图像证据由 gather_evidence.py 从 state.sqlite "
                 "image_evidence 确定性采集（人验通过后写入），"
                 "AI 分析发现随核验结论一并固证。")
    return "\n".join(lines)


def render_evidence(block: dict[str, Any], sections: dict) -> str:
    """八、书证清单：确定性图像证据小节在前 + LLM 叙述在后（可无叙述）。

    图像证据是确定性块，无论 sections 是否提供 evidence 都注入；
    两者皆空返回空串（由调用方落「本段无内容」占位）。
    """
    img = render_image_evidence(block)
    body = _coerce_section_body(sections.get("evidence"))
    if img and body:
        return img + "\n\n" + body
    return img or body


def render_correlation(block: dict[str, Any]) -> str:
    det = block
    corr = det.get("关联核验", {})
    lines: list[str] = []

    # P7：线索图谱/资金/书证引用（evidence_refs 自动装配）
    lines += render_clue_refs(det.get("线索证据引用"))
    lines.append("")

    paths = corr.get("过桥路径") or []
    lines.append(f"**过桥路径**：{'发现 ' + str(len(paths)) + ' 条' if paths else '未发现'}")
    if paths:
        lines.append("")
        for p in paths[:20]:
            # p 可能是 dict（to_dict 后）或字符串
            if isinstance(p, dict):
                src = p.get("source", "")
                bridge = p.get("bridge", "")
                dest = p.get("dest", "")
                amt_in = p.get("amount_in")
                amt_out = p.get("amount_out")
                line = f"- {src} → {bridge} → {dest}"
                if amt_in is not None or amt_out is not None:
                    line += f"（流入 {amt_in} / 流出 {amt_out}）"
                rows = p.get("source_rows") or []
                if rows:
                    line += f"；溯源：{'; '.join(rows)}"
                lines.append(line)
            else:
                lines.append(f"- {p}")
    lines.append("")
    lines.append("> 过桥路径由图库两跳识别（Cypher 多跳 + SQL 自连接双轨），"
                 "用于识别第三方过桥结构。")

    rules = corr.get("规则手册") or []
    lines.append("")
    lines.append(f"**适用规则手册**：{len(rules)} 条")
    if rules:
        lines.append("")
        lines.append("| 规则 | 名称 | 间类 |")
        lines.append("|------|------|------|")
        for r in rules[:50]:
            jian = r.get("间类") or []
            lines.append(
                f"| {r.get('id', '')} | {r.get('名称', '')} "
                f"| {'、'.join(jian) if isinstance(jian, list) else jian or '—'} |"
            )
    return "\n".join(lines)


def _fmt_dt(value: Any) -> str:
    """'2026-09-13T02:12:18' → '2026-09-13 02:12'；空值/异常形态 → '—'。"""
    if not value:
        return "—"
    return str(value).replace("T", " ")[:16]


def render_sources(block: dict[str, Any]) -> str:
    src = block.get("数据源清单", {})
    lines: list[str] = []

    # 数据底座：案件库版本 + 本体包版本
    db = src.get("案件库")
    version = src.get("数据版本")
    lines.append(f"- 案件库：{db or '未解析'}"
                 + (f"（数据版本 v{version}）" if version is not None else ""))
    pack = src.get("本体包") or "—"
    osv = src.get("本体版本")
    lines.append(f"- 本体包：{pack}"
                 + (f"（schema_version={osv}）" if osv is not None else ""))

    # 已导入数据源批次（ingest meta 登记口径）
    regs = src.get("登记数据源")
    lines.append("")
    if regs is None:
        # 老版本 evidence / 采集异常降级时块缺失：不得伪装成"无数据源"
        lines.append("**已导入数据源批次**：未知（采集端未提供登记）")
    elif not regs:
        lines.append("**已导入数据源批次**：（无已导入批次登记）")
    else:
        lines.append(f"**已导入数据源批次**：{len(regs)} 个")
        lines.append("")
        lines.append("| 数据文件 | 上传批次 | 目标数据集 | 行数 | 导入时间 |")
        lines.append("|---|---|---|---|---|")
        for r in regs:
            lines.append(
                f"| {r.get('filename', '')} | {r.get('upload_id', '')} "
                f"| {r.get('table_name') or '—'} | {r.get('rows', '')} "
                f"| {_fmt_dt(r.get('created_at'))} |"
            )
        staged = src.get("未导入批次计数") or 0
        if staged:
            lines.append("")
            lines.append(f"> 另有 {staged} 个批次已上传但未导入（staged），"
                         "未进入语义层，不计入本报告依据。")

    # 本线索画布引用数据源（per-clue，无 clue_id 时该列表为空）
    clue_srcs = src.get("本线索数据源") or []
    if clue_srcs:
        lines.append("")
        lines.append(f"**本线索画布引用数据源**：{len(clue_srcs)} 个")
        lines.append("")
        for s in clue_srcs:
            ds = s.get("dataset") or ""
            name = s.get("filename") or ""
            bid = s.get("upload_id") or ""
            n = s.get("溯源行数")
            line = f"- {ds}"
            if name:
                line += f"（{name}）"
            if bid:
                line += f"，批次 {bid}"
            if n is not None:
                line += f"，溯源行 {n} 条"
            lines.append(line)

    lines.append("")
    lines.append("> 数据源清单由 gather_evidence.py 从 ingest 登记与画布"
                 "节点确定性采集，不依赖 LLM 陈述。")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 附录：引用索引（契约可写主体＝渲染生成）
# ----------------------------------------------------------------------

# 正文引用标记 [cite:ref]：ref 不允许含方括号
_CITE_TOKEN = re.compile(r"\[cite:([^\[\]]+?)\]")


def _explicit_citations(sections: dict) -> list[dict[str, Any]]:
    """sections.citations（可选）规范化为 dict 列表；异常形态忽略。"""
    cites = sections.get("citations") or []
    if isinstance(cites, dict):
        cites = [cites]
    if not isinstance(cites, list):
        return []
    return [c for c in cites if isinstance(c, dict)]


def extract_cite_refs(sections: dict,
                      keep: set[str] | None = None) -> list[str]:
    """从叙述段正文按段落顺序提取 [cite:ref]，去重保序。

    确定性段（sufficiency/correlation/sources）不扫描，被 rtype 剔除的段
    （如 C 类的 inferences/pending）也不扫描。
    """
    refs: list[str] = []
    seen: set[str] = set()
    for key, _ in SECTION_TITLES:
        if key in DETERMINISTIC:
            continue
        if keep is not None and key not in keep:
            continue
        body = _coerce_section_body(sections.get(key))
        for m in _CITE_TOKEN.finditer(body):
            ref = m.group(1).strip()
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def build_citation_rows(sections: dict,
                        keep: set[str] | None = None) -> list[dict[str, Any]]:
    """渲染生成引用索引行：

      - 编号＝正文出现序（cite_id 从 1 起）；
      - 摘要取自 sections.citations 中按 ref 匹配的显式条目；
      - 显式提供但正文未引用的条目追加在末尾（兼容旧式纯显式 citations）。
    """
    refs = extract_cite_refs(sections, keep)
    explicit = _explicit_citations(sections)
    summary_by_ref = {
        str(c.get("ref")): c.get("summary", "")
        for c in explicit if c.get("ref") is not None
    }
    rows: list[dict[str, Any]] = [
        {"cite_id": i, "ref": ref, "summary": summary_by_ref.get(ref, "")}
        for i, ref in enumerate(refs, 1)
    ]
    known = set(refs)
    nxt = len(rows) + 1
    for c in explicit:
        ref = c.get("ref")
        if ref is None or str(ref) in known:
            continue
        rows.append({"cite_id": nxt, "ref": str(ref),
                     "summary": c.get("summary", "")})
        nxt += 1
    return rows


def build_markdown(evidence: dict, sections: dict, rtype: str) -> str:
    keep = TYPE_KEEP.get(rtype)
    degraded = evidence.get("降级") or []
    det = evidence.get("确定性块") or {}

    out: list[str] = []
    out.append(f"# {TYPE_NAME.get(rtype, '研判报告')}")
    out.append("")
    out.append(f"- 案件：{evidence.get('case_id', '')}")
    if evidence.get("clue_id"):
        out.append(f"- 线索：{evidence.get('clue_id')}")
    out.append(f"- 数据版本：v{evidence.get('data_version')}"
               if evidence.get("data_version") is not None
               else "- 数据版本：未解析")
    out.append(f"- 生成方式：{evidence.get('生成方式', 'deterministic')}")
    out.append("")

    if degraded:
        out.append("> ⚠ **降级声明**：以下能力在本案中不可用，"
                   "报告相应内容缺失或未经内核复核")
        out.append(">")
        for d in degraded:
            out.append(f"> - {d}")
        out.append("")

    if rtype == "C":
        out.append("> 本报告由 AI 整理已固证素材，**不含定性结论**。"
                   "定性权属于办案人（正兵）。")
        out.append("")

    for key, title in SECTION_TITLES:
        if keep is not None and key not in keep:
            continue
        if key == "sufficiency":
            body = render_sufficiency(det)
        elif key == "correlation":
            body = render_correlation(det)
        elif key == "sources":
            body = render_sources(det)
        elif key == "evidence":
            body = render_evidence(det, sections)
        else:
            body = _coerce_section_body(sections.get(key))
        out.append(f"## {title}")
        out.append("")
        out.append(body if body else "（本段无内容）")
        out.append("")

    # 附录：引用索引——渲染生成（契约：A/B/C 三类均保留），
    # 从正文 [cite:ref] 标记提取；无引用时显式空段，不静默消失。
    cite_rows = build_citation_rows(sections, keep)
    out.append("## 附录：引用索引")
    out.append("")
    if cite_rows:
        out.append("| 编号 | 引用 | 摘要 |")
        out.append("|------|------|------|")
        for c in cite_rows:
            out.append(
                f"| {c.get('cite_id', '')} | {c.get('ref', '')} "
                f"| {str(c.get('summary', ''))[:60]} |"
            )
    else:
        out.append("（本段无内容）")
    out.append("")

    return "\n".join(out)


def build_docx(md: str, out_path: str) -> None:
    try:
        from docx import Document
    except ImportError:
        raise SystemExit(
            "未安装 python-docx，无法导出 docx。\n"
            "安装：pip install python-docx\n"
            "或改用 --format md（无需额外依赖）。"
        )
    doc = Document()
    for line in md.split("\n"):
        s = line.rstrip()
        if not s:
            continue
        if s.startswith("# "):
            doc.add_heading(s[2:], level=0)
        elif s.startswith("## "):
            doc.add_heading(s[3:], level=1)
        elif s.startswith("| "):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            row = doc.add_table(rows=1, cols=len(cells))
            for i, c in enumerate(cells):
                row.rows[0].cells[i].text = c
        elif s.startswith("> "):
            doc.add_paragraph(s[2:], style="Intense Quote")
        elif s.startswith("- "):
            doc.add_paragraph(s[2:], style="List Bullet")
        else:
            doc.add_paragraph(s)
    doc.save(out_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染研判报告")
    ap.add_argument("--evidence", required=True, help="gather_evidence.py 产出")
    ap.add_argument("--sections", required=True, help="Agent 撰写的叙述段 JSON")
    ap.add_argument("--type", default="A", choices=["A", "B", "C"])
    ap.add_argument("--format", default="md", choices=["md", "docx"])
    ap.add_argument("--out", default="研判报告.md")
    args = ap.parse_args()

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    sections = json.loads(Path(args.sections).read_text(encoding="utf-8"))

    # 红线：确定性段由 evidence 渲染，忽略 sections 中的同名段
    override = [k for k in DETERMINISTIC if sections.get(k)]
    if override:
        print(f"提示：{override} 为确定性段，已忽略 sections.json 中的同名内容",
              file=__import__("sys").stderr)

    md = build_markdown(evidence, sections, args.type)
    if args.format == "docx":
        build_docx(md, args.out)
    else:
        Path(args.out).write_text(md, encoding="utf-8")
    print(f"已生成：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
