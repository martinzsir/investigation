"""
scripts/build_ontology.py
语义层构建 CLI：从 ontology/<pack>/*.json 声明显式编译 obj_*/lnk_* 语义表。

用法：
    python -m scripts.build_ontology                      # 构建 default 案件包并打印统计
    python -m scripts.build_ontology --pack default       # 指定案件包
    python -m scripts.build_ontology --actions            # 查看 Action 注册表
    python -m scripts.build_ontology --functions          # 查看 Function 目录（只读计算）
"""
from __future__ import annotations

import argparse

from core import Store
from core.ontology import build_ontology, actions_report
from core.ontology_loader import load_pack


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="构建语义层（Object/Link/Action/Function）")
    ap.add_argument("--pack", default="default", help="ontology 案件包名（ontology/<pack>/）")
    ap.add_argument("--actions", action="store_true", help="只打印 Action 注册表")
    ap.add_argument("--functions", action="store_true", help="只打印 Function 目录")
    args = ap.parse_args(argv)

    if args.actions:
        for a in actions_report(args.pack):
            req = f" 必填:{','.join(p['name'] for p in a['parameters'] if p['required'])}"
            role = f" 角色:{a['requires_role']}" if a["requires_role"] != "any" else ""
            term = " [终态]" if a["terminal"] else ""
            fx = f" 副作用:{','.join(a['side_effects'])}" if a["side_effects"] else ""
            print(f"  {a['name']:<8} → {a['target_status']}{req}{term}{role}{fx}  "
                  f"来源:{','.join(a['allowed_from'])}")
        return

    if args.functions:
        pack = load_pack(args.pack)
        for f in pack.functions.values():
            print(f"  {f.name:<32} [{f.output_type:<6}] {f.impl:<3} 输入:{','.join(f.inputs)}")
            if f.description:
                print(f"    └ {f.description}")
        return

    store = Store()
    stats = build_ontology(store.conn, pack=args.pack)
    pack = load_pack(args.pack)
    print(f"=== 语义层构建完成（案件包：{args.pack}）===")
    for name, n in stats["objects"].items():
        print(f"  obj_{name:<12} {n} 行")
    for name, n in stats["links"].items():
        print(f"  lnk_{name:<12} {n} 行")
    for s in stats["skipped"]:
        print(f"  ⚠ 跳过 {s}")
    print(f"  声明：{len(pack.objects)} 对象 / {len(pack.links)} 链接 / "
          f"{len(pack.actions)} 动作 / {len(pack.functions)} 函数")


if __name__ == "__main__":
    main()
