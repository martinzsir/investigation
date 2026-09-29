"""
core/data_elements.py —— 数据元标准注册表（REQ-D-001）。

数据元回答"这个值该长什么样"（类型/长度/格式/校验位/敏感度/推荐清洗）。
声明在 ontology/<pack>/data_elements.json；校验算法（checksum）在此注册：
未知算法装载期硬失败（fail-closed 不放宽，REQ-D-001 AC-2）。零第三方依赖。

词汇归一算法（norm_vocab_alias）也在此注册：声明层（ontology_loader 建
别名索引、枚举标签/别名碰撞检查）与代码层（core/item.py 归一化查询）必须
共用同一份实现——各写一份的下场是"声明说没抢名、查询却命中另一条"，
两类表对不上时没有任何报错。
"""
from __future__ import annotations

from typing import Callable

CHECKSUM_ALGOS: dict[str, Callable[[str], bool]] = {}


def norm_vocab_alias(s) -> str:
    """词汇归一化比较键：去空格、下划线、连字符并转小写。

    为什么不是简单 lower()：正兵可能填 "IMEI"、"imei"、"I-M-E-I"，
    这类输入若识别不了会落进 None——None 是"识别不了"，不是"无凭证"，
    两者绝不能混（无凭证是合法登记，识别不了是数据错误）。

    声明层与查询层共用本函数：枚举 label/别名建索引时归一化，查询输入
    也归一化，索引与查询永远同口径。
    """
    return str(s or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def register_checksum(name: str, fn: Callable[[str], bool]) -> None:
    """注册校验算法；同名重复注册硬失败（算法名唯一）。"""
    if name in CHECKSUM_ALGOS:
        raise ValueError(f"checksum 算法重复注册：'{name}'（REQ-D-001：算法名唯一）")
    CHECKSUM_ALGOS[name] = fn


_IDCARD_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_IDCARD_CHECK = "10X98765432"


def checksum_idcard_mod11(v: str) -> bool:
    """GB 11643-1999 公民身份号码校验位（ISO 7064:1983 MOD 11-2）。"""
    v = (v or "").strip().upper()
    if len(v) != 18 or not v[:17].isdigit():
        return False
    s = sum(int(c) * w for c, w in zip(v[:17], _IDCARD_WEIGHTS))
    return _IDCARD_CHECK[s % 11] == v[17]


register_checksum("idcard_mod11", checksum_idcard_mod11)


def checksum_luhn(v: str) -> bool:
    """Luhn 算法（ISO/IEC 7812-1）：银行卡号/IMEI 等校验位验证。"""
    v = (v or "").strip()
    if not v.isdigit() or len(v) < 12:
        return False
    total = 0
    for i, ch in enumerate(reversed(v)):
        d = int(ch)
        if i % 2 == 1:            # 从右起偶数位（索引 1,3,...）加倍
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


register_checksum("luhn", checksum_luhn)


# GB 32100-2015 统一社会信用代码：18 位，字符集 31 个（不含 I、O、S、V、Z）。
_CREDIT_CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_CREDIT_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def checksum_credit_code_mod31(v: str) -> bool:
    """GB 32100-2015 统一社会信用代码校验位（MOD 31）。

    与身份证同属"格式合法但校验位错"的脏数据识别：校验失败只标未核实，
    不硬失败（D10）。字符集外的字符一律判非法（含易混字符 I/O/S/V/Z）。
    """
    v = (v or "").strip().upper()
    if len(v) != 18:
        return False
    try:
        vals = [_CREDIT_CHARSET.index(c) for c in v]
    except ValueError:
        return False
    s = sum(vals[i] * _CREDIT_WEIGHTS[i] for i in range(17))
    check = 31 - (s % 31)
    if check == 31:
        check = 0
    return vals[17] == check


register_checksum("credit_code_mod31", checksum_credit_code_mod31)
