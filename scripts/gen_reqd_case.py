"""
scripts/gen_reqd_case.py —— 海州"11·03"刷单返利电诈案数据生成器（REQ-D 业务测试案例）。

确定性合成数据（无随机、无真实 PII），脏数据行数与案例文档逐项精确一致：
  data/reqd_case/银行流水_电诈.parquet      120 行（货币符/千分位 15、科学计数法 3、
                                            负数 5、孤峰 1、连字符卡号 8、星号卡号 6、
                                            校验位错 4、脏日期 10、未来日期 2、
                                            非法日期 2、重复流水号 3 对、组织名 40）
  data/reqd_case/通话记录_电诈.parquet      80 行（三种电话格式 9、非法号段 2）
  data/reqd_case/人员身份_电诈.parquet      26 行（校验位错 6、17 位 3、小写 x 2、
                                            双"李强"强证据全异 2、"李  强"变体 1、
                                            "王银行"误报陷阱 1）
  data/reqd_case/案件台账_电诈.parquet       1 行（案件类别"杀猪盘"代码表外值）
  data/reqd_case/银行流水_复合列.parquet    15 行（对方信息 = 姓名|身份证号）
  data/reqd_case/通话记录_旧版.parquet      10 行（缺"对端"列的旧版变体）

build_tables() 返回 {表名: (列, 行)}，测试与 parquet 落盘共用同一真源。
用法：python -m scripts.gen_reqd_case
"""
from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "reqd_case"


# ---- 校验位工具（与 core/data_elements.py 同口径的独立实现，生成合法/非法样本）----

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def idcard_check_digit(first17: str) -> str:
    s = sum(int(c) * w for c, w in zip(first17, _ID_WEIGHTS))
    return _ID_CHECK[s % 11]


def make_idcard(base17: str, *, valid: bool = True, lower_x: bool = False,
                short: bool = False) -> str:
    """合法/校验位错误/17 位（缺校验位）/小写 x 结尾的身份证号。

    base17 必须是 17 位前缀（多余位掐掉容错）；
      short=True   → 返回缺校验位的 17 位（016 format 失败样本）；
      valid=False  → 追加一位错误校验位（016 checksum 失败样本）；
      lower_x=True → 校验位恰为 X 时用小写 x（合法，016 AC-8 防误报样本）。
    """
    base17 = (base17 or "").strip()[:17]
    assert len(base17) == 17 and base17.isdigit(), base17
    if short:
        return base17
    check = idcard_check_digit(base17)
    if not valid:
        bad = "0" if check != "0" else "1"
        return base17 + bad
    if lower_x and check == "X":
        return base17 + "x"
    return base17 + ("x" if lower_x else check)


def luhn_digit(payload: str) -> str:
    """Luhn 校验位（payload 末位补齐后整体 mod10=0）。"""
    total = 0
    for i, ch in enumerate(reversed(payload + "0")):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def make_card(payload15: str, *, valid: bool = True) -> str:
    if valid:
        return payload15 + luhn_digit(payload15)
    bad = luhn_digit(payload15)
    return payload15 + ("0" if bad != "0" else "1")   # 必破坏 mod10


# ---- 合成名册（全合成，避开真实名人；个人名不含电诈组织词 商贸/科技/工作室/引流）----

SUBJECTS = ["祁明烨", "邵一鸣", "孟繁星", "华清越", "樊皓宁", "戴若曦",
            "温书豪", "葛天菲", "郁晨曦", "童千帆", "米建勋", "阮青山",
            "岳鸣谦", "闵子腾", "任慕白", "廖俊驰"]            # 16 受害人/嫌疑人
_SURNAMES = "赵钱孙李周吴郑王冯陈褚卫沈韩杨朱秦许何吕施张孔曹严华金魏陶姜"
_GIVENS = ["志明", "嘉禾", "梓涵", "宇轩", "泽彦", "辰宜", "静安", "书瑶",
           "云舒", "景行", "望舒", "知远"]
ORG_NAMES = (
    [f"海州{s}商贸" for s in
     ("恒信", "广泰", "汇金", "联丰", "腾达", "隆昌", "瑞丰", "嘉禾",
      "永顺", "宏图", "百汇", "天跃", "启程", "安捷")]                 # 14
    + [f"{s}科技" for s in
       ("淘数", "云帆", "星河", "极光", "蓝湾", "迅腾", "卓越", "智联",
        "灵犀", "蜂鸟", "磐石", "睿思")]                               # 12
    + [f"{s}工作室" for s in
       ("皓月", "逐风", "拾光", "青柠", "麦浪", "星芒", "布谷", "白杨")]  # 8
    + ["精准引流", "全网引流", "短视频引流", "社群引流", "搜索引流",
       "代运营引流"]                                                   # 6
)                                                                      # 40
PERSONAL_POOL = [f"{_SURNAMES[i % 30]}{_GIVENS[i // 30]}" for i in range(64)]
assert len(set(PERSONAL_POOL)) == 64 and len(set(ORG_NAMES)) == 40
assert not any(any(t in n for t in ("商贸", "科技", "工作室", "引流"))
               for n in SUBJECTS + PERSONAL_POOL)

# 组织词（与 ontology/reqd_case/clean_rules.json 电诈词表同源，仅生成器自用断言）
_FRAUD_TOKENS = ("商贸", "科技", "工作室", "引流")

_CLEAN_AMOUNTS = [4800.00, 5200.00, 6800.00, 5350.00, 6150.00, 4950.00]
_DATES = [f"2025-01-{d:02d}" for d in range(5, 30)] + \
         [f"2025-02-{d:02d}" for d in range(1, 21)] + \
         [f"2025-03-{d:02d}" for d in range(1, 16)]
assert len(_DATES) == 60


def _card_seq(i: int) -> str:
    return make_card(f"6222{i % 10}{(i * 37 + 101) % 100000:05d}{i * 13 % 10000:04d}"
                     f"{(i * 91 + 7) % 1000:03d}")[:15] + ""  # 占位，实际在行内拼


def _flow_rows() -> list[dict]:
    """120 行银行流水：12 组脏数据互不重叠，行数与文档精确一致。"""
    rows: list[dict] = []
    n = 0
    di = 0

    def add(主体, 对方, 金额, 日期, 卡号, 摘要, 流水号=None):
        nonlocal n
        n += 1
        rows.append({
            "流水号": 流水号 or f"HZ2025-{n:04d}", "主体": 主体, "对方": 对方,
            # 金额列统一文本（真实导出即混合脏值形态；float 规格化为两位小数）
            "金额": 金额 if isinstance(金额, str) else f"{金额:.2f}",
            "日期": 日期, "卡号": 卡号, "摘要": 摘要,
        })

    for i in range(15):        # 货币符+千分位 15（5×￥1,280.50 / 10×48,000.00）
        add(SUBJECTS[i % 16], PERSONAL_POOL[i],
            "￥1,280.50" if i < 5 else "48,000.00",
            _DATES[di % 60], make_card(f"62220{i:02d}3456{(i * 17 + 89) % 10000:04d}"),
            "刷单返利垫付")
        di += 1
    for i in range(3):         # 科学计数法 3（DuckDB DOUBLE 可解析 → 1280.0）
        add(SUBJECTS[(i + 3) % 16], PERSONAL_POOL[15 + i], "1.28e3",
            _DATES[di % 60], make_card(f"62221{i:02d}3456{(i * 23 + 45) % 10000:04d}"),
            "返利入账")
        di += 1
    for i in range(5):         # 负数 5（016 AC-3）
        add(SUBJECTS[(i + 6) % 16], PERSONAL_POOL[18 + i], "-500",
            _DATES[di % 60], make_card(f"62222{i:02d}3456{(i * 29 + 11) % 10000:04d}"),
            "冲正退回")
        di += 1
    add(SUBJECTS[11], PERSONAL_POOL[23], "9,999,999.99", _DATES[di % 60],
        make_card("6222334567890123"), "大额归集（孤峰）")            # 孤峰 1
    di += 1
    for i in range(8):         # 连字符卡号 8（006 AC-2：digits_only 抢救）
        card = ("6222-0212-3456-7890" if i == 0 else
                f"6222-{(i * 111) % 10000:04d}-{(i * 733) % 10000:04d}-"
                f"{(i * 97 + 321) % 10000:04d}")
        add(SUBJECTS[(i + 12) % 16], PERSONAL_POOL[24 + i],
            _CLEAN_AMOUNTS[i % 6], _DATES[di % 60], card, "垫付刷单")
        di += 1
    for i in range(6):         # 星号遮蔽卡号 6（reject_if:contains_mask → 剔除）
        add(SUBJECTS[i % 16], PERSONAL_POOL[32 + i], _CLEAN_AMOUNTS[(i + 1) % 6],
            _DATES[di % 60], "6222********7890", "垫付刷单")
        di += 1
    for i in range(4):         # 卡号校验位错 4（016 AC-1，luhn）
        add(SUBJECTS[(i + 2) % 16], PERSONAL_POOL[38 + i], _CLEAN_AMOUNTS[i % 6],
            _DATES[di % 60], make_card(f"62224{i:02d}5678{(i * 41 + 77) % 10000:04d}",
                                       valid=False), "刷单垫付")
        di += 1
    dirty_dates = (["2025年1月6日"] * 3 + ["2025/1/6"] * 3
                   + ["2025-1-6"] * 2 + ["2025.01.06"] * 2)           # 10
    for i, d in enumerate(dirty_dates):
        add(SUBJECTS[(i + 5) % 16], PERSONAL_POOL[42 + i], _CLEAN_AMOUNTS[i % 6],
            d, make_card(f"62225{i:02d}6789{(i * 53 + 13) % 10000:04d}"), "刷单")
        di += 1
    for i in range(2):         # 未来日期 2（016 AC-4）
        add(SUBJECTS[(i + 8) % 16], PERSONAL_POOL[47 + i], _CLEAN_AMOUNTS[i % 6],
            "2026-12-31", make_card(f"62226{i:02d}7890{(i * 61 + 29) % 10000:04d}"),
            "预付")
        di += 1
    for i in range(2):         # 非法日期 2（B2-08 回归：TRY_CAST 降级）
        add(SUBJECTS[(i + 10) % 16], PERSONAL_POOL[49 + i], _CLEAN_AMOUNTS[i % 6],
            "2025-02-30", make_card(f"62227{i:02d}8901{(i * 67 + 31) % 10000:04d}"),
            "刷单")
        di += 1
    # 重复流水号 3 对（内容略异；015 keep_latest 收敛）
    for i, base in enumerate(("HZ2025-9101", "HZ2025-9102", "HZ2025-9103")):
        add(SUBJECTS[(i + 13) % 16], PERSONAL_POOL[51 + i], "4800.00",
            f"2025-02-{10 + i:02d}",
            make_card(f"62228{i:02d}9012{(i * 71 + 41) % 10000:04d}"),
            "重复导入·先", 流水号=base)
        add(SUBJECTS[(i + 13) % 16], PERSONAL_POOL[51 + i], "4800.50",
            f"2025-02-{10 + i:02d}",
            make_card(f"62228{i:02d}9012{(i * 71 + 41) % 10000:04d}"),
            "重复导入·后", 流水号=base)
    for i, org in enumerate(ORG_NAMES):        # 组织名 40（008 AC-1/4）
        add(SUBJECTS[i % 16], org, _CLEAN_AMOUNTS[i % 6], _DATES[(i + 30) % 60],
            make_card(f"62229{i % 100:02d}0123{(i * 83 + 51) % 10000:04d}"),
            f"刷单返利{('引流款' if i % 2 else '佣金')}")
    for i in range(18):        # 干净行 18（补齐 120）
        add(SUBJECTS[(i + 4) % 16], PERSONAL_POOL[(i * 3 + 7) % 64],
            _CLEAN_AMOUNTS[i % 6], _DATES[(i + 40) % 60],
            make_card(f"62230{i % 100:02d}1234{(i * 97 + 61) % 10000:04d}"),
            "货款")
    assert len(rows) == 120, len(rows)
    return rows


def _calls_rows() -> list[dict]:
    """80 行通话记录：非法号段 2 + 三种电话格式 9 + 干净 69。"""
    rows = []
    bad = ["12345678901", "12345678901"]                 # 12 位非法号段 ×2
    fmts = (["138 0013 8000"] * 3 + ["+86 138-0013-8000"] * 3
            + ["008613800138000"] * 3)                  # 三种格式各 3（末组=0086+11位手机号）
    raws = bad + fmts
    for i in range(80):
        raw = raws[i] if i < len(raws) else f"138{i:08d}"
        rows.append({
            "主体": SUBJECTS[i % 16],
            "对端": PERSONAL_POOL[(i * 5 + 3) % 64],
            "日期": _DATES[i % 60],
            "次数": (i % 9) + 1,
            "对端号码_原始": raw,
        })
    return rows


def _id_base(i: int) -> str:
    """17 位身份证前缀：6 位地区 + 8 位出生日期 + 3 位顺序码（校验位由 make_idcard 补）。"""
    return (f"3301{i % 10:02d}"              # 地区码 6 位
            f"19{i % 4 + 80:02d}"            # 出生年 1980-1983
            f"{i % 12 + 1:02d}"              # 月 01-12
            f"{i % 28 + 1:02d}"              # 日 01-28
            f"{i * 137 % 1000:03d}")         # 顺序码 3 位


def _person_rows() -> list[dict]:
    """26 行人员身份：双"李强"陷阱 + 误报陷阱 + 各类校验位样本。"""
    rows = []
    # 1-6 校验位错（016 AC-1）
    for i, nm in enumerate(("尤志远", "卞国强", "涂长海", "詹淑英", "廖春梅", "蒲建军")):
        rows.append({"姓名": nm, "电话": f"137{i:08d}",
                     "身份证号": make_idcard(_id_base(i), valid=False),
                     "住址": f"海州市梧桐街{i + 1}号"})
    # 7-9 17 位（format 失败）
    for i, nm in enumerate(("闵涛", "应雪芹", "谈俊杰")):
        rows.append({"姓名": nm, "电话": f"136{i:08d}",
                     "身份证号": make_idcard(_id_base(i + 6), short=True),
                     "住址": f"海州市银杏巷{i + 1}号"})
    # 10-11 合法小写 x 结尾（016 AC-8 误报陷阱；确保校验位恰为 X）
    base_x = "33010219920305438"
    assert idcard_check_digit(base_x) == "X"
    for i, nm in enumerate(("桂文革", "彭少芬")):
        rows.append({"姓名": nm, "电话": f"135{i:08d}",
                     "身份证号": base_x + ("x" if i == 0 else "X"),
                     "住址": f"海州市枫林渡{i + 1}号"})
    # 12-14 双"李强"（强证据全异）+ "李  强"（与李强#1 同一人，005 改值）
    id1 = make_idcard("330102198805124317")
    id2 = make_idcard("330105199203084326")
    rows.append({"姓名": "李强", "电话": "13800001001", "身份证号": id1,
                 "住址": "海州市开发区创业路12号"})
    rows.append({"姓名": "李强", "电话": "13900002002", "身份证号": id2,
                 "住址": "海州市鲤城区学府路4号"})
    rows.append({"姓名": "李  强", "电话": "13800001001", "身份证号": id1,
                 "住址": "海州市开发区创业路12号"})
    # 15 "王银行"（005 AC-5 误报陷阱：电诈词表下不得被组织词误剔）
    rows.append({"姓名": "王银行", "电话": "13800003003",
                 "身份证号": make_idcard("330106197511123301"),
                 "住址": "海州市银行巷3号"})
    # 16-26 正常 11 行（含部分与流水主体重叠的自然人）
    for i, nm in enumerate(SUBJECTS[:11]):
        rows.append({"姓名": nm, "电话": f"139{i + 10:08d}",
                     "身份证号": make_idcard(_id_base(i + 15)),
                     "住址": f"海州市青萍路{i + 20}号"})
    assert len(rows) == 26, len(rows)
    return rows


def _ledger_rows() -> list[dict]:
    # 涉案金额 0.48（万元口径录入而数据元声明单位"元"→ 020 AC-2 量级混用提示）
    return [{"案件编号": "HZ-2025-1103", "案件类别": "杀猪盘",
             "案发日期": "2025-01-03", "涉案金额": 0.48}]


def _composite_rows() -> list[dict]:
    rows = []
    for i in range(15):
        nm = PERSONAL_POOL[(i * 2 + 1) % 64]
        rows.append({"流水号": f"FH2025-{i + 1:03d}",
                     "对方信息": f"{nm}|{make_idcard(_id_base(i + 26))}",
                     "金额": 6800.00, "日期": f"2025-02-{i % 20 + 1:02d}"})
    return rows


def _old_calls_rows() -> list[dict]:
    return [{"主体": SUBJECTS[i % 16], "日期": _DATES[i % 60], "次数": i % 5 + 1}
            for i in range(10)]


TABLES = {
    "银行流水_电诈": ("流水号 主体 对方 金额 日期 卡号 摘要", _flow_rows),
    "通话记录_电诈": ("主体 对端 日期 次数 对端号码_原始", _calls_rows),
    "人员身份_电诈": ("姓名 电话 身份证号 住址", _person_rows),
    "案件台账_电诈": ("案件编号 案件类别 案发日期 涉案金额", _ledger_rows),
    "银行流水_复合列": ("流水号 对方信息 金额 日期", _composite_rows),
    "通话记录_旧版": ("主体 日期 次数", _old_calls_rows),
}


def build_tables() -> dict[str, tuple[list[str], list[dict]]]:
    """{表名: (列名列表, 行 dict 列表)} —— 测试与落盘共用的单一真源。"""
    out = {}
    for name, (colspec, fn) in TABLES.items():
        cols = colspec.split()
        rows = fn()
        for r in rows:
            assert set(r) == set(cols), f"{name} 列不齐: {set(cols) ^ set(r)}"
        out[name] = (cols, rows)
    return out


def dump_fixture(path: Path | None = None) -> Path:
    """把 build_tables() 固化为 tests/fixtures/reqd_case.json（测试真源，入 git）。"""
    import json
    path = path or (Path(__file__).resolve().parent.parent / "tests" / "fixtures"
                    / "reqd_case.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = {name: {"columns": cols, "rows": rows}
              for name, (cols, rows) in build_tables().items()}
    path.write_text(json.dumps({
        "case": "海州11·03刷单返利电诈团伙案（REQ-D 业务测试案例）",
        "generator": "python -m scripts.gen_reqd_case",
        "tables": tables,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main() -> None:
    import sys
    if "--fixture" in sys.argv:
        p = dump_fixture()
        print(f"fixture 已固化：{p}")
        return
    import pyarrow as pa
    import pyarrow.parquet as pq
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, (cols, rows) in build_tables().items():
        cols_data = {c: [r[c] for r in rows] for c in cols}
        pq.write_table(pa.table(cols_data), DATA_DIR / f"{name}.parquet")
        print(f"{name}.parquet: {len(rows)} 行 × {len(cols)} 列")


if __name__ == "__main__":
    main()
