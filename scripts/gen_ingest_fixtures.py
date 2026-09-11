"""
scripts/gen_ingest_fixtures.py —— 数据接入全流程五格式夹具生成器。

产出到 data/test/ingest/，供「上传 → analyze 推荐 → 列映射 → 导入 → BUILD →
合规扫描」全链路手工/界面测试（表名/列名与 ontology/default/bindings.json 对齐）：

  人员信息（14 行，列名即声明源列；向导自动命中「人员信息」，element_hints 高置信）：
    人员信息.parquet / .csv(逗号) / _分号.csv / .xlsx / .json(records 数组) /
    _嵌套.json({"data":{"records":[...]}}) / .sqlite(人员信息 + 附件清单干扰表)
  脏值仅 3 行：带空格身份证(py despace 抢救)、+86 手机(strip_cc 抢救)、
    校验位错身份证 + 性别越界（合规扫描 checksum_failed/enum_unknown 样本）。

  银行流水_增量（10 行，列名是向导别名 付款方/收款方/金额（元）/交易日期/交易币种，
    需人工映射到 主体/对方/金额/日期/币种）：
    银行流水_增量.parquet / .csv / _分号.csv / .xlsx / .json / _嵌套.json /
    .sqlite(流水增量 + 手续费明细干扰表)
  脏值 2 行：千分位金额（transform strip_thousands 抢救）、中文日期（cn_date_norm 抢救）；
  币种列 9/10 命中 DE_CURRENCY 枚举（"韩元"越界 → 合规 enum_unknown）。

用法：
  /root/.venvs/inves/bin/python -m scripts.gen_ingest_fixtures
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.gen_robust_case import _id18, _id18_bad_checksum  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "test" / "ingest"

PERSON_COLS = ["姓名", "证件类型", "身份证号", "手机号", "性别", "案件类别"]
FLOW_COLS = ["付款方", "收款方", "金额（元）", "交易日期", "交易币种"]


# ---------------- 人员信息（14 行：11 干净 + 3 脏值）----------------

def _person_df() -> pd.DataFrame:
    rows: list[dict] = []
    genders = ["男", "女", "男", "女", "男", "女", "男", "女", "男", "女", "男"]
    case_types = ["贪污贿赂", "洗钱", "敲诈勒索", "电信网络诈骗", "组织领导传销"]
    names = ["周正阳", "沈知秋", "韩叔同", "吴若笙", "郑云起", "卫兰舟",
             "蒋士则", "秦可箴", "许停云", "何栖泽", "卢见山"]
    for i, name in enumerate(names, start=1):
        body = f"110101{1990 + i:04d}0101{i:03d}"
        rows.append({
            "姓名": name,
            "证件类型": "居民身份证",
            "身份证号": _id18(body),
            "手机号": f"1380101{i:04d}",
            "性别": genders[i - 1],
            "案件类别": case_types[(i - 1) % len(case_types)],
        })
    # 12：带空格身份证（analyze 不计入 format 命中；导入后 despace 抢救为合法值）
    body12 = "11010120020101012"
    id12 = _id18(body12)
    rows.append({"姓名": "苏挽晴", "证件类型": "居民身份证",
                 "身份证号": f"{id12[:6]} {id12[6:14]} {id12[14:]}",
                 "手机号": "13801010012", "性别": "女", "案件类别": "洗钱"})
    # 13：+86 前缀手机（strip_cc 抢救）
    rows.append({"姓名": "林尽欢", "证件类型": "居民身份证",
                 "身份证号": _id18("11010120030101013"),
                 "手机号": "+8613801010013", "性别": "男", "案件类别": "贪污贿赂"})
    # 14：校验位错身份证 + 性别"中性"越界（合规双违规样本）
    rows.append({"姓名": "高蔓生", "证件类型": "居民身份证",
                 "身份证号": _id18_bad_checksum("11010120040101014"),
                 "手机号": "13801010014", "性别": "中性", "案件类别": "敲诈勒索"})
    assert len(rows) == 14
    return pd.DataFrame(rows, columns=PERSON_COLS)


# ---------------- 银行流水增量（10 行，向导别名列名；2 行脏值）----------------

def _flow_df() -> pd.DataFrame:
    rows = [
        ("赵启明", "钱卫东", "5000.00", "2024-01-05", "人民币"),
        ("赵启明", "孙雅然", "12500.50", "2024-01-12", "人民币"),
        ("冯敬亭", "钱卫东", "8600.00", "2024-02-03", "人民币"),
        ("褚文彬", "李卫宁", "3300.00", "2024-02-15", "人民币"),
        ("卫兰舟", "蒋士则", "9900.00", "2024-03-02", "人民币"),
        ("何栖泽", "许停云", "4200.00", "2024-03-18", "人民币"),
        ("高蔓生", "苏挽晴", "7800.00", "2024-04-06", "美元"),
        ("卢见山", "林尽欢", "5100.00", "2024-04-21", "人民币"),
        ("沈知秋", "韩叔同", "50,000.00", "2024-05-09", "人民币"),    # 千分位 → transform 抢救
        ("吴若笙", "郑云起", "6700.00", "2024年5月18日", "韩元"),       # 中文日期抢救 + 币种越界
    ]
    assert len(rows) == 10
    return pd.DataFrame(rows, columns=FLOW_COLS)


# ---------------- 五格式落盘 ----------------

def _write_five_formats(stem: str, df: pd.DataFrame, *, sqlite_tables: list[tuple]) -> None:
    """stem 前缀写 parquet/csv(逗号+分号)/xlsx/json/嵌套json/sqlite 七件。"""
    df.to_parquet(OUT_DIR / f"{stem}.parquet", index=False)
    df.to_csv(OUT_DIR / f"{stem}.csv", index=False, encoding="utf-8-sig")
    df.to_csv(OUT_DIR / f"{stem}_分号.csv", index=False, sep=";",
              encoding="utf-8-sig")
    df.to_excel(OUT_DIR / f"{stem}.xlsx", index=False, engine="openpyxl")
    records = df.to_dict(orient="records")
    (OUT_DIR / f"{stem}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / f"{stem}_嵌套.json").write_text(
        json.dumps({"code": 0, "data": {"records": records}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    db_path = OUT_DIR / f"{stem}.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        for table_name, table_df in sqlite_tables:
            table_df.to_sql(table_name, conn, index=False, if_exists="fail")
    finally:
        conn.close()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    person = _person_df()
    _write_five_formats(
        "人员信息", person,
        sqlite_tables=[
            ("人员信息", person),   # rowid 第一张：默认读入目标
            ("附件清单", pd.DataFrame({
                "附件编号": ["ATT-001", "ATT-002", "ATT-003"],
                "文件名": ["询问笔录.pdf", "银行回执扫描.zip", "通话清单.xlsx"]})),
        ])
    flow = _flow_df()
    _write_five_formats(
        "银行流水_增量", flow,
        sqlite_tables=[
            ("流水增量", flow),
            ("手续费明细", pd.DataFrame({
                "扣费日期": ["2024-01-31", "2024-02-29"],
                "手续费": ["5.50", "3.00"]})),
        ])
    n_files = len(list(OUT_DIR.iterdir()))
    print(f"接入夹具已生成：{OUT_DIR}（{n_files} 个文件）")
    for p in sorted(OUT_DIR.iterdir()):
        print(" -", p.name, f"{p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
