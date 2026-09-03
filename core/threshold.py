"""
core/threshold.py
REQ-027 阈值策略对象（分位数自适应）。

分工（红线）：
  - 声明在 ontology/<pack>/thresholds.json（方法、样本要求、fallback、夹紧范围、per）
  - 计算只读，不写 rule params 原值（返回新 params 副本 + 元组 method/value/degraded）
  - relative_median 走 FeatureStore 历史或 sample_rows（当前会话内查询到的频次样本）；
    样本不足 min_samples → fallback 值且 is_degraded=True，显式可审计。
  - 结果一定在 bounded_by [min, max] 区间夹紧（AC3）。
"""
from __future__ import annotations

import json
import math
import random
import statistics
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parent.parent / "ontology"
SCHEMA_VERSION = 2


def load_thresholds(pack: str = "default", base_dir: Path | None = None) -> dict:
    """返回 {rule_id: threshold_spec_dict}；pack/thresholds.json 不存在 → 空 dict，
    规则仍按 rules.json 硬编码跑（向后兼容）。"""
    root = (base_dir or PACK_ROOT) / pack
    p = root / "thresholds.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"thresholds.json schema_version={data.get('schema_version')}，"
            f"本内核期望 {SCHEMA_VERSION}")
    out = {}
    for i, t in enumerate(data.get("thresholds", [])):
        rid = t.get("rule")
        if not rid:
            raise ValueError(f"thresholds[{i}] 缺 rule 字段")
        if t.get("method") not in ("absolute", "relative_median"):
            raise ValueError(
                f"thresholds[{i}]({rid}) method='{t.get('method')}' 非法，"
                f"允许 absolute/relative_median")
        out[rid] = t
    return out


def _bounded(val, b):
    if not b:
        return val
    lo, hi = b.get("min"), b.get("max")
    if lo is not None and val < lo:
        return lo
    if hi is not None and val > hi:
        return hi
    return val


def _collect_samples(store, rule_id: str, per: list[str]) -> list[float]:
    """拉 R3 call_frequency_spike 对应的通话频次样本（per=subject 按主体分组）。

    非 R3：暂无 relative_median 声明，返回空列表（以后扩展时可按 rule→sample SQL 映射）。
    """
    if rule_id != "R3":
        return []
    try:
        rows = store.query(
            "SELECT caller_raw AS subject, SUM(cnt) AS freq "
            "FROM (SELECT caller_raw, callee_raw, COUNT(*) AS cnt FROM obj_call "
            "      GROUP BY caller_raw, callee_raw) t "
            "GROUP BY caller_raw")
    except Exception:
        return []
    if per and "subject" in per:
        return [float(r.get("freq") or 0) for r in rows]
    return [float(r.get("freq") or 0) for r in rows]


def resolve_rule_params(store, rule_id: str, params: dict,
                        pack: str = "default", seed: int | None = 20260501,
                        force_samples: list | None = None
                        ) -> tuple[dict, str, float | None, bool]:
    """根据 threshold.json 的策略，把 rule.params 副本中对应 bound_param 替换为计算值。

    Returns (params_copy, method, threshold_value, is_degraded)。
    force_samples：测试注入样本（跳过 FeatureStore / obj_call 查库，便于 fixture）。
    """
    params = dict(params)
    specs = load_thresholds(pack)
    sp = specs.get(rule_id)
    if sp is None:
        return params, "absolute_hardcoded", None, False

    method = sp["method"]
    b = sp.get("bounded_by")
    fb = sp.get("fallback")

    if method == "absolute":
        v = sp.get("value")
        if v is None:
            return params, "absolute_no_value", None, False
        v = _bounded(v, b)
        bp = sp.get("bound_param")
        if bp and bp in params:
            params[bp] = v
        return params, "absolute", v, False

    if method == "relative_median":
        mspec = sp.get("params", {}) or {}
        min_samples = int(mspec.get("min_samples", 20))
        multiplier = float(mspec.get("multiplier", 2.0))
        if seed is not None:
            rnd = random.Random(seed)
            _ = rnd.random()  # 保证固定 seed 下后续计算 deterministic（AC4）
        if force_samples is not None:
            samples = [float(x) for x in force_samples]
        else:
            samples = _collect_samples(store, rule_id, sp.get("per") or [])
        if len(samples) >= min_samples:
            median = statistics.median(samples)
            # 防止中位数 0
            if median == 0 and samples:
                median = max(sorted(samples)[max(0, len(samples)//2)], 1e-9)
            val = float(median) * multiplier
            # per=subject 下：向上取整（阈值为整数次数），保留一位小数
            if all(abs(s - round(s)) < 1e-6 for s in samples):
                val = int(math.ceil(val))
            val = _bounded(val, b)
            bp = sp.get("bound_param")
            if bp and bp in params:
                params[bp] = val
            return params, "relative_median", val, False
        # 样本不足 → fallback + degraded
        v = fb if fb is not None else params.get(sp.get("bound_param"), 30)
        v = _bounded(v, b)
        bp = sp.get("bound_param")
        if bp and bp in params:
            params[bp] = v
        method_desc = f"relative_median→fallback(样本={len(samples)}<min={min_samples})"
        return params, method_desc, (v if v is not None else None), True

    return params, f"unknown:{method}", None, False
