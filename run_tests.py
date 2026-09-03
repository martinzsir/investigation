"""
run_tests.py —— 统一测试入口

测试组（--only 可选）：
  mcp         MCP server 端到端（scripts.mcp_client_test）
  graph       图库层 Q2 过桥双轨（tests.test_graph）
  miaosuan    庙算假设引擎（tests.test_miaosuan）
  org         组织层级对齐（tests.test_org_alignment）
  review      人工确认工作台（tests.test_review_queue）
  disposal    处置状态机 + 审计链（test_disposal.py 脚本式）
  ontology    语义层 Object/Link/Action（tests.test_ontology）
  version     REQ-001 版本时钟/依赖图（tests.test_ontology_version）
  eventbus    REQ-006 事件总线（tests.test_event_bus）
  ingest      REQ-005 分区校验隔离（tests.test_ingest_validate）
  spec        Schema/规则/引用完整性（test_schemas/test_rule_schema/
              test_reference_integrity/test_audit_chain）
  planner     REQ-018 受影响范围计算（tests.test_rebuild_planner）
  gateway     REQ-002 语义层读网关（tests.test_gateway）
  guard       REQ-003 直查拦截 + 静态扫描（tests.test_store_guard）
  features    REQ-015 L1 特征落盘（tests.test_features）
  incremental REQ-004 语义层增量重建（tests.test_incremental_semantic）
  audit       REQ-003 直查静态扫描（scripts/audit_straight_sql.py）
  e2e         端到端集成（tests.test_run_all）

用法：
    python run_tests.py              # 跑全部
    python run_tests.py --fast       # 跳过端到端（最快反馈）
    python run_tests.py --only org   # 只跑指定组
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

GROUPS = {
    "mcp":         ("MCP Server 端到端", [sys.executable, "-m", "scripts.mcp_client_test"]),
    "graph":       ("图库层 Q2 过桥双轨", [sys.executable, "-m", "unittest", "tests.test_graph"]),
    "miaosuan":    ("庙算假设引擎 三层机制", [sys.executable, "-m", "unittest", "tests.test_miaosuan"]),
    "org":         ("组织层级对齐", [sys.executable, "-m", "unittest", "tests.test_org_alignment"]),
    "review":      ("人工确认工作台", [sys.executable, "-m", "unittest", "tests.test_review_queue"]),
    "disposal":    ("处置状态机+审计链", [sys.executable, "test_disposal.py"]),
    "ontology":    ("语义层 Object/Link/Action", [sys.executable, "-m", "unittest", "tests.test_ontology"]),
    "version":     ("REQ-001 版本时钟/依赖图", [sys.executable, "-m", "unittest", "tests.test_ontology_version"]),
    "eventbus":    ("REQ-006 事件总线", [sys.executable, "-m", "unittest", "tests.test_event_bus"]),
    "ingest":      ("REQ-005 分区校验隔离", [sys.executable, "-m", "unittest", "tests.test_ingest_validate"]),
    "spec":        ("Schema/规则/引用完整性", [sys.executable, "-m", "unittest",
                                                 "tests.test_schemas", "tests.test_rule_schema",
                                                 "tests.test_reference_integrity", "tests.test_audit_chain"]),
    "planner":     ("REQ-018 受影响范围计算", [sys.executable, "-m", "unittest", "tests.test_rebuild_planner"]),
    "gateway":     ("REQ-002 语义层读网关", [sys.executable, "-m", "unittest", "tests.test_gateway"]),
    "guard":       ("REQ-003 直查拦截+静态扫描", [sys.executable, "-m", "unittest", "tests.test_store_guard"]),
    "features":    ("REQ-015 L1 特征落盘", [sys.executable, "-m", "unittest", "tests.test_features"]),
    "incremental": ("REQ-004 语义层增量重建", [sys.executable, "-m", "unittest", "tests.test_incremental_semantic"]),
    "audit":       ("REQ-003 直查静态扫描", [sys.executable, "scripts/audit_straight_sql.py", "--fail-on-violation"]),
    "e2e":         ("端到端集成", [sys.executable, "-m", "unittest", "tests.test_run_all"]),
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
