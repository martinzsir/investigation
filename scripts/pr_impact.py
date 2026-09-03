"""
scripts/pr_impact.py
PR 影响报告（REQ-019 AC4）：分析 diff，输出新增对象/改阈值/改 SQL/受影响规则。

用法：
  python scripts/pr_impact.py <base_ref>...<head_ref>
  python scripts/pr_impact.py main...HEAD
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git_diff(base_head: str) -> list[str]:
    """返回 diff 中变更的文件列表。"""
    r = subprocess.run(
        ["git", "diff", "--name-only", base_head],
        capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return []
    return [f.strip() for f in r.stdout.splitlines() if f.strip()]


def impact_report(base_head: str) -> str:
    """生成 markdown 格式的 PR 影响报告。"""
    files = _git_diff(base_head)
    changed = {"objects": [], "links": [], "bindings": [],
               "rules": [], "functions": [], "actions": []}
    for f in files:
        for kind in changed:
            if f"ontology/" in f and f"{kind}.json" in f:
                changed[kind].append(f)
    lines = ["## PR 影响报告", ""]
    for kind, fl in changed.items():
        if fl:
            lines.append(f"### {kind} 变更")
            lines.append(f"- {' | '.join(fl)}")
            if kind == "rules":
                lines.append("- 影响：检测判据/阈值变化，需跑全量规则回归")
            elif kind == "functions":
                lines.append("- 影响：Function SQL/参数变化，需验证所有引用该 Function 的规则")
            elif kind == "objects":
                lines.append("- 影响：对象类型变化，需重建语义层 + 全量回归")
            elif kind == "bindings":
                lines.append("- 影响：数据源/管道变化，需重建语义层 + 验证新数据源")
            lines.append("")
    if not any(changed.values()):
        lines.append("无 ontology 文件变更。")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法：python scripts/pr_impact.py <base>...<head>", file=sys.stderr)
        sys.exit(1)
    print(impact_report(sys.argv[1]))


if __name__ == "__main__":
    main()
