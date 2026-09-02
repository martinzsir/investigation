"""
导出操作台数据快照：以 DuckDB clue_disposal_status 表为处置状态真值源，
覆盖 lineage_clues.json 中的原始线索明细。输出 dashboard_data.json。
"""
import json, os
import duckdb

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "investigation.duckdb")
CLUES = os.path.join(BASE, "output", "lineage_clues.json")

con = duckdb.connect(DB)

# 1) 处置状态真值（来自 DuckDB 表）
status_rows = con.execute(
    "SELECT clue_id, status, note, operator, updated_at FROM clue_disposal_status ORDER BY updated_at DESC"
).fetchall()
status_map = {}
for cid, status, note, op, ts in status_rows:
    status_map[cid] = {
        "status": status,
        "note": note or "",
        "operator": op or "",
        "updated_at": (ts.isoformat() if hasattr(ts, "isoformat") else str(ts)),
    }

# 2) 线索明细（来自 lineage_clues.json）
with open(CLUES) as f:
    payload = json.load(f)

clues = []
for c in payload["clues"]:
    cid = c["clue_id"]
    st = status_map.get(cid, {"status": "待查", "note": "", "operator": "", "updated_at": ""})
    clues.append({
        "clue_id": cid,
        "skill_id": c.get("skill_id", ""),
        "title": c.get("title", ""),
        "detail": c.get("detail", ""),
        "assumption_chain": c.get("assumption_chain", []),
        "jian_types": c.get("jian_types", []),
        "source_rows": c.get("source_rows", []),
        "needs_human_review": c.get("needs_human_review", True),
        "定性_policy": c.get("定性_policy", ""),
        "status": st["status"],
        "note": st["note"],
        "operator": st["operator"],
        "updated_at": st["updated_at"],
    })

# 3) 统计
by_status = {}
for c in clues:
    by_status[c["status"]] = by_status.get(c["status"], 0) + 1

data = {
    "total_clues": len(clues),
    "by_status": by_status,
    "jian_coverage": payload.get("jian_coverage", {}),
    "cross_level": payload.get("cross_level", ""),
    "clues": clues,
}

out = os.path.join(BASE, "output", "dashboard_data.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(json.dumps({"file": out, "total": len(clues), "by_status": by_status}, ensure_ascii=False))
