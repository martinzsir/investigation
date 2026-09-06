"""
core/clean_ops.py —— 唯一 op 注册表（REQ-D-004 / 实施方案 AD-2）。

clean 层（REQ-D-005/006）与 transform 层（REQ-D-009/011）共用同一注册表，
两层只是注入点不同，禁止两套并行实现。

- 声明引用名在此注册，loader 校验 binding/data_elements 引用必须已注册（fail-closed 不放宽）；
- 同名 op 重复注册硬失败（REQ-D-004 AC-2）；
- CLEAN_RULE_NAMES（core/ontology.py）由本注册表 clean 层实时派生（AC-1 动态集合）；
- 零第三方依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

LAYERS = ("clean", "transform", "any")


@dataclass(frozen=True)
class OpSpec:
    name: str
    impl: str                        # "py"（fn 可调用）| "sql"（sql_template 表达式模板）
    layer: str = "clean"             # 注入点：clean（Python 侧行后处理）| transform（SQL 投影内）| any
    fn: Callable | None = None       # impl=py 的实现
    sql_template: str | None = None  # impl=sql 的表达式模板（批 D2/D5 启用）
    param_enum: tuple[str, ...] | None = None  # 带参 op 的参数白名单（REQ-D-011 防注入）
    description: str = ""


OPS: dict[str, OpSpec] = {}


def register_op(name: str, *, impl: str, layer: str = "clean",
                fn: Callable | None = None, sql_template: str | None = None,
                param_enum: tuple[str, ...] | list[str] | None = None,
                description: str = "") -> OpSpec:
    """注册一个 op；结构非法或同名重复注册硬失败（REQ-D-004 AC-2）。

    impl 标注主实现；fn 与 sql_template 至少提供其一——两者齐备且 layer="any" 时
    同一 op 在 clean（py）与 transform（sql）两层均可用（REQ-D-006/011 归一 op）。
    param_enum 声明带参 op（"name:param"）的合法参数白名单；自由文本参数一律硬失败。
    """
    if impl not in ("py", "sql"):
        raise ValueError(f"op '{name}' impl='{impl}' 非法，允许 'py' | 'sql'")
    has_fn = callable(fn)
    has_sql = bool((sql_template or "").strip())
    if not has_fn and not has_sql:
        raise ValueError(f"op '{name}' 必须提供可调用 fn（py）或 sql_template（sql）至少一个实现")
    if layer not in LAYERS:
        raise ValueError(f"op '{name}' layer='{layer}' 非法，允许 {LAYERS}")
    pe: tuple[str, ...] | None = None
    if param_enum is not None:
        if not isinstance(param_enum, (tuple, list)) or \
           not all(isinstance(x, str) and x.strip() for x in param_enum):
            raise ValueError(f"op '{name}' param_enum 必须是非空字符串数组（参数白名单）")
        pe = tuple(x.strip() for x in param_enum)
    if name in OPS:
        raise ValueError(
            f"清洗 op 重复注册：'{name}'（REQ-D-004 AC-2：同名 op 唯一，"
            f"现有实现 impl={OPS[name].impl}/layer={OPS[name].layer}）")
    OPS[name] = OpSpec(name=name, impl=impl, layer=layer, fn=fn,
                       sql_template=sql_template, param_enum=pe,
                       description=description)
    return OPS[name]


def split_op(tok: str) -> tuple[str, str | None]:
    """把 op token 拆为 (name, param)：'reject_if:contains_mask' → ('reject_if','contains_mask')。"""
    name, _, param = str(tok).partition(":")
    name = name.strip()
    param = param.strip() if param else None
    return name, (param or None)


def validate_op(tok: str, layer: str) -> tuple[str, str | None]:
    """校验 op token 在指定层（'clean'|'transform'）可用；返回 (name, param)。

    未知 op / 层不匹配 / 缺该层实现 / 带参但无白名单或参数不在白名单 → ValueError
    （REQ-D-009 AC-6 未知硬失败；REQ-D-011 字符串参数仅白名单取值，防注入）。
    """
    name, param = split_op(tok)
    spec = OPS.get(name)
    if spec is None:
        raise ValueError(f"op '{name}' 未在 op 注册表注册")
    if layer == "clean":
        if spec.layer not in ("clean", "any"):
            raise ValueError(f"op '{name}' layer={spec.layer}，不可用于 clean 层")
        if not callable(spec.fn):
            raise ValueError(f"op '{name}' 无 py 实现，不能在 clean（Python 侧）层使用")
    elif layer == "transform":
        if spec.layer not in ("transform", "any"):
            raise ValueError(f"op '{name}' layer={spec.layer}，不可用于 transform 层")
        if not (spec.sql_template or "").strip():
            raise ValueError(f"op '{name}' 无 sql_template，不能在 transform（SQL 投影）层使用")
    else:  # pragma: no cover - 内部调用约定
        raise ValueError(f"validate_op layer='{layer}' 非法，允许 clean | transform")
    if param is not None:
        if not spec.param_enum:
            raise ValueError(f"op '{name}' 不接受参数（得到 ':{param}'）")
        if param not in spec.param_enum:
            raise ValueError(
                f"op '{name}' 参数 '{param}' 不在白名单 {list(spec.param_enum)}"
                f"（字符串参数仅允许白名单取值，防注入）")
    return name, param


def clean_layer_names() -> set[str]:
    """clean 层可用 op 名（CLEAN_RULE_NAMES 的数据源）。"""
    return {n for n, s in OPS.items() if s.layer in ("clean", "any")}


def transform_layer_names() -> set[str]:
    """transform 层可用 op 名（批 D2 loader 校验用）。"""
    return {n for n, s in OPS.items() if s.layer in ("transform", "any")}


def compile_sql_expr(ops, col_expr: str) -> str:
    """把声明 op 链编译为 SQL 表达式（REQ-D-009 / AD-1 投影注入）。

    ops 为 op 名序列（str 或 list[str]），按声明顺序链式套用：
    第一个 op 作用于源列，后续 op 作用于前序结果。仅支持 impl=sql 的 op——
    sql_template 中 {col} 为被处理表达式占位符；带 "op:param" 形式时 param 必须在
    该 op 的 param_enum 白名单内（REQ-D-011，自由文本硬失败防注入），{param} 占位符
    渲染为白名单字面值。未知 op / 参数非白名单硬失败（REQ-D-009 AC-6）。
    """
    if isinstance(ops, str):
        ops = [ops]
    expr = col_expr
    for tok in ops:
        name, param = split_op(tok)
        spec = OPS.get(name)
        if spec is None:
            raise ValueError(f"transform 引用未注册 op：'{name}'（REQ-D-009 AC-6）")
        if not (spec.sql_template or "").strip():
            raise ValueError(
                f"transform op '{name}' 无 sql_template——transform 在 SQL 投影内编译"
                f"执行，仅支持带 SQL 实现的声明式 op（py op 属 clean 层）")
        if param is not None:
            if not spec.param_enum:
                raise ValueError(f"transform op '{name}' 不接受参数（得到 ':{param}'）")
            if param not in spec.param_enum:
                raise ValueError(
                    f"transform op '{name}' 参数 '{param}' 不在白名单 {list(spec.param_enum)}"
                    f"（字符串参数仅白名单取值，防注入）")
            expr = spec.sql_template.replace("{col}", expr).replace("{param}", param)
        else:
            expr = spec.sql_template.replace("{col}", expr)
    return expr


# ---- 内置 SQL transform op（REQ-D-009 脏值抢救最小集；paramless，批 D5 扩充参数化 op）----
# 模板约定：{col} = 被处理表达式（链式编译时为前序 op 的结果）。
register_op("strip_thousands", impl="sql", layer="transform",
            sql_template="regexp_replace({col}, ',', '', 'g')",
            description="千分位逗号剥离：48,000.00 → 48000.00（TRY_CAST 前生效）")
register_op("strip_currency", impl="sql", layer="transform",
            sql_template=r"regexp_replace({col}, '[¥￥$€£\s]', '', 'g')",
            description="货币符号剥离：￥1,280.50 → 1,280.50")
register_op("cn_date_norm", impl="sql", layer="transform",
            sql_template=("regexp_replace(regexp_replace(regexp_replace("
                          "{col}, '年', '-'), '月', '-'), '日', '')"),
            description="中文日期归一：2024年3月15日 → 2024-3-15（TRY_CAST 可解析）")


# ---- 案件级词表（REQ-D-007：从代码硬编码外置到 clean_rules.json，可合并/替换）----
DEFAULT_ORG_KEYWORDS = ("公司", "局", "厂", "中心", "部", "建材", "建设", "银行",
                        "财政", "集团", "院", "所", "处", "队")
DEFAULT_SUMMARY_TOKENS = ("现金存入", "工资", "代发", "利息", "转账", "存款", "取现")


class CleanContext(set):
    """py op 运行上下文。

    set 成员 = 工商登记 org 名单（``v in ctx`` 既有口径不变）；另携带案件级词表
    org_keywords / summary_tokens（REQ-D-007）。传入普通 set（无词表属性）时
    op 回落内置基线词表，旧调用/测试全兼容。
    """

    def __init__(self, org_names=(), org_keywords=DEFAULT_ORG_KEYWORDS,
                 summary_tokens=DEFAULT_SUMMARY_TOKENS):
        super().__init__(org_names or ())
        self.org_keywords = tuple(org_keywords)
        self.summary_tokens = frozenset(summary_tokens)


def build_clean_context(org_names, clean_rules: dict | None) -> "CleanContext":
    """按 clean_rules.json 声明构造清洗上下文（REQ-D-007 合并/替换语义）。

    clean_rules: {"mode": "merge"(默认)|"replace",
                  "org_keywords": [...], "summary_tokens": [...]}
    - merge：内置基线词表 + 案件词表追加（基线词不重复）；
    - replace：案件词表整体替换基线（电诈场景换贪腐词表）；空表 = 仅靠 org 名单。
    """
    cr = clean_rules or {}
    mode = cr.get("mode", "merge")
    if mode not in ("merge", "replace"):
        raise ValueError(f"clean_rules.json mode='{mode}' 非法，允许 merge | replace")
    extra_kw = list(cr.get("org_keywords") or [])
    extra_tok = list(cr.get("summary_tokens") or [])
    if mode == "merge":
        kws = list(DEFAULT_ORG_KEYWORDS) + [k for k in extra_kw
                                            if k not in DEFAULT_ORG_KEYWORDS]
        toks = list(DEFAULT_SUMMARY_TOKENS) + [t for t in extra_tok
                                               if t not in DEFAULT_SUMMARY_TOKENS]
    else:
        kws, toks = extra_kw, extra_tok
    return CleanContext(org_names, org_keywords=kws, summary_tokens=toks)


# ---- REQ-D-006/011 声明式归一原子 op（py + sql 双实现，clean/transform 两层可用）----
# 电诈等场景常见脏格式的无参归一（零自由文本、零注入面）；模板 {col}=被处理表达式。

def _digits_only(v, _ctx=None):
    return re.sub(r"\D", "", str(v if v is not None else ""))


register_op("digits_only", impl="py", layer="any", fn=_digits_only,
            sql_template=r"regexp_replace({col}, '[^0-9]', '', 'g')",
            description="仅保留数字：手机号 '138 0013 8000' / 卡号 '6222-0212-3456-7890' → 纯数字")

_CC_RE = re.compile(r"^(?:\+?86|0086)(1[3-9]\d{9})$")


def _strip_cc(v, _ctx=None):
    s = str(v if v is not None else "")
    m = _CC_RE.match(s)
    return m.group(1) if m else s


register_op("strip_cc", impl="py", layer="any", fn=_strip_cc,
            sql_template=("CASE WHEN regexp_matches({col}, '^(\\+?86|0086)1[3-9][0-9]{9}$') "
                          "THEN regexp_replace({col}, '^(\\+?86|0086)', '') ELSE {col} END"),
            description="剥手机国家码（+86/0086，仅 86+11 位手机号形态）：8613800138000 → 13800138000")

_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


def _strip_paren(v, _ctx=None):
    return _PAREN_RE.sub("", str(v if v is not None else ""))


register_op("strip_paren", impl="py", layer="any", fn=_strip_paren,
            sql_template=r"regexp_replace({col}, '[（(][^）)]*[）)]', '', 'g')",
            description="剥括号注释：'李强（绰号小强）' → '李强'")


def _despace(v, _ctx=None):
    return re.sub(r"\s+", "", str(v if v is not None else ""))


register_op("despace", impl="py", layer="any", fn=_despace,
            sql_template=r"regexp_replace({col}, '[\s　]', '', 'g')",
            description="去全部空白（首尾+内部+全角空格）：'李  强' → '李强'（人名拼写变体归一，REQ-D-005 改值）")


def _to_upper(v, _ctx=None):
    return str(v if v is not None else "").upper()


def _to_lower(v, _ctx=None):
    return str(v if v is not None else "").lower()


register_op("to_upper", impl="py", layer="any", fn=_to_upper,
            sql_template="UPPER({col})", description="大写归一：abc → ABC")
register_op("to_lower", impl="py", layer="any", fn=_to_lower,
            sql_template="LOWER({col})", description="小写归一：ABC → abc")


def _pad_date(v, _ctx=None):
    s = str(v if v is not None else "")
    if "-" not in s:
        return s
    return "-".join(p.zfill(2) if p.isdigit() else p for p in s.split("-"))


register_op("pad_date", impl="py", layer="any", fn=_pad_date,
            sql_template=(r"regexp_replace(regexp_replace({col}, '-([0-9])-', '-0\1-'), "
                          r"'-([0-9])$', '-0\1')"),
            description="日期补零：2024-3-5 → 2024-03-05（常接 cn_date_norm 之后）")


# ---- 带参 py op（clean 层行拒绝；参数白名单防注入，REQ-D-011）----
def _reject_if(v, _ctx=None, param=None):
    """条件拒绝（滤行双通道）：param=contains_mask 时值含遮蔽符（* / ×）整行剔除。"""
    s = v if v is not None else ""
    if param == "contains_mask" and ("*" in str(s) or "×" in str(s)):
        return (s, False)
    return v


register_op("reject_if", impl="py", layer="clean", fn=_reject_if,
            param_enum=("contains_mask",),
            description="条件拒绝整行：reject_if:contains_mask 命中星号/全角乘号遮蔽"
                        "（如 6222********7890 为脱敏残片，不入语义层）")
