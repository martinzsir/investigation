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
    description: str = ""


OPS: dict[str, OpSpec] = {}


def register_op(name: str, *, impl: str, layer: str = "clean",
                fn: Callable | None = None, sql_template: str | None = None,
                description: str = "") -> OpSpec:
    """注册一个 op；结构非法或同名重复注册硬失败（REQ-D-004 AC-2）。"""
    if impl not in ("py", "sql"):
        raise ValueError(f"op '{name}' impl='{impl}' 非法，允许 'py' | 'sql'")
    if impl == "py" and not callable(fn):
        raise ValueError(f"op '{name}' impl=py 必须提供可调用 fn")
    if impl == "sql" and not (sql_template or "").strip():
        raise ValueError(f"op '{name}' impl=sql 必须提供 sql_template")
    if layer not in LAYERS:
        raise ValueError(f"op '{name}' layer='{layer}' 非法，允许 {LAYERS}")
    if name in OPS:
        raise ValueError(
            f"清洗 op 重复注册：'{name}'（REQ-D-004 AC-2：同名 op 唯一，"
            f"现有实现 impl={OPS[name].impl}/layer={OPS[name].layer}）")
    OPS[name] = OpSpec(name=name, impl=impl, layer=layer, fn=fn,
                       sql_template=sql_template, description=description)
    return OPS[name]


def clean_layer_names() -> set[str]:
    """clean 层可用 op 名（CLEAN_RULE_NAMES 的数据源）。"""
    return {n for n, s in OPS.items() if s.layer in ("clean", "any")}


def transform_layer_names() -> set[str]:
    """transform 层可用 op 名（批 D2 loader 校验用）。"""
    return {n for n, s in OPS.items() if s.layer in ("transform", "any")}


def compile_sql_expr(ops, col_expr: str) -> str:
    """把声明 op 链编译为 SQL 表达式（REQ-D-009 / AD-1 投影注入）。

    ops 为 op 名序列（str 或 list[str]），按声明顺序链式套用：
    第一个 op 作用于源列，后续 op 作用于前序结果。仅支持 impl=sql 的
    无参 op——sql_template 中 {col} 为被处理表达式占位符；
    带 "op:param" 参数形式的 op 属批 D5 参数化机制，此处显式拒绝。
    未知 op 硬失败（REQ-D-009 AC-6）。
    """
    if isinstance(ops, str):
        ops = [ops]
    expr = col_expr
    for tok in ops:
        op = str(tok).strip()
        if ":" in op:
            raise ValueError(
                f"transform 带参 op '{op}' 属批 D5 参数化机制（当前仅支持无参声明式 op）")
        spec = OPS.get(op)
        if spec is None:
            raise ValueError(f"transform 引用未注册 op：'{op}'（REQ-D-009 AC-6）")
        if spec.impl != "sql" or not spec.sql_template:
            raise ValueError(
                f"transform op '{op}' impl={spec.impl}——transform 在 SQL 投影内编译"
                f"执行，仅支持 impl=sql 的声明式 op")
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
