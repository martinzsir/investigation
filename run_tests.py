"""
run_tests.py —— 统一测试入口

一次跑完七组测试：
  1. MCP server 端到端（scripts.mcp_client_test）     —— 33 项
  2. 图库层（tests.test_graph）                       —— 12 项
  3. 庙算假设引擎（tests.test_miaosuan）              —— 18 项
     （数据驱动映射 / 人机协同接口 / 规则约束标记）
  4. 组织层级对齐（tests.test_org_alignment）        —— 11 项
  5. 人工确认工作台（tests.test_review_queue）       —— 10 项
  6. 处置状态机 + 审计链（test_disposal.py 脚本式）   —— 20+ 断言
  7. 端到端集成（tests.test_run_all）                —— 10 项

用法：
    python run_tests.py              # 跑全部
    python run_tests.py --fast       # 跳过端到端（最快反馈）
    python run_tests.py --only org   # 只跑指定组（mcp/miaosuan/graph/org/review/disposal/e2e）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

GROUPS = {
    "mcp":      ("MCP Server 端到端", [sys.executable, "-m", "scripts.mcp_client_test"]),
    "graph":    ("图库层 Q2 过桥双轨", [sys.executable, "-m", "unittest", "tests.test_graph"]),
    "miaosuan": ("庙算假设引擎 三层机制", [sys.executable, "-m", "unittest", "tests.test_miaosuan"]),
    "org":      ("组织层级对齐", [sys.executable, "-m", "unittest", "tests.test_org_alignment"]),
    "review":   ("人工确认工作台", [sys.executable, "-m", "unittest", "tests.test_review_queue"]),
    "disposal": ("处置状态机+审计链", [sys.executable, "test_disposal.py"]),
    "e2e":      ("端到端集成", [sys.executable, "-m", "unittest", "tests.test_run_all"]),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过端到端集成测试")
    ap.add_argument("--only", choices=list(GROUPS), help="只跑指定测试组")
    args = ap.parse_args()

    names = [args.only] if args.only else list(GROUPS)
    if args.fast:
        names = [n for n in names if n != "e2e"]

    failed: list[str] = []
    for name in names:
        title, cmd = GROUPS[name]
        print(f"\n{'=' * 60}\n>>> [{name}] {title}\n{'=' * 60}")
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tail = (r.stdout or "") + (r.stderr or "")
        # 只打印摘要行，避免刷屏
        for line in tail.splitlines():
            if line.startswith(("Ran ", "OK", "FAILED", "✅", "❌")) or "Error" in line:
                print("   ", line)
        if r.returncode != 0:
            failed.append(name)
            print("    --- 完整输出 ---")
            print(tail)

    print(f"\n{'=' * 60}")
    if failed:
        print(f"❌ 失败组：{failed}")
        return 1
    print(f"✅ 全部通过：{', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
