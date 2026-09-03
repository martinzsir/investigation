"""
scripts/scan_hardcoded_names.py
REQ-024 AC1：扫描 functions.json，SQL 模板中不得硬编码人名（LIKE '%中文%' 字面量）。

人名/业务词只允许出现在案件知识包（ontology/<pack>/case_knowledge.json）。
用法：
    python -m scripts.scan_hardcoded_names            # 扫描，发现命中则退出码 1
    python -m scripts.scan_hardcoded_names --fail-on-hit
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# LIKE '...' 字面量（含中文即视为硬编码业务词）
LIKE_LITERAL = re.compile(r"LIKE\s+'([^']*)'", re.IGNORECASE)
CJK = re.compile(r"[一-鿿]")


def scan_functions_file(path: Path) -> list[dict]:
    """返回命中清单：[{function, literal}]。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    hits = []
    for f in data.get("functions", []):
        sql = f.get("sql") or ""
        for m in LIKE_LITERAL.finditer(sql):
            literal = m.group(1)
            stripped = literal.strip("%")
            if stripped and CJK.search(stripped):
                hits.append({"function": f.get("name"), "literal": literal})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-on-hit", action="store_true",
                    help="发现硬编码人名时退出码 1（CI 红线）")
    ap.add_argument("--functions", default=str(ROOT / "ontology" / "default" / "functions.json"))
    args = ap.parse_args()

    hits = scan_functions_file(Path(args.functions))
    if not hits:
        print("✅ functions.json 未发现硬编码人名（业务词只走 case_knowledge.json）")
        return 0
    print(f"❌ 发现 {len(hits)} 处硬编码人名（REQ-024 红线）：")
    for h in hits:
        print(f"  - {h['function']}: LIKE '{h['literal']}'")
    print("→ 迁移到 ontology/<pack>/case_knowledge.json，改由 org_interest_links 等知识包参数化函数消费。")
    return 1 if args.fail_on_hit else 0


if __name__ == "__main__":
    sys.exit(main())
