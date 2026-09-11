# -*- coding: utf-8 -*-
"""合并 8 章为 final.md，生成目录与质量报告。"""
import json
import re
from pathlib import Path

BASE = Path(r"D:\dev\inves_duckdb\projects\sunwu-tech-proposal")

TITLES = [
    "项目定位与设计目标",
    "总体架构设计",
    "语义层：Ontology 编译内核",
    "数据接入与治理",
    "规则引擎与 Function 计算层",
    "线索生命周期、处置与写路径",
    "安全、权限与可观测性",
    "质量保障、部署与演进路线",
]

CJK = re.compile(r"[\u4e00-\u9fff]")
H1 = re.compile(r"^#\s+.*$", re.M)


def cjk_count(text: str) -> int:
    return len(CJK.findall(text))


chapters = []
for i, title in enumerate(TITLES, start=1):
    path = BASE / f"chapter-{i:02d}.md"
    raw = path.read_text(encoding="utf-8")
    body = H1.sub("", raw, count=1).strip()
    chapters.append((i, title, body))

# ---------- 组装 final.md ----------
parts = []
parts.append("# 孙武侦查官 · 确定性侦查推演内核\n## 技术方案书（内部技术设计文档）\n")

total = sum(cjk_count(b) for _, _, b in chapters)

parts.append(
    f"> 本文档基于 `D:\\dev\\inves_duckdb` 仓库源码逐模块核对撰写，"
    f"共 8 章、约 {total:,} 字。\n>\n"
    "> 事实来源：源码本身（未引用既有 md/txt 文档结论）。"
    "凡与既有文档冲突之处，一律以代码为准并在第 8 章记录。\n>\n"
    "> 生成日期：2026-09-11\n\n---\n"
)

parts.append("## 目录\n")
for i, title, body in chapters:
    words = cjk_count(body)
    target = [4500, 6000, 6500, 5000, 6000, 5000, 5500, 4500][i - 1]
    parts.append(f"- 第 {i} 章　{title}　（{words:,} 字 / 目标 {target:,} 字）")
parts.append("\n---\n")

for i, title, body in chapters:
    parts.append(f"# 第 {i} 章　{title}\n\n{body}\n\n---\n")

final = "\n".join(parts)
(BASE / "final.md").write_text(final, encoding="utf-8")

# ---------- 质量报告 ----------
report = {
    "项目": "孙武侦查官技术方案书（内部技术设计文档）",
    "章节数": len(chapters),
    "总字数（中文字符）": total,
    "目标字数": 43000,
    "达标率": f"{total / 43000 * 100:.1f}%",
    "各章字数": {f"chapter-{i:02d}": cjk_count(b) for i, _, b in chapters},
    "检查项": {
        "章节齐全": len(chapters) == 8,
        "总字数达标(>=90%)": total >= 43000 * 0.9,
        "无####及以下标题": "####" not in final,
        "无TODO标记": "TODO" not in final and "待补充" not in final,
    },
}

(BASE / "quality-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)

print(json.dumps(report, ensure_ascii=False, indent=2))
