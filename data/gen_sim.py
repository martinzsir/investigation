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
    """轨迹出行：地点为杭州市真实行政区划 + 真实道路门牌。

    为什么必须是真实地址
    --------------------
    旧版用「项目{name}」占位，parse_admin_path 无法解析 → 三轨编码全部
    落空 → obj_location.geocode_source='raw_fallback'、lat/lng 全 NULL
    → 空间研判（落脚点画像 / CGT 概率面 / 坐标同框）整条链路空转。

    设计要点
    --------
    1) 每个项目一个真实地址，保留「张卫国于公示日 +1 天到项目现场」的叙事；
    2) 同一路段（文三路）不同门牌 → dual_segment_key 相同 → 归并为同一
       location，张/李先后到访构成同框（R4 与 geo_co_located_radius）；
    3) 张卫国有效坐标事件 ≥5 → geo_profile_cgt 可成概率面。
    4) **新增时刻列**：源表带真实非零时刻（非 00:00:00），接入后 timestamp
       可判 minute/second 档，支持「同时同地」等时刻级判据；时刻全零会
       被 derive_time_precision 降级为 date 档（见 core/time_semantics.py）。
    """
    proj_addr = {
        "滨江路改造": "浙江省杭州市滨江区滨江路1288号",
        "城东管网":   "浙江省杭州市上城区庆春路200号",
        "安置房一期": "浙江省杭州市西湖区文三路100号",
        "桥梁加固":   "浙江省杭州市拱墅区莫干山路111号",
        "市政绿化":   "浙江省杭州市上城区秋涛北路456号",
        "智慧交通":   "浙江省杭州市滨江区江南大道3688号",
        "安置房二期": "浙江省杭州市余杭区文一西路1500号",
    }
    # 项目现场到访：分配工作时段时刻（上午/下午）
    # 时刻列必须是完整日期时间（cn_datetime_norm 按空格拆日期段/时刻段，
    # 纯时间无日期段会导致 TRY_CAST TIMESTAMP 落 NULL）
    proj_times = ["09:30:00", "14:00:00", "10:15:00", "16:45:00",
                  "08:50:00", "15:20:00", "11:00:00"]
    rows = []
    for (name, b), ts in zip(PROJECTS, proj_times):
        d = pd.Timestamp(b) + pd.DateOffset(days=1)
        rows.append({"日期": d,
                     "时刻": f"{d.strftime('%Y-%m-%d')} {ts}",
                     "主体": "张卫国",
                     "地点": proj_addr[name]})

    # 张卫国高频落脚点（文三路，与「安置房一期」项目地址同路段）
    # 晚间时刻 → 体现下班后到访
    zhang_late_times = ["19:30:00", "20:15:00", "21:00:00", "18:45:00", "22:10:00"]
    for d, ts in zip(("2020-01-08", "2020-07-15", "2021-02-20", "2022-06-11", "2023-03-09"),
                     zhang_late_times):
        rows.append({"日期": pd.Timestamp(d),
                     "时刻": f"{d} {ts}",
                     "主体": "张卫国",
                     "地点": "浙江省杭州市西湖区文三路100号"})

    # 李志强：相邻日到访同路段（文三路259号与 100 号归并为同一 location）
    li_times = ["18:00:00", "21:30:00", "17:15:00"]
    for d, ts in zip(("2020-01-07", "2020-07-14", "2021-02-21"), li_times):
        rows.append({"日期": pd.Timestamp(d),
                     "时刻": f"{d} {ts}",
                     "主体": "李志强",
                     "地点": "浙江省杭州市西湖区文三路259号"})
    # 李志强自有落脚点
    li_own_times = ["13:00:00", "09:45:00"]
    for (d, a), ts in zip((("2020-05-09", "浙江省杭州市滨江区江陵路88号"),
                            ("2021-11-23", "浙江省杭州市拱墅区莫干山路111号")),
                           li_own_times):
        rows.append({"日期": pd.Timestamp(d),
                     "时刻": f"{d} {ts}",
                     "主体": "李志强", "地点": a})

    pd.DataFrame(rows).to_parquet(OUT / "轨迹出行.parquet", index=False)


def gen_osint():
    pd.DataFrame([
        {"主体": "宏业建设", "公开信息": "招投标公示一致",     "发布日期": "2020-04-10", "来源": "公共资源交易网"},
        {"主体": "A建材",    "公开信息": "经营状态正常",     "发布日期": "2021-06-21", "来源": "企业信用公示"},
        {"主体": "张卫国",   "公开信息": "分管招投标",       "发布日期": "2019-03-01", "来源": "政府官网公示"},
        {"主体": "李志强",   "公开信息": "宏业法人",         "发布日期": "2019-08-15", "来源": "企业信用公示"},
    ]).to_parquet(OUT / "公开OSINT.parquet", index=False)


def gen_report():
    pd.DataFrame([
        {"举报日期": "2022-01-10", "分类": "经济类", "被举报人": "张卫国",
         "举报人": "匿名", "内容": "匿名举报：张卫国收受宏业李志强现金约120万元"},
        {"举报日期": "2022-03-22", "分类": "职务类", "被举报人": "张卫国",
         "举报人": "内部职工", "内容": "反映张卫国在安置房一期招投标中为宏业建设提前透露底标"},
        {"举报日期": "2023-02-15", "分类": "经济类", "被举报人": "李志强",
         "举报人": "同行", "内容": "A建材（李志强妻弟名下）收到宏业大额转账后，当日转给张卫国配偶"},
    ]).to_parquet(OUT / "举报材料.parquet", index=False)


def main():
    gen_flow(); gen_calls(); gen_bids(); gen_business(); gen_traj(); gen_osint(); gen_report()
    print("模拟数据已生成到", OUT)


if __name__ == "__main__":
    main()
