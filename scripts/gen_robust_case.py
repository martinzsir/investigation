"""
scripts/gen_robust_case.py —— default 案件包鲁棒性测试数据生成器。

产出 8 张 parquet（表名/源列与 ontology/default/bindings.json 严格对齐）到 data/test/：
  data/test/银行流水.parquet     85 行（信号 29 + 金额脏值 23 + 日期脏值 13 +
                                        人名噪声 14 + 重复 2 + 币种探针 4）
  data/test/通话记录.parquet     72 行（信号 54 + 脏噪声 18）
  data/test/轨迹出行.parquet     28 行（信号 14 + 脏噪声 14）
  data/test/招投标档案.parquet   13 行（信号  7 + 脏噪声  6）
  data/test/工商信息.parquet     11 行（信号  3 + 脏噪声  8）
  data/test/举报材料.parquet     13 行（信号  3 + 脏噪声 10）
  data/test/公开OSINT.parquet    13 行（信号  4 + 脏噪声  9）
  data/test/人员信息.parquet     22 行（数据元全链路：5 个 DE 合法/脏值/清洗抢救
                                        + NULL 身份剔除/空串保留/重复折叠）

设计原则：
  1. 确定性合成（无随机、无真实 PII；张卫国/李志强/宏业建设/A建材沿用 data/gen_sim.py
     的虚拟案例角色；身份证号为按 GB 11643 MOD11 现算校验位的合成号），
     行数与脏值类型逐项 assert 固定，可回归比对。
  2. "脏而不崩、信号不丢"：脏值集中在噪声行（编译期 TRY_CAST 降级 NULL +
     source_value_cast_failed 诊断；实体名 NULL 行编译期剔除 +
     entity_null_name_dropped 诊断，均不中断 build）；全部侦查信号行使用合法 ISO
     日期/数值，保证 R1/R2/R3/R4/R5/R6 在脏数据洪泛下仍可命中。
  3. 全部列以 VARCHAR 物理类型（数值字面量 + 文本 + NULL）落盘，模拟真实 CSV/Excel
     导出后 parquet 化的最脏形态，类型定型由语义层编译期 TRY_CAST 承担。

数据元口径（v2 起 default 包对象挂全域数据元，合规扫描 core/compliance.scan 有目标）：
  - transaction.amount/date 挂 DE_AMOUNT/DE_DATE，binding transform 在 SQL 投影内
    CAST 前抢救：千分位/货币符金额（"100,000"/"￥95,000.00"）与中文/未补零日期
    （"2021年10月1日"/"2021-9-5"）物化时即为合法值，不再降级 NULL；
  - 各日期对象（call/trackpoint/bid_project/tipoff/osint_article）同挂 DE_DATE +
    transform；未来日期 2099-01-01 CAST 合法但越 DE_DATE.range.max → range_violation；
  - transaction.currency 挂 DE_CURRENCY（源列「币种」optional，主 data/ 缺列降级
    类型化 NULL；本数据集 4 行探针含越界枚举"韩元"）；
  - person_identity 挂 DE_IDCARD/DE_PHONE/DE_GENDER/DE_ID_TYPE/DE_CASE_TYPE：
    string 投影直通，DE clean_rule 在 py 层自动挂接（despace / strip_cc+digits_only），
    带空格身份证、+86/86 前缀与空格/连字符手机入库即归一；校验位错/17 位/越界枚举
    落 compliance_violation（format_mismatch/checksum_failed/enum_unknown）。

脏值矩阵（噪声行覆盖，括号为预期内核行为）：
  金额 decimal : 千分位/货币符(¥￥$€£)（transform strip_thousands+strip_currency
                 CAST 前抢救为合法值）/全角数字(仍 NULL)/科学计数法/"10万元"/"500元"/
                 欧式点号/负数(CAST 成功 → DE_AMOUNT range_violation)/零/
                 空串/None/"无"/纯空格/前后空格/"abc"/超大值/高精度小数/"1e400"(Inf 溢出)
  日期 date    : 中文年月日(transform cn_date_norm 抢救)/斜杠(DuckDB 直解)/点分隔(仍 NULL)/
                 未补零(pad_date/直解可救)/非法(02-30,13月)/紧凑 20211001/两位年/
                 未来日期(CAST 合法 → DE_DATE range_violation)/带时间串(直解)/空串/None/"未知"
  人名 string  : 首尾空格/全角空格(strip 可归一)/内部空格(名称分裂)/括号注释/空串/NULL/
                 SQL 注入探针/零宽字符/超长名/emoji 生僻字/组织词与摘要 token(滤行)
                 entity 型 name_property=NULL 行编译期剔除（entity_null_name_dropped，
                 见下"已修复缺口"①）；空串 "" 是确定值，保留并可空串互联。
  次数 integer : 0/负数/None/空串/"五次"/小数串/超 BIGINT 上界
  timestamp    : 带时区偏移串/非日期文本/None
  duration     : None/负数/"30天"文本/0/超大值
  关系完整性   : optional 列缺失(法人/状态/关联/分管领导/采集时间/保留天数)、
                 INNER JOIN 未命中(被举报人 NULL)、LEFT JOIN 未命中(匿名举报人)、
                 中标方/组织名空格变体导致 lnk_involved_in 降级丢边、完全重复行

已修复缺口（本数据集 v1 压测发现，v2 探针随修复回归）：
  ① [已修复] entity 代理键分配 sorted({name_property 值}) 不容忍 NULL——person/account/
     bid_project 等实体型对象 name_property=NULL 曾使 build 硬失败
     （TypeError: '<' not supported between NoneType and str）。现编译期剔除无身份行
     并落 stats["null_identity"] → run_health kind=entity_null_name_dropped（warning），
     run_all.py / server diagnose worker 均已接线；event 型按行代理键不受影响。
  ② [已修复] R1/R2/R6 等检测器 SQL 曾用裸 CAST(amount AS BIGINT)，遇 Inf/越界 DOUBLE
     （如文本 "1e400" 经 TRY_CAST AS DOUBLE 得 Inf）抛 ConversionException 使整条函数
     崩溃。现统一改 TRY_CAST（functions.json R1/R2/R6、scripts/init_duckdb.py 两处、
     core/gateway.py wan_integer_rate；core/sampling.py Python 侧补 OverflowError），
     越界行按 NULL 被 WHERE 排除、不参与整数判定。本数据集保留 "1e400" 探针防回归。
  语义备注：空串 "" 与 NULL 在链接层不同——空串会相互 JOIN（被举报人="" 会挂到 UNION
  产生的空名 person，形成"无名实体"互联边），NULL 不匹配；实体名空串为有意保留探针，
  解读边数时需区分二者。

信号清单（与 data/gen_sim.py 同源，必须存活）：
  S1 工资 20 笔（2019-2023 季初，非整数金额，对方财政局，R1 对照样本）
  S2 张卫国 7 笔整万元现金存入（中标公示日 +[7,5,18,10,8,13,7] 天，R1+R6）
  S3 过桥两笔：宏业建设→A建材 460 万、A建材→张卫国配偶 170 万（R2）
  S4 张卫国→李志强 42 通 + 4 个低频对照对（R3：42 ≥ 2×中位 3）
  S5 7 个项目中标次日张卫国/李志强同地点（同日或邻日，R4 co_located）
  S6 工商登记 3 行（R5：宏业法人李志强、A建材法人李志强妻弟、张卫国配偶关联）
  S7 举报 3 条（张卫国×2、李志强×1；匿名/实名各有，tipoff 双链接）
  S8 OSINT 4 篇（宏业建设/A建材/张卫国/李志强，osint_mentions）

用法：
  /root/.venvs/inves/bin/python -m scripts.gen_robust_case
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "test"

# 7 个虚拟工程项目及中标公示日（与 data/gen_sim.py 一致）
PROJECTS = [
    ("滨江路改造", "2019-06-18"), ("城东管网", "2020-03-25"), ("安置房一期", "2020-11-10"),
    ("桥梁加固", "2021-09-02"), ("市政绿化", "2022-04-20"), ("智慧交通", "2022-12-05"),
    ("安置房二期", "2023-08-15"),
]
BID_OFFSETS = [7, 5, 18, 10, 8, 13, 7]
AMT = 100000  # 整数现金存入每笔 10 万元

# 安全探针（值层字符串，验证任何环节都不会被当作 SQL 执行；纯合成无害内容）
SQLI_1 = "'; DROP TABLE obj_person; --"
SQLI_2 = "' OR '1'='1"
LONG_NAME = "超长名称测试" * 25                       # 150 字
LONG_TEXT = "举报内容超长文本压测，重复段落。" * 25   # 375 字

# 日期脏值矩阵（default 管道挂 DE_DATE transform=cn_date_norm+pad_date，
# 中文年月日/未补零在 TRY_CAST 前被抢救；其余预期以实际 DuckDB 行为为准）
DIRTY_DATES = [
    "2021年10月1日",   # 中文格式（cn_date_norm → 2021-10-01，transform 抢救）
    "2021/10/01",      # 斜杠（DuckDB TRY_CAST 直解 → 2021-10-01）
    "2021.10.01",      # 点分隔（→ NULL）
    "2021-9-5",        # 未补零（pad_date → 2021-09-05，可抢救样本）
    "2021-02-30",      # 非法日期（→ NULL）
    "2099-01-01",      # 未来日期（CAST 成功，合规 range_violation 样本）
    "",                # 空串（→ NULL）
    None,              # NULL
    "未知",            # 文本（→ NULL）
    "2021-10-01 09:30:00",  # 带时间（CAST AS DATE 成功 → 2021-10-01，可抢救样本）
    "21-10-01",        # 两位年（歧义 → NULL）
    "2021/13/01",      # 非法月（→ NULL）
    "20211001",        # 紧凑格式（→ NULL）
]


# ---------------- 银行流水（日期/主体/对方/金额/币种）85 行 ----------------

# GB 11643-1999 MOD11-2 身份证校验位（与 core/data_elements.py 同算法，本地自包含）
_IDCARD_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDCARD_CHECK = "10X98765432"


def _id18(code17: str) -> str:
    """17 位本体（6 位地址码 + 8 位出生日期 + 3 位顺序码）→ 带合法校验位的 18 位身份证。"""
    assert len(code17) == 17 and code17.isdigit()
    return code17 + _IDCARD_CHECK[
        sum(int(c) * w for c, w in zip(code17, _IDCARD_WEIGHTS)) % 11]


def _id18_bad_checksum(code17: str) -> str:
    """同本体但校验位故意写错（格式合法、checksum_failed 样本）。"""
    good = _id18(code17)
    for ch in "0123456789X":
        if ch != good[17]:
            return good[:17] + ch
    raise AssertionError("unreachable")


# 带内部空格的合法身份证（despace 抢救样本：去空白后校验位合法）
_ID_SPACED = _id18("31010419900101123")
IDCARD_SPACED = f"{_ID_SPACED[:6]} {_ID_SPACED[6:14]} {_ID_SPACED[14:]}"


def _flow_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S1：工资 20 笔（非整数，固定按季发放；R1 对照样本）----
    for y in range(2019, 2024):
        for m in (1, 4, 7, 10):
            rows.append({"日期": f"{y}-{m:02d}-05", "主体": "张卫国",
                         "对方": "财政局", "金额": 18532 + m})
    assert len(rows) == 20

    # ---- S2：季末/公示窗口整数现金存入 7 笔（R1 + R6 信号，金额日期必须合法）----
    for (name, bid), off in zip(PROJECTS, BID_OFFSETS):
        d = pd.Timestamp(bid) + pd.DateOffset(days=off)
        rows.append({"日期": d.strftime("%Y-%m-%d"), "主体": "张卫国",
                     "对方": "现金存入", "金额": AMT})

    # ---- S3：第三方过桥两笔（R2：整数大额单向链）----
    rows.append({"日期": "2021-10-01", "主体": "宏业建设",
                 "对方": "A建材", "金额": 4600000})
    rows.append({"日期": "2021-11-15", "主体": "A建材",
                 "对方": "张卫国配偶", "金额": 1700000})
    assert len(rows) == 29

    # ---- D-AMT：金额脏值 23 行（日期/主体合法，隔离金额列行为）----
    dirty_amounts = [
        "100,000",            # 千分位 → strip_thousands 抢救 100000
        "1,280.50",           # 千分位小数 → 抢救 1280.5
        "￥95,000.00",        # 全角货币符 + 千分位 → 抢救 95000
        "¥500",               # 半角货币符 → strip_currency 抢救 500
        "$1,200.00",          # 美元符 + 千分位 → 抢救 1200
        "€300",               # 欧元符 → 抢救 300
        "£200",               # 英镑符 → 抢救 200
        "１０００００",        # 全角数字 → NULL（无全角归一 op）
        "1.28e3",             # 科学计数法（DOUBLE 可解析 → 1280.0，可抢救样本）
        "10万元",             # 中文单位 → NULL
        "500元",              # 中文后缀 → NULL
        -500,                 # 负数（CAST 成功 → DE_AMOUNT range_violation 样本）
        0,                    # 零值（合法边界）
        "",                   # 空串 → NULL
        None,                 # NULL
        "无",                 # 占位文本 → NULL
        "   ",                # 纯空格 → strip_currency 剥成空串 → NULL
        "  8600  ",           # 前后空格 → strip_currency 去空白 → 8600
        "abc",                # 纯文本 → NULL
        999999999999.99,      # 超大金额（合法但逼近，孤峰样本；仍在 BIGINT 内）
        18532.123456,         # 高精度小数（DOUBLE 截断样本）
        "1.000.000",          # 欧式点号千分位（多点号 → NULL）
        "1e400",              # Inf 溢出：TRY_CAST AS DOUBLE=Inf → 检测器 TRY_CAST BIGINT 降级 NULL 排除（防回归，缺口②）
    ]
    assert len(dirty_amounts) == 23
    for i, amt in enumerate(dirty_amounts):
        rows.append({"日期": f"2022-05-{(i % 28) + 1:02d}",
                     "主体": ("孟繁星" if i % 2 else "华清越"),
                     "对方": "测试商户", "金额": amt})

    # ---- D-DATE：日期脏值 13 行（金额/主体合法，隔离日期列 CAST 行为）----
    for i, d in enumerate(DIRTY_DATES):
        rows.append({"日期": d, "主体": "樊皓宁",
                     "对方": "戴若曦", "金额": 6200 + i})

    # ---- D-NAME：主体/对方人名脏值 14 行（非整数合法金额，避免在变体名下制造 R1/R6 信号）----
    name_pairs = [
        ("  张卫国  ", "李志强"),          # 首尾 ASCII 空格 → person strip 归一并入张卫国
        ("张卫国　", "李志强"),            # 全角尾空格 → Python strip 去 Unicode 空白，可归一
        ("张 卫国", "李志强"),             # 内部空格 → strip 不可救，名称分裂实体
        ("李志强（宏业法人）", "张卫国"),  # 括号注释（person 链无 strip_paren）→ 分裂噪声
        ("张卫国\u200b", "李志强"),        # 零宽字符 → 分裂实体
        ("王𠀀", "温书豪"),               # 扩展 B 区生僻字（4 字节 UTF-8 边界）
        ("测试😀表情", "温书豪"),          # emoji（UTF-8 多字节边界）
        ("财政局", "戴若曦"),              # 组织词（财政/局）→ exclude_org_tokens 滤出 person
        ("宏业建设有限公司", "A建材"),     # 组织词（公司/建设）→ 滤出 person
        (LONG_NAME, "温书豪"),             # 超长人名
        (SQLI_1, "温书豪"),                # SQL 注入探针（值层，必须原样落库不执行）
        ("温书豪", SQLI_2),                # SQL 注入探针（对方侧）
        (None, "温书豪"),                  # 主体 NULL → entity 无身份行编译期剔除（entity_null_name_dropped）
        ("郁晨曦", None),                  # 对方 NULL → account 侧无身份行剔除（链接 LEFT JOIN 保留边、外键 NULL）
    ]
    assert len(name_pairs) == 14
    for i, (subj, peer) in enumerate(name_pairs):
        rows.append({"日期": f"2022-06-{(i % 28) + 1:02d}",
                     "主体": subj, "对方": peer, "金额": 8888 + i * 0.01})

    # ---- DUP：完全重复行 2 行（测 COUNT 聚合膨胀/去重鲁棒性，非整数不污染信号）----
    dup = {"日期": "2022-07-08", "主体": "童千帆", "对方": "米建勋", "金额": 3210.55}
    rows.append(dict(dup))
    rows.append(dict(dup))
    assert len(rows) == 81

    # ---- D-CUR：币种列（DE_CURRENCY，源列「币种」optional；主 data/ 缺列降级 NULL）----
    # 81 行既有信号/噪声默认人民币；4 行探针：美元/港币合法、"韩元"越界枚举、NULL 跳过。
    # 金额用 .5 小数且远离中标窗口，避免制造 R1/R2/R6 噪声。
    for r in rows:
        r["币种"] = "人民币"
    cur_rows = [
        ("2023-03-06", 1200.5, "美元"),
        ("2023-03-07", 900.5, "港币"),
        ("2023-03-08", 500.5, "韩元"),    # 不在 DE_CURRENCY 枚举 → enum_unknown
        ("2023-03-09", 300.5, None),
    ]
    for d, amt, cur in cur_rows:
        rows.append({"日期": d, "主体": "鲁以墨", "对方": "外币兑换点",
                     "金额": amt, "币种": cur})

    assert len(rows) == 85, len(rows)
    return rows


# ---------------- 通话记录（主体/对端/日期/次数）72 行 ----------------

def _calls_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S4a：张卫国→李志强 42 通（2020 上半年；R3 头部对端，每行次数=1）----
    for i in range(42):
        month = (i % 6) + 1
        day = (i % 27) + 1
        rows.append({"主体": "张卫国", "对端": "李志强",
                     "日期": f"2020-{month:02d}-{day:02d}", "次数": 1})

    # ---- S4b：低频对照对 12 通（中位数 ≈3，42 ≥ 2×3 命中 R3）----
    for _ in range(3):
        rows.append({"主体": "张卫国", "对端": "陈会计", "日期": "2020-02-14", "次数": 1})
    for _ in range(2):
        rows.append({"主体": "张卫国", "对端": "王主任", "日期": "2020-04-09", "次数": 1})
    for _ in range(4):
        rows.append({"主体": "李志强", "对端": "刘工", "日期": "2020-05-11", "次数": 1})
    for _ in range(3):
        rows.append({"主体": "孟繁星", "对端": "邵一鸣", "日期": "2020-06-18", "次数": 1})
    assert len(rows) == 54

    # ---- D-TIMES：次数脏值 7 行（TRY_CAST BIGINT 行为隔离）----
    dirty_times = [
        0,                          # 零次（合法边界）
        -3,                         # 负数（CAST 成功，业务异常）
        None,                       # NULL
        "",                         # 空串 → NULL
        "五次",                     # 中文数字 → NULL
        "3.9",                      # 小数串 → BIGINT 失败 → NULL
        "9999999999999999999",      # 超 BIGINT 上界 → NULL
    ]
    for i, t in enumerate(dirty_times):
        rows.append({"主体": "阮青山", "对端": "岳鸣谦",
                     "日期": f"2020-07-{i + 1:02d}", "次数": t})

    # ---- D-DATE：日期脏值 5 行（次数合法）----
    for i, d in enumerate(DIRTY_DATES[:5]):
        rows.append({"主体": "闵子腾", "对端": "任慕白",
                     "日期": d, "次数": (i % 5) + 1})

    # ---- D-NAME：通话主体/对端脏值 4 行 ----
    rows.append({"主体": "  张卫国  ", "对端": "李志强", "日期": "2020-08-01", "次数": 2})
    rows.append({"主体": "廖俊驰", "对端": SQLI_1, "日期": "2020-08-02", "次数": 1})
    rows.append({"主体": None, "对端": "葛天菲", "日期": "2020-08-03", "次数": 1})   # NULL 名 → 无身份行剔除
    rows.append({"主体": "葛天菲", "对端": "", "日期": "2020-08-04", "次数": 1})   # 空串保留：空名 person 互联探针

    # ---- DUP：完全重复通话 2 行 ----
    dup = {"主体": "闵子腾", "对端": "任慕白", "日期": "2020-09-09", "次数": 6}
    rows.append(dict(dup))
    rows.append(dict(dup))

    assert len(rows) == 72, len(rows)
    return rows


# ---------------- 轨迹出行（主体/地点/日期）28 行 ----------------

def _track_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S5：7 个项目中标次日张卫国到场；李志强同日(前3)/邻日(后4)同地 → R4 7 对 ----
    for i, (name, bid) in enumerate(PROJECTS):
        d_zwg = pd.Timestamp(bid) + pd.DateOffset(days=1)
        d_lzq = d_zwg if i < 3 else d_zwg + pd.DateOffset(days=1 if i % 2 else -1)
        loc = f"项目{name}"
        rows.append({"主体": "张卫国", "地点": loc, "日期": d_zwg.strftime("%Y-%m-%d")})
        rows.append({"主体": "李志强", "地点": loc, "日期": d_lzq.strftime("%Y-%m-%d")})
    assert len(rows) == 14

    # ---- D-DATE：轨迹日期脏值 5 行（地点/主体合法）----
    for i, d in enumerate(DIRTY_DATES[5:10]):
        rows.append({"主体": "温书豪", "地点": "海州市客运站", "日期": d})

    # ---- D-LOC：地点脏值 4 行 ----
    rows.append({"主体": "童千帆", "地点": None, "日期": "2022-08-01"})
    rows.append({"主体": "米建勋", "地点": "", "日期": "2022-08-02"})
    rows.append({"主体": "阮青山", "地点": SQLI_2, "日期": "2022-08-03"})
    rows.append({"主体": "岳鸣谦", "地点": f"海州市图书馆{LONG_NAME}", "日期": "2022-08-04"})

    # ---- D-NAME：主体脏值 3 行 ----
    rows.append({"主体": "  张卫国  ", "地点": "海州市政府", "日期": "2022-09-01"})
    rows.append({"主体": "张 卫国", "地点": "项目滨江路改造（北门）", "日期": "2019-06-20"})
    # 上一行：地点别名（带后缀）不与信号地点精确匹配，名称又分裂 → 纯噪声，不得误命中 R4
    rows.append({"主体": None, "地点": "海州市火车站", "日期": "2022-09-02"})  # NULL 名 → 无身份行剔除

    # ---- DUP：完全重复轨迹 2 行 ----
    dup = {"主体": "孟繁星", "地点": "海州市第一医院", "日期": "2022-10-10"}
    rows.append(dict(dup))
    rows.append(dict(dup))

    assert len(rows) == 28, len(rows)
    return rows


# ---------------- 招投标档案（项目/中标方/中标公示日/分管领导）13 行 ----------------

def _bid_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S6：7 个项目信号行。"桥梁加固"中标方故意带首尾空格——
    # lnk_involved_in 为 INNER JOIN obj_org（raw_name 精确匹配），该边降级丢失；
    # lnk_time_window 不依赖中标方，R6 碰撞不受影响。----
    for i, (name, bid) in enumerate(PROJECTS):
        winner = "  宏业建设  " if name == "桥梁加固" else "宏业建设"
        rows.append({"项目": name, "中标方": winner, "中标公示日": bid,
                     "分管领导": "张卫国"})
    assert len(rows) == 7

    # ---- D-PUBDATE：公示日脏值 2 行（time_window 连不上边，不崩）----
    rows.append({"项目": "测试路改造A", "中标方": "宏业建设",
                 "中标公示日": "2022年5月1日", "分管领导": "张卫国"})
    rows.append({"项目": "测试路改造B", "中标方": "宏业建设",
                 "中标公示日": "2022-02-30", "分管领导": None})

    # ---- D-OPT：分管领导缺失（optional 列）2 行 ----
    rows.append({"项目": "测试路改造C", "中标方": "宏业建设",
                 "中标公示日": "2099-01-01", "分管领导": None})
    rows.append({"项目": "测试路改造D", "中标方": "宏业建设",
                 "中标公示日": "2023-05-05", "分管领导": ""})

    # ---- D-TITLE/WINNER：项目名/中标方脏值 2 行 ----
    rows.append({"项目": SQLI_1, "中标方": "某投标单位",
                 "中标公示日": "2023-06-06", "分管领导": "张卫国"})
    rows.append({"项目": None, "中标方": "",
                 "中标公示日": None, "分管领导": None})  # 项目 NULL → bid_project 无身份行剔除（INNER 链接自然丢边）

    assert len(rows) == 13, len(rows)
    return rows


# ---------------- 工商信息（主体/法人/状态/关联）11 行 ----------------

def _org_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S7：R5 利益关联信号 3 行（与 gen_sim 完全一致，主通道不得污染）----
    rows.append({"主体": "宏业建设", "法人": "李志强", "状态": "存续", "关联": ""})
    rows.append({"主体": "A建材", "法人": "李志强妻弟", "状态": "存续", "关联": ""})
    rows.append({"主体": "张卫国配偶", "法人": "", "状态": "-", "关联": "张卫国"})

    # ---- D-OPT：法人/状态/关联缺失（均为 optional_columns）3 行 ----
    rows.append({"主体": "海州城投集团", "法人": None, "状态": None, "关联": None})
    rows.append({"主体": "广泰商贸", "法人": "", "状态": "", "关联": ""})
    rows.append({"主体": "  城投集团  ", "法人": "陈会计", "状态": "存续", "关联": ""})
    # 上一行：主体首尾空格（org binding 无 strip）→ 与"海州城投集团"成为两个实体，
    # 测组织名称分裂（不影响信号通道）。

    # ---- D-STATUS：枚举外/异常状态值 2 行 ----
    rows.append({"主体": "旧桥施工队", "法人": "王主任", "状态": "吊销", "关联": ""})
    rows.append({"主体": "顺风运输队", "法人": "刘工",
                 "状态": "在业（列入经营异常名录）", "关联": ""})

    # ---- D-MISC：SQL 探针 / 超长主体 2 行 ----
    rows.append({"主体": SQLI_2, "法人": "葛天菲", "状态": "存续", "关联": ""})
    rows.append({"主体": LONG_NAME, "法人": "郁晨曦", "状态": "注销", "关联": ""})

    # ---- DUP：完全重复登记 1 行（宏业建设重复登记，org 实体按名去重不膨胀）----
    rows.append({"主体": "宏业建设", "法人": "李志强", "状态": "存续", "关联": ""})

    assert len(rows) == 11, len(rows)
    return rows


# ---------------- 举报材料（举报日期/分类/被举报人/举报人/内容）12 行 ----------------

def _tipoff_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S8：信号举报 3 条（tipoff_targets_person INNER + tipoff_from_reporter LEFT）----
    rows.append({"举报日期": "2022-01-10", "分类": "经济类", "被举报人": "张卫国",
                 "举报人": "匿名", "内容": "匿名举报：张卫国收受宏业李志强现金约120万元"})
    rows.append({"举报日期": "2022-03-22", "分类": "职务类", "被举报人": "张卫国",
                 "举报人": "内部职工",
                 "内容": "反映张卫国在安置房一期招投标中为宏业建设提前透露底标"})
    rows.append({"举报日期": "2023-02-15", "分类": "经济类", "被举报人": "李志强",
                 "举报人": "同行",
                 "内容": "A建材（李志强妻弟名下）收到宏业大额转账后，当日转给张卫国配偶"})

    # ---- D-DATE：举报日期脏值 3 条 ----
    rows.append({"举报日期": "2022年3月1日", "分类": "经济类",
                 "被举报人": "张卫国", "举报人": "匿名", "内容": "中文日期举报压测"})
    rows.append({"举报日期": "2099-01-01", "分类": "职务类",
                 "被举报人": "李志强", "举报人": "内部职工", "内容": "未来日期举报压测"})
    rows.append({"举报日期": None, "分类": "经济类",
                 "被举报人": "张卫国", "举报人": "匿名", "内容": "空日期举报压测"})

    # ---- D-REL：关系完整性——被举报人 NULL / 举报人 NULL 2 条 ----
    # NULL 被举报人：无身份行不入 obj_person，tipoff_targets INNER JOIN 真正丢边（不留边）；
    # 空串互联语义由 D-CONTENT 段举报人 "" 保留覆盖（见文件头"已修复缺口"语义备注）。
    rows.append({"举报日期": "2022-08-08", "分类": "",
                 "被举报人": None, "举报人": "匿名", "内容": "被举报人 NULL（无身份，INNER 丢边）"})
    rows.append({"举报日期": "2022-09-09", "分类": "其他类别",
                 "被举报人": "张卫国", "举报人": None, "内容": "无举报人（reporter LEFT JOIN 容忍）"})

    # ---- D-CONTENT：内容脏值 3 条 ----
    rows.append({"举报日期": "2022-10-10", "分类": "经济类",
                 "被举报人": "李志强", "举报人": "", "内容": None})
    rows.append({"举报日期": "2022-11-11", "分类": "经济类",
                 "被举报人": "张卫国", "举报人": "匿名", "内容": LONG_TEXT})
    rows.append({"举报日期": "2022-12-12", "分类": None,
                 "被举报人": "张卫国", "举报人": SQLI_1,
                 "内容": "多行文本\n第二行\n含引号'与反斜杠\\以及探针 " + SQLI_2})

    # ---- DUP：完全重复举报 1 条 ----
    rows.append({"举报日期": "2023-01-05", "分类": "经济类",
                 "被举报人": "李志强", "举报人": "同行",
                 "内容": "重复举报压测（与下一条完全相同）"})
    rows.append({"举报日期": "2023-01-05", "分类": "经济类",
                 "被举报人": "李志强", "举报人": "同行",
                 "内容": "重复举报压测（与下一条完全相同）"})

    assert len(rows) == 13, len(rows)
    return rows


# ---------------- 公开OSINT（主体/公开信息/发布日期/来源/采集时间/保留天数）13 行 ----

def _osint_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- S9：信号文章 4 篇（osint_mentions；时间/timestamp/duration 全合法）----
    rows.append({"主体": "宏业建设", "公开信息": "招投标公示一致", "发布日期": "2020-04-10",
                 "来源": "公共资源交易网", "采集时间": "2020-04-11 08:30:00", "保留天数": 1825})
    rows.append({"主体": "A建材", "公开信息": "经营状态正常", "发布日期": "2021-06-21",
                 "来源": "企业信用公示", "采集时间": "2021-06-22 09:15:00", "保留天数": 1825})
    rows.append({"主体": "张卫国", "公开信息": "分管招投标", "发布日期": "2019-03-01",
                 "来源": "政府官网公示", "采集时间": "2019-03-02 10:00:00", "保留天数": 3650})
    rows.append({"主体": "李志强", "公开信息": "宏业法人", "发布日期": "2019-08-15",
                 "来源": "企业信用公示", "采集时间": "2019-08-16 14:20:00", "保留天数": 3650})

    # ---- D-DATE：发布日期脏值 3 篇 ----
    rows.append({"主体": "宏业建设", "公开信息": "中文日期文章", "发布日期": "2021年9月1日",
                 "来源": "公共资源交易网", "采集时间": "2021-09-02 08:00:00", "保留天数": 365})
    rows.append({"主体": "A建材", "公开信息": "非法日期文章", "发布日期": "2021-02-30",
                 "来源": "企业信用公示", "采集时间": None, "保留天数": None})
    rows.append({"主体": "张卫国", "公开信息": "未来日期文章", "发布日期": "2099-01-01",
                 "来源": "政府官网公示", "采集时间": "2026-09-01 12:00:00", "保留天数": 0})

    # ---- D-TS：采集时间 timestamp 脏值 2 篇（CAST TIMESTAMP 行为隔离）----
    rows.append({"主体": "李志强", "公开信息": "带时区时间戳", "发布日期": "2022-01-01",
                 "来源": "企业信用公示",
                 "采集时间": "2022-01-01T10:00:00+08:00",   # 带偏移 → TIMESTAMP 失败 → NULL
                 "保留天数": 30})
    rows.append({"主体": "李志强", "公开信息": "非时间文本", "发布日期": "2022-02-02",
                 "来源": "网络转载", "采集时间": "not-a-timestamp", "保留天数": -5})

    # ---- D-DUR：保留天数脏值 2 篇 ----
    rows.append({"主体": "宏业建设", "公开信息": "文本保留天数", "发布日期": "2022-03-03",
                 "来源": None, "采集时间": "2022-03-04 08:00:00", "保留天数": "30天"})
    rows.append({"主体": "A建材", "公开信息": "超大保留天数", "发布日期": "2022-04-04",
                 "来源": "", "采集时间": "2022-04-05 08:00:00",
                 "保留天数": 9999999999999999999})  # 超 INTEGER 上界 → NULL

    # ---- D-TEXT：正文/主体脏值 2 篇 ----
    rows.append({"主体": "  张卫国  ", "公开信息": LONG_TEXT, "发布日期": "2022-05-05",
                 "来源": SQLI_1, "采集时间": "2022-05-06 08:00:00", "保留天数": 365})
    rows.append({"主体": None, "公开信息": None, "发布日期": None,
                 "来源": None, "采集时间": None, "保留天数": None})  # 全空篇：event 型按行代理键容忍 NULL，行保留

    assert len(rows) == 13, len(rows)
    return rows


# ---------------- 人员信息（姓名/证件类型/身份证号/手机号/性别/案件类别）22 行 ----------------
# 数据元全链路压测表：5 个 DE 合法值 + 脏值 + py 层 clean_rule 抢救样本 +
# NULL 身份剔除/空串保留/重复折叠。全部身份证为 MOD11 现算合成号，无真实 PII。
# 实体型按 raw_name 分配代理键：同名重复行折叠（第 19 行 = 第 1 行），
# NULL 姓名行编译期剔除（null_identity），空串 "" 姓名保留为无名实体。

def _person_rows() -> list[dict]:
    rows: list[dict] = [
        # ---- 合法建档（合规扫描零违规；张卫国/李志强 沿用虚拟案例角色）----
        {"姓名": "张卫国", "证件类型": "居民身份证",
         "身份证号": _id18("31010119850312001"), "手机号": "13800138000",
         "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "李志强", "证件类型": "居民身份证",
         "身份证号": _id18("31010119791125032"), "手机号": "13900139000",
         "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "王雅琴", "证件类型": "居民身份证",
         "身份证号": _id18("31010219900708124"), "手机号": "13712345678",
         "性别": "女", "案件类别": "洗钱"},
        {"姓名": "陈会计", "证件类型": "居民身份证",
         "身份证号": _id18("31010319881201336"), "手机号": "13655556666",
         "性别": "女", "案件类别": "贪污贿赂"},
        # 护照号填进身份证列 → format_mismatch + checksum_failed 双违规码
        {"姓名": "林慕鸥", "证件类型": "护照",
         "身份证号": "G12345678", "手机号": "13500001111",
         "性别": "男", "案件类别": "电信网络诈骗"},
        # 通行证人员无身份证号（NULL 合规跳过，不计违规）
        {"姓名": "葛天菲", "证件类型": "港澳居民来往内地通行证",
         "身份证号": None, "手机号": "13122223333",
         "性别": "女", "案件类别": "组织领导传销"},
        # ---- py 层 clean_rule 抢救样本（入库即合法，合规零违规）----
        {"姓名": "温书豪", "证件类型": "居民身份证",       # despace：带空格身份证
         "身份证号": IDCARD_SPACED,
         "手机号": "13877778888", "性别": "男", "案件类别": "敲诈勒索"},
        {"姓名": "岳鸣谦", "证件类型": "居民身份证",       # digits_only：空格手机
         "身份证号": _id18("31011619910203567"),
         "手机号": " 138 0013 8000 ", "性别": "男", "案件类别": "敲诈勒索"},
        {"姓名": "阮青山", "证件类型": "居民身份证",       # strip_cc：86 前缀手机
         "身份证号": _id18("31010519820415678"),
         "手机号": "8613755556666", "性别": "男", "案件类别": "洗钱"},
        # ---- 违规探针（合规扫描逐码命中）----
        {"姓名": "米建勋", "证件类型": "居民身份证",       # checksum_failed
         "身份证号": _id18_bad_checksum("31010619920304456"),
         "手机号": "13911112222", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "童千帆", "证件类型": "居民身份证",       # 17 位：format+checksum
         "身份证号": "3101011990010112",
         "手机号": "13822223333", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "闵子腾", "证件类型": "居民身份证",       # 手机 5 位：format_mismatch
         "身份证号": _id18("31010719930722789"),
         "手机号": "12345", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "戴若曦", "证件类型": "居民身份证",       # 性别越界：enum_unknown
         "身份证号": _id18("31010819861201017"),
         "手机号": "13833334444", "性别": "中性", "案件类别": "贪污贿赂"},
        {"姓名": "樊皓宁", "证件类型": "驾驶证",           # 证件类型越界：enum_unknown
         "身份证号": _id18("31010919850506234"),
         "手机号": "13844445555", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "华清越", "证件类型": "居民身份证",       # 案件类别越界：enum_unknown
         "身份证号": _id18("31011019940809567"),
         "手机号": "13855556666", "性别": "女", "案件类别": "盗窃罪"},
        # ---- NULL 空值行（NULL 值合规扫描跳过）----
        {"姓名": "孟繁星", "证件类型": "居民身份证",
         "身份证号": None, "手机号": None,
         "性别": "未知", "案件类别": None},
        # ---- 身份探针：NULL 剔除 / 空串保留 / 重复折叠 ----
        {"姓名": None, "证件类型": "居民身份证",          # NULL 名 → 无身份行剔除
         "身份证号": _id18("31011219880303112"),
         "手机号": "13866667777", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "", "证件类型": "居民身份证",            # 空串名保留（无名实体）
         "身份证号": _id18("31011319901224345"),
         "手机号": "13500005555", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "张卫国", "证件类型": "居民身份证",       # 与首行完全相同 → 实体折叠
         "身份证号": _id18("31010119850312001"), "手机号": "13800138000",
         "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "廖俊驰", "证件类型": "居民身份证",       # digits_only：连字符手机
         "身份证号": _id18("31011419950617456"),
         "手机号": "139-0013-9001", "性别": "男", "案件类别": "贪污贿赂"},
        {"姓名": "任慕白", "证件类型": "居民身份证",       # strip_cc：+86 前缀手机
         "身份证号": _id18("31011519870928789"),
         "手机号": "+8613812345678", "性别": "女", "案件类别": "洗钱"},
        {"姓名": SQLI_2, "证件类型": "居民身份证",        # SQL 注入探针（值层原样落库）
         "身份证号": _id18("31011719920408890"),
         "手机号": "13611112222", "性别": "男", "案件类别": "洗钱"},
    ]
    assert len(rows) == 22, len(rows)
    return rows


TABLES = {
    "银行流水": ("日期 主体 对方 金额 币种", _flow_rows),
    "通话记录": ("主体 对端 日期 次数", _calls_rows),
    "轨迹出行": ("主体 地点 日期", _track_rows),
    "招投标档案": ("项目 中标方 中标公示日 分管领导", _bid_rows),
    "工商信息": ("主体 法人 状态 关联", _org_rows),
    "举报材料": ("举报日期 分类 被举报人 举报人 内容", _tipoff_rows),
    "公开OSINT": ("主体 公开信息 发布日期 来源 采集时间 保留天数", _osint_rows),
    "人员信息": ("姓名 证件类型 身份证号 手机号 性别 案件类别", _person_rows),
}


def build_tables() -> dict[str, tuple[list[str], list[dict]]]:
    """{表名: (列名列表, 行 dict 列表)} —— 测试可直接 import 的单一真源。"""
    out: dict[str, tuple[list[str], list[dict]]] = {}
    for name, (colspec, fn) in TABLES.items():
        cols = colspec.split()
        rows = fn()
        for r in rows:
            assert set(r) == set(cols), f"{name} 列不齐: 缺{set(cols) - set(r)} 多{set(r) - set(cols)}"
        out[name] = (cols, rows)
    return out


def _cell(v):
    """None 保留为 null；数值转字面字符串；文本原样。

    全部列物理落为 VARCHAR（pyarrow string），模拟真实 CSV/Excel 导出后
    parquet 化的最脏文本形态；类型定型由语义层编译期 TRY_CAST 承担。
    """
    if v is None:
        return None
    return v if isinstance(v, str) else str(v)


def main() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, (cols, rows) in build_tables().items():
        schema = pa.schema([pa.field(c, pa.string()) for c in cols])
        table = pa.table({c: [_cell(r[c]) for r in rows] for c in cols}, schema=schema)
        pq.write_table(table, DATA_DIR / f"{name}.parquet")
        total += len(rows)
        print(f"{name}.parquet: {len(rows)} 行 × {len(cols)} 列")
    print(f"鲁棒性测试数据已生成：{DATA_DIR}（共 {total} 行，8 张表）")


if __name__ == "__main__":
    main()
