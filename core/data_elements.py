"""
core/data_elements.py —— 数据元标准注册表（REQ-D-001）。

数据元回答"这个值该长什么样"（类型/长度/格式/校验位/敏感度/推荐清洗）。
声明在 ontology/<pack>/data_elements.json；校验算法（checksum）在此注册：
未知算法装载期硬失败（fail-closed 不放宽，REQ-D-001 AC-2）。零第三方依赖。
"""
from __future__ import annotations

from typing import Callable

CHECKSUM_ALGOS: dict[str, Callable[[str], bool]] = {}


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
