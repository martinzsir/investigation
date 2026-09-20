"""导出血缘追溯报告：五间转化链 + Function 计算过程 + 线索级血缘。"""
import json
from pathlib import Path

from core import Store
from core.functions import FunctionExecutor
from core.pack_loader import discover
from core.rules import run_rules
from core.wujian import load_wujian

# 镜头包与五间词汇经 pack_loader 自枚举后才进注册表
discover()

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

d = json.load(open(OUT / "lineage_clues.json", encoding="utf-8"))
store = Store()
fx = FunctionExecutor(store, "default")
wj = load_wujian("default")

L = []


def w(s=""):
    L.append(s)


w("# 血缘追溯报告 · 五间转化与 Function 计算过程")
w()
w(f"- 产物：`output/lineage_clues.json`（total_clues={d['total_clues']}）")
w(f"- 案件级交叉等级：**{d['cross_level']}**")
w(f"- 处置分布：{json.dumps(d['by_status'], ensure_ascii=False)}")
w()
w("---")
w()

# ---------------- 1. 五间词汇与转化链 ----------------
w("## 一、五间完整转化链")
w()
w("转化路径：`源对象类型 → 间类 → 线索间类 → 独立源计数 → 交叉等级`")
w()
w("### 1.1 词表声明（`packs/wujian/jians.json`）")
w()
w("| 间类 | 源对象类型 | 权重 | 默认密级 |")
w("|---|---|---|---|")
if wj:
    for j in wj.jians:
        w(f"| {j.name} | {', '.join(j.source_object_types)} | {j.weight} | {j.default_clearance} |")
w()
w("升格映射（`min_independent_sources` 硬编码 1/2/3，名称可配）：")
w()
if wj:
    for lv in wj.cross_levels:
        w(f"- {lv.min_independent_sources} 源 → **{lv.name}**")
w()

w("### 1.2 线索 → 间类 落位结果")
w()
w("| 线索 | 标题 | 技能 | 间类 | 独立源数 | 交叉等级 | 计分 |")
w("|---|---|---|---|---|---|---|")
for c in d["clues"]:
    det = c["detail"]
    n = len(c.get("jian_types") or [])
    w(f"| `{c['clue_id']}` | {c['title']} | {c['skill_id']} | "
      f"{'/'.join(c.get('jian_types') or []) or '—'} | {n} | "
      f"{det.get('cross_level', '—')} | {det.get('priority_score', '—')} |")
w()

w("### 1.3 间类覆盖（`jian_coverage`）")
w()
w("```json")
w(json.dumps(d["jian_coverage"], ensure_ascii=False, indent=2))
w("```")
w()
w("> 案件级等级由**独立源数**决定，与计分解耦（`scoring.json` 红线）。")
w()
w("---")
w()

# ---------------- 2. 规则命中 ----------------
w("## 二、虚实扫描：规则命中与判据回溯")
w()
findings = run_rules(store, stage="xu_shi")
w(f"命中 {len(findings)} 条：")
w()
w("| 规则 | 候选虚处 | 维度 | 间类 | 假设 | 溯源行数 | 判据（rule_text 摘要） |")
w("|---|---|---|---|---|---|---|")
for f in findings:
    txt = (f.get("rule_text") or "")[:40] + "…"
    w(f"| {f['rule_id']} | {f['候选虚处']} | {f.get('dimension', '—')} | "
      f"{'/'.join(f.get('jian_types') or [])} | {f.get('assumption') or '—'} | "
      f"{len(f.get('source_rows') or [])} | {txt} |")
w()
w("---")
w()

# ---------------- 3. Function 计算过程 ----------------
w("## 三、Function（pack 计算）执行矩阵")
w()
w("| Function | impl | 输入语义表 | 实际参数 params_used | 结果行数 | 降级 |")
w("|---|---|---|---|---|---|")
for name in sorted(fx._specs()):
    spec = fx._specs()[name]
    try:
        r = fx.invoke(name, {})
        rows = r.get("rows")
        res = r.get("result")
        if rows is not None:
            cnt = len(rows)
        elif isinstance(res, dict) and "rows" in res:
            cnt = len(res.get("rows") or [])
        elif isinstance(res, dict) and res.get("hit"):
            cnt = 1
        else:
            cnt = 0
        pu = json.dumps(r.get("params_used", {}), ensure_ascii=False)
        deg = "⚠ 是" if r.get("degraded") else "否"
        w(f"| `{name}` | {spec.impl} | {', '.join(spec.inputs)} | `{pu}` | {cnt} | {deg} |")
    except Exception as e:
        w(f"| `{name}` | {spec.impl} | {', '.join(spec.inputs)} | — | — | 异常 {type(e).__name__} |")
w()
w("> SQL 轨：`sql` 原文在 `ontology/default/functions.json`，首词必须 SELECT/WITH；")
w("> py 轨：`impl_ref` 指向 `core/functions.py` 注册实现，经 `ReadOnlyStore` 只读访问。")
w()
w("---")
w()

# ---------------- 4. 单条线索完整血缘 ----------------
w("## 四、单条线索完整血缘链（样例）")
w()
# 按技能各取一条代表（clue_id 每次运行重新生成，不可硬编码）
_sample_ids: list[str] = []
for sid in ["xu_shi", "qi_zheng", "yong_jian"]:
    for c in d["clues"]:
        if c["skill_id"] == sid:
            _sample_ids.append(c["clue_id"])
            break
# 补一条间类最多的（交叉升格样例）
if d["clues"]:
    _sample_ids.append(max(d["clues"], key=lambda c: len(c.get("jian_types") or []))["clue_id"])

for cid in dict.fromkeys(_sample_ids):
    for c in d["clues"]:
        if c["clue_id"] != cid:
            continue
        det = c["detail"]
        w(f"### `{cid}` · {c['title']}")
        w()
        w(f"- 产出技能：`{c['skill_id']}`")
        w(f"- 假设链：`{c.get('assumption_chain') or '—'}`")
        w(f"- 间类：`{c.get('jian_types')}`")
        w(f"- 规则：{det.get('rule_id', '—')}")
        if det.get("rule_text"):
            w(f"- 判据原文：{det['rule_text']}")
        w(f"- 优先级：rank={det.get('priority_rank')} score={det.get('priority_score')} "
          f"（公式 `{det.get('score_formula')}`，来源 {det.get('score_source')}）")
        if det.get("score_basis"):
            w("- 计分分解：")
            w()
            w("  | 维度 | 原始值 | 权重 | 贡献 |")
            w("  |---|---|---|---|")
            for k, v in det["score_basis"].items():
                w(f"  | {k} | {v.get('raw')} | {v.get('weight')} | {v.get('contrib')} |")
            w()
        w(f"- 溯源行（{len(c.get('source_rows') or [])} 行）：")
        w()
        w("  ```json")
        w("  " + json.dumps(c.get("source_rows"), ensure_ascii=False, indent=2).replace("\n", "\n  "))
        w("  ```")
        w(f"- 状态：`{c['status']}`，审计链 {len(c.get('audit_log') or [])} 条")
        for a in c.get("audit_log") or []:
            w(f"  - {a['from_status']}→{a['to_status']} by {a['operator']} "
              f"({a['timestamp']}, event_id={a['event_id']})")
        w()

w("---")
w()

# ---------------- 5. 健康度 ----------------
w("## 五、运行健康度")
w()
w("```json")
w(json.dumps(d["健康度"], ensure_ascii=False, indent=2)[:4000])
w("```")
w()

out = OUT / "血缘追溯报告.md"
out.write_text("\n".join(L), encoding="utf-8")
print(f"✅ {out}")
print(f"   {len(L)} 行")
