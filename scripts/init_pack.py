"""
scripts/init_pack.py
REQ-044 AC2: 从模板初始化新案件包。

用法：
    python -m scripts.init_pack <新包名> [--from <模板包>]
    python -m scripts.init_pack case_2024_001 --from default
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.pack import PackManager


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从模板初始化新案件包（REQ-044 AC2）")
    parser.add_argument("pack_name", help="新案件包名")
    parser.add_argument("--from", dest="from_pack", default="default",
                        help="模板包名（默认 default）")
    parser.add_argument("--list", action="store_true",
                        help="列出所有案件包")
    args = parser.parse_args()

    pm = PackManager()

    if args.list:
        packs = pm.list_packs()
        print("已注册案件包：")
        for p in packs:
            print(f"  - {p}")
        return 0

    try:
        pm.init_pack(args.pack_name, from_pack=args.from_pack)
        print(f"案件包已创建：{args.pack_name}（模板：{args.from_pack}）")
        print(f"路径：ontology/{args.pack_name}/")
        print("下一步：编辑 bindings.json 填入数据源，然后运行：")
        print(f"  python -m scripts.build_ontology --pack {args.pack_name}")
        return 0
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
