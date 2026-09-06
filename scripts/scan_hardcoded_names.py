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


# ---- REQ-D-003 AC-2：枚举空间/代码表不得硬编码具体人名/地名 ----
# 实体类维度名（代码表只应含抽象类别/标准值域，出现实体维度即误写案件实体名）
ENTITY_DIM = re.compile(r"(主体|人物|当事人|姓名|人名|客户名|嫌疑人|被害人|证人)")
# 实体关系指称（具体人的别称/关系，非代码表值）
ENTITY_REF = re.compile(r"(配偶|之子|之女|绰号|外号|别名|妻弟|丈夫|妻子)")
# 历史 demo 硬编码实体名（清零兜底）
DEMO_NAMES = ("张卫国", "李志强", "A建材", "张卫国配偶")


def scan_enum_space(space) -> list[dict]:
    """扫描枚举空间/代码表，返回硬编码人名/地名命中 [{dim, value, reason}]。

    代码表（性别/币种/证件类型/案件类别…）与庙算侦查维度（行为/时间/金额/关系）
    只应含抽象类别值；出现实体维度（主体/人物/姓名）的具体值、实体关系指称
    （配偶/绰号…）或已知 demo 实体名，即把案件实体误写进代码表（REQ-D-003 AC-2）。
    """
    hits: list[dict] = []
    for dim, vals in (space or {}).items():
        dim_s = str(dim)
        is_entity_dim = bool(ENTITY_DIM.search(dim_s))
        for v in (vals or []):
            s = str(v)
            if s in DEMO_NAMES:
                hits.append({"dim": dim_s, "value": s, "reason": "历史 demo 硬编码实体名"})
            elif is_entity_dim:
                hits.append({"dim": dim_s, "value": s,
                             "reason": f"代码表出现实体维度「{dim_s}」的具体值（主体应从案件数据派生）"})
            elif ENTITY_REF.search(s):
                hits.append({"dim": dim_s, "value": s,
                             "reason": "含实体关系指称（配偶/绰号等），非代码表值"})
    return hits


def scan_pack_enum(pack_dir: Path) -> list[dict]:
    """REQ-D-003 AC-2：扫一个 pack 的 enum_space + 数据元代码表是否含人名/地名。"""
    hits: list[dict] = []
    esp = pack_dir / "enum_space.json"
    if esp.exists():
        data = json.loads(esp.read_text(encoding="utf-8"))
        hits += scan_enum_space(data.get("space", {}))
        hits += scan_enum_space(data.get("code_tables", {}))
    dep = pack_dir / "data_elements.json"
    if dep.exists():
        data = json.loads(dep.read_text(encoding="utf-8"))
        tbl: dict[str, list[str]] = {}
        for eid, spec in (data.get("elements") or {}).items():
            if not isinstance(spec, dict) or not spec.get("enum"):
                continue
            dim = str(spec.get("enum_space_dim") or spec.get("name") or eid)
            tbl.setdefault(dim, []).extend(str(v) for v in spec["enum"])
        hits += scan_enum_space(tbl)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-on-hit", action="store_true",
                    help="发现硬编码人名时退出码 1（CI 红线）")
    ap.add_argument("--functions", default=str(ROOT / "ontology" / "default" / "functions.json"))
    ap.add_argument("--pack", default=str(ROOT / "ontology" / "default"),
                    help="待扫枚举空间/代码表的 ontology 包目录（REQ-D-003）")
    args = ap.parse_args()

    hits = scan_functions_file(Path(args.functions))
    enum_hits = scan_pack_enum(Path(args.pack))
    if not hits and not enum_hits:
        print("✅ functions.json 未发现硬编码人名（业务词只走 case_knowledge.json）")
        print("✅ enum_space/数据元代码表未发现硬编码人名/地名（REQ-D-003）")
        return 0
    if hits:
        print(f"❌ 发现 {len(hits)} 处 functions.json 硬编码人名（REQ-024 红线）：")
        for h in hits:
            print(f"  - {h['function']}: LIKE '{h['literal']}'")
        print("→ 迁移到 ontology/<pack>/case_knowledge.json，改由 org_interest_links 等知识包参数化函数消费。")
    if enum_hits:
        print(f"❌ 发现 {len(enum_hits)} 处枚举空间/代码表硬编码实体名（REQ-D-003 AC-2）：")
        for h in enum_hits:
            print(f"  - [{h['dim']}] {h['value']}：{h['reason']}")
        print("→ 主体候选从案件数据（obj_person 等）派生；标准代码表只放抽象类别值。")
    return 1 if args.fail_on_hit else 0


if __name__ == "__main__":
    sys.exit(main())
