"""
data/gen_sim.py
生成模拟数据：银行流水 / 通话记录 / 招投标档案 / 工商信息 / 轨迹出行 / 公开OSINT / 举报材料。
金额统一以"元"存储（万为单位在查询时换算，避免单位错配）。
输出 Parquet 分区，供 DuckDB read_parquet(...) 直接扫描。
"""

from pathlib import Path
import pandas as pd

OUT = Path(__file__).parent
OUT.mkdir(exist_ok=True)

PROJECTS = [
    ("滨江路改造", "2019-06-18"), ("城东管网", "2020-03-25"), ("安置房一期", "2020-11-10"),
    ("桥梁加固", "2021-09-02"), ("市政绿化", "2022-04-20"), ("智慧交通", "2022-12-05"),
    ("安置房二期", "2023-08-15"),
]
AMT = 100000  # 每笔 10万（元）


def gen_flow():
    rows = []
    # 工资（非整数，用于对比）
    for y in range(2019, 2024):
        for m in [1, 4, 7, 10]:
            rows.append({"日期": f"{y}-{m:02d}-05", "主体": "张卫国", "对方": "财政局", "金额": 18532 + m})
    # 季度末整数现金存入（与中标时间窗耦合）
    offsets = [7, 5, 18, 10, 8, 13, 7]
    for (name, bid), off in zip(PROJECTS, offsets):
        d = pd.Timestamp(bid) + pd.DateOffset(days=off)
        rows.append({"日期": d.strftime("%Y-%m-%d"), "主体": "张卫国", "对方": "现金存入", "金额": AMT})
    # 过桥：宏业 → A建材 → 配偶
    rows.append({"日期": "2021-10-01", "主体": "宏业建设", "对方": "A建材", "金额": 4600000})
    rows.append({"日期": "2021-11-15", "主体": "A建材", "对方": "张卫国配偶", "金额": 1700000})
    pd.DataFrame(rows).to_parquet(OUT / "银行流水.parquet", index=False)


def gen_calls():
    rows = []
    for i in range(114):
        rows.append({"日期": f"2020-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
                     "主体": "张卫国", "对端": "李志强", "次数": 1})
    pd.DataFrame(rows).to_parquet(OUT / "通话记录.parquet", index=False)


def gen_bids():
    pd.DataFrame(
        [{"项目": n, "中标公示日": b, "中标方": "宏业建设", "分管领导": "张卫国"}
         for n, b in PROJECTS]
    ).to_parquet(OUT / "招投标档案.parquet", index=False)


def gen_business():
    pd.DataFrame([
        {"主体": "宏业建设", "法人": "李志强", "状态": "存续"},
        {"主体": "A建材", "法人": "李志强妻弟", "状态": "存续"},
        {"主体": "张卫国配偶", "关联": "张卫国", "状态": "-"},
    ]).to_parquet(OUT / "工商信息.parquet", index=False)


def gen_traj():
    rows = [{"日期": pd.Timestamp(b) + pd.DateOffset(days=1), "主体": "张卫国", "地点": f"项目{name}"}
            for name, b in PROJECTS]
    pd.DataFrame(rows).to_parquet(OUT / "轨迹出行.parquet", index=False)


def gen_osint():
    pd.DataFrame([
        {"主体": "宏业建设", "公开信息": "招投标公示一致"},
        {"主体": "A建材", "公开信息": "经营状态正常"},
        {"主体": "张卫国", "公开信息": "分管招投标"},
        {"主体": "李志强", "公开信息": "宏业法人"},
    ]).to_parquet(OUT / "公开OSINT.parquet", index=False)


def gen_report():
    pd.DataFrame([{"内容": "匿名举报：张卫国收受宏业李志强现金约120万元"}]).to_parquet(
        OUT / "举报材料.parquet", index=False)


def main():
    gen_flow(); gen_calls(); gen_bids(); gen_business(); gen_traj(); gen_osint(); gen_report()
    print("模拟数据已生成到", OUT)


if __name__ == "__main__":
    main()
