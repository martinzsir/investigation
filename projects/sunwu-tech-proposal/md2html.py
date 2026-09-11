# -*- coding: utf-8 -*-
"""final.md -> 印刷级 HTML（供 Chrome headless 打印为 PDF）。

零第三方依赖的极简 Markdown 解析器，只覆盖本文档实际用到的语法：
h1-h3 / 段落 / 无序列表 / 表格 / 引用块 / 分隔线 / 行内 code 与加粗。
"""
import html
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "final.md"
DST = BASE / "final.html"

TITLE = "孙武侦查官 · 确定性侦查推演内核"
SUBTITLE = "技术方案书（内部技术设计文档）"


# ---------------------------------------------------------------- inline
def inline(text: str) -> str:
    """行内格式化：先保护 code，再处理加粗，最后转义。"""
    slots = []

    def stash(rendered: str) -> str:
        slots.append(rendered)
        return "\x00%d\x00" % (len(slots) - 1)

    # 行内代码
    def code(m):
        return stash("<code>%s</code>" % html.escape(m.group(1)))

    text = re.sub(r"`([^`]+)`", code, text)

    # 加粗
    def bold(m):
        return stash("<strong>%s</strong>" % html.escape(m.group(1)))

    text = re.sub(r"\*\*(.+?)\*\*", bold, text)

    text = html.escape(text)

    # 还原占位
    for i, rendered in enumerate(slots):
        text = text.replace("\x00%d\x00" % i, rendered)
    return text


# ---------------------------------------------------------------- block
def convert(md: str) -> str:
    lines = md.split("\n")
    out = []
    i = 0
    n = len(lines)
    para = []

    def flush_para():
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para).strip()))
            para.clear()

    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        # 空行
        if not line.strip():
            flush_para()
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"\s*(-{3,}|\*{3,})\s*", line):
            flush_para()
            out.append("<hr/>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            lvl = min(len(m.group(1)), 3)
            out.append('<h%d>%s</h%d>' % (lvl, inline(m.group(2).strip()), lvl))
            i += 1
            continue

        # 表格
        if line.lstrip().startswith("|") and i + 1 < n and re.fullmatch(
                r"\s*\|[\s:|-]+\|?\s*", lines[i + 1]):
            flush_para()
            rows = []
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                rows.append(lines[j].strip())
                j += 1

            def cells(r):
                r = r.strip().strip("|")
                return [c.strip() for c in r.split("|")]

            head = cells(rows[0])
            body = [cells(r) for r in rows[2:]]
            t = ['<table>', '<thead><tr>']
            t += ["<th>%s</th>" % inline(c) for c in head]
            t.append('</tr></thead><tbody>')
            for r in body:
                t.append("<tr>" + "".join(
                    "<td>%s</td>" % inline(c) for c in r) + "</tr>")
            t.append('</tbody></table>')
            out.append("".join(t))
            i = j
            continue

        # 引用块
        if line.lstrip().startswith(">"):
            flush_para()
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            # 去掉引用块内的空行分隔，合并为段落
            chunk = [b for b in buf if b.strip()]
            out.append("<blockquote>" + "".join(
                "<p>%s</p>" % inline(b.strip()) for b in chunk) + "</blockquote>")
            continue

        # 无序列表
        if re.match(r"^\s*[-*+]\s+", line):
            flush_para()
            items = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip())
                i += 1
            out.append("<ul>" + "".join(
                "<li>%s</li>" % inline(it) for it in items) + "</ul>")
            continue

        # 普通段落行
        para.append(line.strip())
        i += 1

    flush_para()
    return "\n".join(out)


CSS = """
@page { size: A4; margin: 22mm 20mm 18mm 20mm; }

* { box-sizing: border-box; }
html { font-size: 10.5pt; }

body {
  margin: 0;
  color: #1f2328;
  background: #fff;
  font-family: "Source Han Serif SC", "Noto Serif CJK SC", "Songti SC",
               "SimSun", "STSong", Georgia, serif;
  line-height: 1.8;
  -webkit-font-smoothing: antialiased;
}

/* ---------- 封面 ---------- */
.cover {
  page-break-after: always;
  min-height: 232mm;
  padding: 34mm 8mm 0 6mm;
  background: linear-gradient(160deg, #fbfcfe 0%, #eef2f8 100%);
  border-top: 3px solid #1a56db;
}
.cover .kicker {
  font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 10pt;
  letter-spacing: 0.32em;
  color: #6b7785;
  margin-bottom: 14mm;
}
.cover h1 {
  font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 30pt;
  font-weight: 700;
  line-height: 1.35;
  margin: 0 0 8mm 0;
  padding: 0;
  border: none;
  color: #12263f;
  page-break-before: auto;
}
.cover .sub {
  font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 15pt;
  color: #40566e;
  margin-bottom: 22mm;
}
.cover .rule {
  width: 46mm; height: 3px; background: #1a56db; margin-bottom: 14mm;
}
.cover dl { margin: 0; font-size: 10.5pt; color: #4a5568; }
.cover dl div { margin-bottom: 3.2mm; }
.cover dt {
  display: inline-block; width: 26mm; color: #8492a6;
  font-family: "Microsoft YaHei", sans-serif;
}
.cover dd { display: inline; margin: 0; }
.cover .foot {
  margin-top: 16mm;
  font-size: 9pt; color: #98a4b3; line-height: 1.9;
  font-family: "Microsoft YaHei", sans-serif;
}

/* ---------- 正文排版 ---------- */
h1, h2, h3 {
  font-family: "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", sans-serif;
  color: #12263f;
  page-break-after: avoid;
  break-after: avoid;
}
h1 {
  font-size: 19pt;
  margin: 0 0 7mm 0;
  padding-bottom: 3.5mm;
  border-bottom: 1.6pt solid #1a56db;
  page-break-before: always;
  break-before: page;
}
h1:first-of-type { page-break-before: auto; break-before: auto; }
h2 {
  font-size: 14pt;
  margin: 9mm 0 4mm 0;
  padding-left: 3mm;
  border-left: 3.5pt solid #1a56db;
}
h3 {
  font-size: 11.5pt;
  margin: 7mm 0 3mm 0;
  color: #2c3e56;
}

p { margin: 0 0 3.4mm 0; text-align: justify; }

ul { margin: 0 0 4mm 0; padding-left: 7mm; }
li { margin-bottom: 1.6mm; }

blockquote {
  margin: 0 0 5mm 0;
  padding: 3.5mm 5mm;
  background: #f6f8fb;
  border-left: 3pt solid #b9c6d6;
  color: #44546a;
}
blockquote p { margin: 0 0 2mm 0; }
blockquote p:last-child { margin-bottom: 0; }

table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 5mm 0;
  font-size: 9.5pt;
  page-break-inside: avoid;
}
th, td {
  border: 0.5pt solid #d4dae2;
  padding: 2.2mm 3mm;
  text-align: left;
  vertical-align: top;
}
th { background: #eef2f7; font-weight: 600; }
tbody tr:nth-child(even) td { background: #fafbfd; }

code {
  font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
  font-size: 9pt;
  background: #f1f3f7;
  border: 0.5pt solid #e2e6ec;
  border-radius: 2px;
  padding: 0.3mm 1.2mm;
  color: #b3286b;
}
strong { font-weight: 700; color: #0f2035; }

hr { border: none; border-top: 0.5pt solid #dfe4ea; margin: 7mm 0; }

a { color: #1a56db; text-decoration: none; }

/* 目录页不分页断开 */
h2 + ul { page-break-inside: auto; }
"""

COVER = """<section class="cover">
  <div class="kicker">SUNWU INVESTIGATION KERNEL</div>
  <h1>{title}</h1>
  <div class="rule"></div>
  <div class="sub">{subtitle}</div>
  <dl>
    <div><dt>文档性质</dt><dd>内部技术设计文档（给研发 / 架构 / 代码审计）</dd></div>
    <div><dt>依据版本</dt><dd>D:\\dev\\inves_duckdb 仓库源码逐模块核对</dd></div>
    <div><dt>篇幅</dt><dd>8 章 · 约 42,074 字</dd></div>
    <div><dt>生成日期</dt><dd>2026-09-11</dd></div>
    <div><dt>技术栈</dt><dd>Python 3.12+ · DuckDB · LadybugDB（可选）· FastAPI · Vue 3</dd></div>
  </dl>
  <div class="foot">
    本文档全部结论以源码为唯一事实来源，未引用既有 md / txt 文档结论。<br/>
    凡与既有文档冲突之处，一律以代码为准并在第 8 章记录。
  </div>
</section>"""


def main():
    md = SRC.read_text(encoding="utf-8")
    body = convert(md)
    cover = COVER.format(title=html.escape(TITLE), subtitle=html.escape(SUBTITLE))
    doc = (
        '<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8"/>'
        "<title>%s</title><style>%s</style></head><body>\n%s\n%s\n</body></html>"
        % (html.escape(TITLE), CSS, cover, body)
    )
    DST.write_text(doc, encoding="utf-8")
    print("OK ->", DST)
    print("chars:", len(doc))


if __name__ == "__main__":
    main()
