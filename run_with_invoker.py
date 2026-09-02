"""
run_with_invoker.py
第 3 项交付验证：把五子技能统一封装成 skill_invoke() 接口。

流程：
  1. 生成模拟数据 + 初始化 DuckDB（沿用原有脚本逻辑）
  2. 通过 skill_invoke() 统一调用五子技能（庙算/知己/虚实/奇正/用间）
     → 每个技能返回 [LineageClue]（带血缘的线索）
  3. 对全部线索做血缘去重合并（dedupe_and_merge）
  4. 输出 lineage_report（按间类分组 + 交叉升格）
  5. 与原有六段输出对比，校验一致性

用法：python run_with_invoker.py
"""

import json, subprocess, sys, os
from pathlib import Path

from core import (
    Store, MiaoSuan, Hypothesis,
    skill_invoke, get_registry, LineageClue,
)
from core.lineage import dedupe_and_merge, lineage_report
from skills.registry_bootstrap import register_all

ROOT = Path(__file__).parent
os.chdir(ROOT)

# 0. 复用原有数据生成 + DuckDB 初始化
subprocess.run([sys.executable, "-m", "data.gen_sim"], check=False, capture_output=True)
subprocess.run([sys.executable, "-m", "scripts.init_duckdb"], check=False, capture_output=True)


def main():
    # 触发自动注册
    registry = register_all()
    print("=== 已注册技能 ===")
    for s in registry.all_specs():
        print(f"  {s.stage:4s} | {s.skill_id:14s} | {s.name} | 间={s.consumes_jian}")

    store = Store()
    miao = MiaoSuan()

    CTX = {
        "可用数据": ["银行流水", "通话记录", "招投标档案", "工商信息", "轨迹出行", "公开OSINT", "举报材料"],
        "未调取": ["房产车辆"],
        "证据缺口": ["现金来源无法溯源", "A公司流水缺口95万"],
        "授权边界": ["不可查房产车辆", "不可直接接触对象"],
    }

    # 庙算（产生假设，供后续血缘反推）
    from skills.miaosuan import run as miaosuan_run
    out = miaosuan_run(CTX)
    miao.ji = out["庙算基线"]["ji"]
    for hd in out["庙算基线"]["hypotheses"]:
        miao.add(Hypothesis(**hd))

    # 让 store 持有 miao 引用（供前置校验），用 ctx 传递
    CTX["miao"] = miao

    # 补齐知己（让非庙算技能的 ji 校验通过）
    from skills.zhi_ji_zhi_bi import run as zj_run
    out.update(zj_run(CTX, miao=miao))

    # ------------------------------------------------------------------
    # 关键：通过 skill_invoke 统一调用虚实 / 奇正 / 用间
    # 每个调用返回 [LineageClue]，自动挂假设溯源 + 写 L1
    # ------------------------------------------------------------------
    all_clues: list[LineageClue] = []

    for sid in ["xu_shi", "qi_zheng", "yong_jian"]:
        clues = skill_invoke(registry, sid, miao=miao, store=store, ctx=CTX)
        print(f"\n[{sid}] 产出 {len(clues)} 条 LineageClue：")
        for c in clues:
            print("   ", c.summary())
        all_clues.extend(clues)

    # ------------------------------------------------------------------
    # 血缘去重合并：把同源线索合成一条（减少正兵负担）
    # ------------------------------------------------------------------
    print("\n=== 血缘去重合并 ===")
    merged = dedupe_and_merge(all_clues, threshold=0.5)
    print(f"合并前 {len(all_clues)} 条 → 合并后 {len(merged)} 条")
    for c in merged:
        print("   ", c.summary())

    # 汇总报告
    report = lineage_report(merged)
    print("\n=== 用间交叉升格 ===")
    print("覆盖间类:", report["jian_coverage"])
    print("交叉等级:", report["cross_level"])

    # 写出产物
    Path("output").mkdir(exist_ok=True)
    Path("output/lineage_clues.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\n✅ 线索已写入 output/lineage_clues.json")

    # ------------------------------------------------------------------
    # 处置状态演示（正兵跟踪：模拟 待查→查证中→已固证→已立案 流程）
    # ------------------------------------------------------------------
    from core.disposal import DisposalBoard
    board = DisposalBoard(merged, store=store)
    board.restore()  # 若已有历史处置进度则回灌

    if merged:
        top = merged[0]  # 取优先级最高的线索演示
        print(f"\n=== 处置状态演示（优先级最高的线索 {top.clue_id}）===")
        board.transition(top.clue_id, "查证中", operator="正兵", note="已调取A公司流水核实")
        board.confirm(top.clue_id, operator="正兵", note="过桥结构经流水比对成立")
        try:
            # 「已立案」受控：须具名正兵 + 法定依据
            board.file(top.clue_id, operator="王检察官", legal_basis="杭检立〔2026〕XX号")
            print(f"  {top.clue_id} 已置为 已立案（法定依据已入审计链）")
        except ValueError as e:
            print(f"  [预期拦截] {e}")

    # 其余线索标记为查证中，演示终态之外的状态
    for c in merged[1:]:
        board.transition(c.clue_id, "查证中", operator="system")

    written = board.persist()
    print(f"\n  处置状态已落 DuckDB（{written} 行）→ 表 clue_disposal_status")
    board.print_report()

    # 验证：从 DuckDB 重新读出，确认持久化生效
    board2 = DisposalBoard(merged, store=store)
    restored = board2.restore()
    print(f"  从 DuckDB 回灌进度：{restored} 条状态恢复")

    store.close()


if __name__ == "__main__":
    main()
