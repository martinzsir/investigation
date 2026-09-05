"""
core/value_type.py
值类型识别（REQ-P P1 / REQ-P-006~009）：纯函数模块，零第三方依赖（仅 stdlib）。

问题背景（.trae/documents/profile/pr/ONTOLOGY_PROFILER.md §三 坑 3）：
  第七批只按 objects.json 的 string 类型筛属性，完全不看内容，无法发现混装——
  transaction.from_raw 里 账号/人名 混写、org.raw_name 里 机构名/人名 混写时，
  归一落点不明确（归一到 account 还是 person？），选错就是链断裂。

两类判定（画像指南 §三）：
  否定式（可靠，命中即排除身份语义的歧义）：phone/id_card/date_str/amount/account/number
  肯定式（需确认）：全不中 → 人名 person / 机构名 org（启发式，误判已知——
    「供应商」「存续」等会被判为 person，由调用侧"只对可连接属性报警"规避刷屏）。

内置修掉的四个画像缺陷中的两个：
  缺陷 1：金额串正则过宽——原 ^[¥￥$]?[\\d,]+(\\.\\d+)?(万|万元|亿|亿元)?$
          可选部分全空时等价 ^[\\d,]+$，把 6222000111110001 判成金额串。
          修法：货币符号/千分位/小数/万元亿单位至少出现其一。
  缺陷 2：否定式顺序错误——账号 ^[\\d*\\-]{6,}$ 是手机号/身份证的超集，
          排前面会吞掉它们。修法：特异性强的排前面（ORDER 即判定序）。

混装判定口径：同一列出现 ≥2 个归一落点（landing）→ mixed。
  沙盒实测两例（from_raw 账号50%人名50%、org.raw_name 机构67%人名33%）均为
  跨落点混装；计划文中「否定式类≥2」是其子集情形，以落点口径覆盖。
  落点建议 = 各类型映射的实体对象（account/person/org），只建议不决定。
"""
from __future__ import annotations

import re
import unicodedata

# 否定式类型，按特异性降序 = classify 的判定顺序（缺陷 2 修复）
ORDER = ("phone", "id_card", "date_str", "amount", "account", "number")

# 缺陷 1 修复：货币符号 | 千分位 | 小数点 | 万元亿单位，至少出现其一才算金额串
_AMOUNT_THOUSANDS = r"^[¥￥$€£]?\d{1,3}(,\d{3})+(\.\d+)?(万|万元|亿|亿元)?$"
_AMOUNT_CURRENCY = r"^[¥￥$€£]\d+(\.\d+)?(万|万元|亿|亿元)?$"
_AMOUNT_UNIT = r"^\d+(\.\d+)?(万|万元|亿|亿元)$"
_AMOUNT_DECIMAL = r"^\d+\.\d+$"
AMOUNT_RE = re.compile(
    f"{_AMOUNT_THOUSANDS}|{_AMOUNT_CURRENCY}|{_AMOUNT_UNIT}|{_AMOUNT_DECIMAL}")

_RE = {
    "phone": re.compile(r"^1[3-9]\d{9}$"),                # 大陆手机号
    "id_card": re.compile(r"^\d{17}[\dXx]$"),             # 18 位身份证
    "date_str": re.compile(
        r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$"),          # 2021-10-01 / 2021/10/1 / 2021年10月1日
    "amount": AMOUNT_RE,
    "account": re.compile(r"^[\d*\-]{6,}$"),              # 账号/卡号（含 6222****1234 遮蔽态）
    "number": re.compile(r"^-?\d+$"),                     # 纯整数
}

# 机构名启发词（肯定式内部分流；与 core.ontology.ORG_KEYWORDS 同源口径，
# 本模块保持零依赖故独立声明，不依赖 core 其他模块）
_ORG_KEYWORDS = ("公司", "银行", "局", "厂", "中心", "集团", "财政", "院",
                 "所", "处", "队", "部", "社", "店", "馆", "委员会", "工作室",
                 "建设", "建材", "分公司", "子公司", "项目")

# 值类型 → 归一落点（实体对象名；只建议不决定）
_LANDING = {
    "account": "account",
    "phone": "person",
    "id_card": "person",
    "person": "person",
    "org": "org",
}


def _norm(value) -> str:
    """NFKC 全角→半角 + 去首尾空白。None/空白 → ''。"""
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def classify(value) -> str:
    """单值判定。返回 ORDER 之一、'empty'，或肯定式 'person'/'org'（需确认）。"""
    s = _norm(value)
    if not s:
        return "empty"
    for t in ORDER:
        if _RE[t].match(s):
            return t
    if any(k in s for k in _ORG_KEYWORDS):
        return "org"
    return "person"


def analyze_column(values) -> dict:
    """列级分析：类型分布 + 混装判定 + 归一落点建议（两个方向都报，只建议不决定）。

    返回：
      total               非None输入总数（含 empty）
      type_dist           {类型: 计数}，含 empty
      negative_types      命中的否定式类型（有序，按 ORDER）
      mixed               是否混装（≥2 个归一落点）
      landing_suggestions 归一落点建议（有序去重，如 ['account','person']）
      needs_confirmation  含肯定式（person/org）→ True（否定式可靠、肯定式需确认）
    """
    dist: dict[str, int] = {}
    for v in values:
        t = classify(v)
        dist[t] = dist.get(t, 0) + 1
    negative = [t for t in ORDER if dist.get(t)]
    landings = sorted({_LANDING[t] for t in dist if t in _LANDING})
    return {
        "total": sum(dist.values()),
        "type_dist": dist,
        "negative_types": negative,
        "mixed": len(landings) >= 2,
        "landing_suggestions": landings,
        "needs_confirmation": any(t in ("person", "org") for t in dist),
    }
