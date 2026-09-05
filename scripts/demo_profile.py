"""
scripts/demo_profile.py
REQ-P M5 演练：六层本体画像 + 数据地图 → output/profile_report.md / data_map.md。

红线：
  - 只读语义层（经 OntologyReadGateway，STALE/未知对象防护由网关保证），
    不写任何 obj_*/lnk_* 表；
  - 默认 health=None（NullRunHealth，连 run_diagnostic 也不写）；
    --record-diagnostics 时才落运行诊断（治理留痕，非画像写回）；
  - 关注主体从 case_knowledge.subject_aliases 声明读取（不硬编码人名）；
  - 全部结论均为【待核实】候选，render 层固化免责文案。

用法：
    python -m scripts.demo_profile                         # 读 investigation.duckdb
    python -m scripts.demo_profile --anchor 2021-10-01     # 指定时间窗锚点
    python -m scripts.demo_profile --record-diagnostics    # 落 run_diagnostic 诊断
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import Store
from core.gateway import OntologyReadGateway
from core.functions import load_case_knowledge
from core.ontology_profile import OntologyProfiler, record_map_gaps
from core.data_map import DataMap
from core.run_health import RunHealth

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "output"

_UNAVAIL = "—"


def _fmt_pct(x) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else _UNAVAIL


def render_profile(report: dict) -> str:
    L: list[str] = []
    ap = L.append
    h = report.get("health") or {}
    ap("# 六层本体画像报告")
    ap("")
    ap("> 红线：只观察不写回。全部结论均为【待核实】候选，不构成办案指导；")
    ap("> 画像质量分与启发式扣分（肯定式识别/变体）均可人工推翻。")
    ap("")
    ap("## 健康度")
    ap("")
    ap(f"- 状态：**{h.get('status', _UNAVAIL)}**　诊断总数：{h.get('诊断总数', 0)}"
      f"（critical {h.get('计数', {}).get('critical', 0)} / "
      f"warning {h.get('计数', {}).get('warning', 0)} / "
      f"info {h.get('计数', {}).get('info', 0)}）")
    if h.get("分类计数"):
        ap(f"- 分类：{h.get('分类计数')}")
    ap(f"- 说明：{h.get('说明', '')}")
    ap("")
    p = report.get("params", {})
    ap("## 参数")
    ap("")
    ap(f"- 时间窗：±{p.get('window_days', _UNAVAIL)} 天；"
      f"锚点：{p.get('anchor_date') or '未提供（时间窗指标 not_evaluated）'}")
    ap(f"- 关注主体（case_knowledge 声明）：{p.get('focus_entities') or '无'}")
    ap("")

    # L4 五间
    ap("## L4 五间覆盖（反向查：缺哪间 → 该补什么数据）")
    ap("")
    ap("| 间类 | 已声明对象/链接 | 已物化 |")
    ap("|---|---|---|")
    for x in report["l4"]["reverse"]:
        declared = ",".join(x["objects"] + x["links"]) or "（无声明，设计缺口）"
        ap(f"| {x['jian']} | {declared} | {'是' if x['has_materialized'] else '否'} |")
    ap("")

    # L5 质量分
    l5 = report["l5"]
    lo, hi = l5["score_range"]
    ap("## L5 质量分")
    ap("")
    ap(f"- 分数：**{lo}**（可推翻启发项回补后区间上沿 {hi}）")
    ap("")
    ap("| 范围 | 位置 | 项 | 扣分 | 依据 |")
    ap("|---|---|---|---:|---|")
    for d in l5["deductions"]:
        ap(f"| {d['severity']} | {d['scope']} | {d['ref']} | {d['points']} "
           f"| {d['code']}：{d['reason']} |")
    ap("")

    # L1/L2 摘要（只列异常/可连接属性，避免全量刷屏）
    ap("## L1/L2 列层/值层摘要（混装/高空值率/缺列/未物化）")
    ap("")
    ap("| 对象.属性 | 声明类型 | 状态 | 空值率 | 基数 | 值类型分布 | 混装 | 落点建议 |")
    ap("|---|---|---|---:|---:|---|---|---|")
    for e in report["l1_l2"]:
        if e["status"] != "ok" or e.get("mixed"):
            vp = e.get("value_profile", {})
            ap(f"| {e['obj']}.{e['prop']} | {e['declared_type']} | {e['status']} "
              f"| {_fmt_pct(vp.get('null_rate'))} | {vp.get('distinct', _UNAVAIL)} "
              f"| {e.get('type_dist', _UNAVAIL)} "
              f"| {'⚠ 是' if e.get('mixed') else _UNAVAIL} "
              f"| {e.get('landing_suggestions', _UNAVAIL)} |")
    ap("")

    # L3 指标
    ap("## L3 语义指标")
    ap("")
    ap("| 对象.属性 | 指标 | 值 | 分子/分母 |")
    ap("|---|---|---:|---|")
    for e in report["l3"]:
        if e.get("status") == "not_evaluated":
            ap(f"| {e['obj']}.{e['prop']} | {e['metric']} | not_evaluated | {e.get('reason','')} |")
        else:
            ap(f"| {e['obj']}.{e['prop']} | {e['metric']} | {e.get('value')} "
              f"| {e.get('numerator')}/{e.get('denominator')} |")
    ap("")
    ap("---")
    ap(f"_{report.get('note', '')}_")
    return "\n".join(L) + "\n"


def render_data_map(dm: DataMap) -> str:
    md = dm.render_markdown().rstrip("\n")
    gaps = dm.normalize_gaps()
    mmd = dm.render_mermaid()
    tail = ["", "## Mermaid 拓扑（断链/缺口虚线）", "",
            "```mermaid", mmd, "```", ""]
    if gaps is None:
        tail += ["> 归一缺口：bindings 缺失，**缺口未计算**（≠无缺口）。", ""]
    elif gaps:
        tail += [f"> 归一缺口 {len(gaps)} 处（已落 map_normalize_gap 诊断）：", ""]
        for g in gaps:
            tail.append(f"> - {g['object']}.{g['prop']}：{g['note']}")
        tail.append("")
    else:
        tail += ["> 归一缺口：0（全部 raw 引用均被等值归一覆盖）。", ""]
    return md + "\n" + "\n".join(tail)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="六层本体画像 + 数据地图演练（只读）")
    ap.add_argument("--pack", default="default", help="ontology 案件包名")
    ap.add_argument("--db", default=None, help="DuckDB 路径（默认 investigation.duckdb）")
    ap.add_argument("--anchor", default=None, help="时间窗锚点日期 YYYY-MM-DD")
    ap.add_argument("--record-diagnostics", action="store_true",
                    help="落 run_diagnostic 运行诊断（默认不落任何库表）")
    args = ap.parse_args(argv)

    store = Store(root=str(ROOT), db_path=args.db) if args.db else Store(root=str(ROOT))
    gw = OntologyReadGateway(store.conn)
    if gw.materialization_state() == "UNBUILT":
        print("语义层未构建：请先跑 python run_all.py --no-cli 或 "
              "python -m scripts.build_ontology")
        return 2

    health = RunHealth(db=store.conn) if args.record_diagnostics else None
    focus = list((load_case_knowledge(args.pack).get("subject_aliases") or {}).keys())
    prof = OntologyProfiler(gw, pack=args.pack, focus_entities=focus,
                            anchor_date=args.anchor, health=health)
    report = prof.profile_all()

    dm = DataMap.from_pack(ROOT / "ontology", pack=args.pack)
    n_gap = record_map_gaps(health, dm.normalize_gaps())
    if health is not None:
        # map 缺口诊断在 profile_all 之后落，刷新健康度小节再渲染
        report["health"] = health.health_section()

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "profile_report.md").write_text(
        render_profile(report), encoding="utf-8")
    (OUT_DIR / "data_map.md").write_text(render_data_map(dm), encoding="utf-8")

    lo, hi = report["l5"]["score_range"]
    print(f"画像完成：质量分 {lo}（区间上沿 {hi}）；"
          f"归一缺口诊断 {n_gap} 条；诊断落库={args.record_diagnostics}")
    print(f"产物：{OUT_DIR/'profile_report.md'}")
    print(f"      {OUT_DIR/'data_map.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
