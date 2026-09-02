"""
run_demo.py
端到端演练：生成数据 → 初始化 DuckDB → 六段输出 → 奇兵拓线 → 五间交叉 → 校验 → 导出。
侦查逻辑与 StarRocks 完全解耦，全部走 DuckDB。
"""

import json
from pathlib import Path
from core import Store, MiaoSuan, validate, redline_check
from skills import miaosuan, zhi_ji_zhi_bi, xu_shi, qi_zheng, yong_jian
import data.gen_sim as gen_sim
import scripts.init_duckdb as init_duckdb

OUT = Path("output")
OUT.mkdir(exist_ok=True)

CTX = {
    "可用数据": ["银行流水", "通话记录", "招投标档案", "工商信息", "轨迹出行", "公开OSINT", "举报材料"],
    "未调取": ["房产车辆"],
    "证据缺口": ["现金来源无法溯源", "A公司流水缺口95万"],
    "授权边界": ["不可查房产车辆", "不可直接接触对象"],
}


def main():
    print("[0] 生成模拟数据（Parquet 分区）...")
    gen_sim.main()
    print("[1] 初始化 DuckDB 温层（替代 StarRocks）...")
    init_duckdb.main()

    store = Store()
    miao = MiaoSuan()
    output: dict = {}

    print("[2] 庙算沙盘...")
    output.update(miaosuan.run(CTX))
    # 复用同一 MiaoSuan 实例（保留知己栏）
    miao.hypotheses = output["庙算基线"]["hypotheses"] and miao  # placeholder
    # 直接用 run 返回的假设回填
    from core import Hypothesis
    miao.ji = output["庙算基线"]["ji"]

    print("[3] 双向盘点（知己强制非空）...")
    output.update(zhi_ji_zhi_bi.run(CTX, miao=miao))

    print("[4] 虚实扫描（DuckDB 扫 Parquet）...")
    output.update(xu_shi.run(CTX, store=store))

    print("[5] 奇正分工（奇兵拓线 + 正兵固证）...")
    output.update(qi_zheng.run(CTX, store=store))

    print("[6] 用间交叉...")
    output.update(yong_jian.run(CTX, store=store))

    print("[7] 全胜校验（自曝风险 + 溯源）...")
    output["全胜校验"] = {
        "现金存入与中标时间窗重合": "可溯源（银行流水 + 中标档案）— 需人工复核",
        "A建材过桥通道": "可溯源（工商+流水）— 须新增授权调取 A公司流水",
        "轨迹同框": "需人工核实是否为公务活动",
        "张卫国收受财物(定性)": "严禁AI给出，须言词证据+法定程序",
    }

    # 校验
    v = validate(output)
    r = redline_check(miao)
    output["_校验"] = {"schema": v, "redline": r}

    # 导出
    (OUT / "六段输出.json").write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("✅ 运行完成，输出：", OUT / "六段输出.json")
    print("校验：", "通过" if v["passed"] and r["passed"] else "有问题")
    print("用间交叉等级：", output["用间交叉"]["交叉等级"])
    store.close()


if __name__ == "__main__":
    main()
