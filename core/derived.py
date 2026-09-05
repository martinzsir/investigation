"""
core/derived.py
REQ-028 DerivedProperty（查询时派生）。

设计：
  DerivedProperty(name, function, inputs: list[str], cache_policy) — 声明一个派生属性。
  cache_policy ∈ {never, ttl, until_source_change, materialized}。
  DerivedPropertyRegistry 是全局 register/get 入口（白名单 Function 守门 AC3）。
  compute() 返回 {value, computed_at, source_version_set, params_hash}。

  权限/展示层纪律：person.risk_score 这种"启发式打分"不进入 registry（AC5 断言），
  保持在展示层/MCP 层临时计算，避免进入"对象属性"的稳定语义层。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# 不允许注册为 DerivedProperty 的属性名（保持启发式/打分在展示层，AC5）
_FORBIDDEN_REGISTRY_NAMES = {
    "person.risk_score", "org.risk_score", "person.risk", "clue.score",
}

_VALID_POLICIES = {"never", "ttl", "until_source_change", "materialized"}

# 全局注册表
_REGISTRY: dict[str, "DerivedProperty"] = {}


@dataclass
class DerivedProperty:
    name: str                        # obj_type.property，如 "person.transaction_count"
    function: str                    # 白名单 Function 名（core/functions.json 已注册）
    inputs: list[str] = field(default_factory=list)  # 映射：function 参数 → 派生输入
    cache_policy: str = "never"      # never | ttl | until_source_change | materialized
    ttl_seconds: int = 60            # ttl 策略的有效期
    # 内部缓存：{params_hash: (value, expire_at_or_source_version)}
    _cache: dict = field(default_factory=dict, repr=False, compare=False)


def list_all() -> dict[str, DerivedProperty]:
    return dict(_REGISTRY)


def get(name: str) -> DerivedProperty | None:
    return _REGISTRY.get(name)


def _assert_name_allowed(name: str) -> None:
    """AC5：不允许的名字（启发式 risk_score 类）直接抛错，避免进入语义层。"""
    if name in _FORBIDDEN_REGISTRY_NAMES:
        raise ValueError(
            f"派生属性 '{name}' 被禁止注册：启发式打分/风险评分必须留在展示层/MCP 层，"
            f"不得作为对象语义属性（AC5）。")


def register(prop: DerivedProperty, *, pack: str = "default") -> DerivedProperty:
    """注册前校验：名称（AC5）、cache_policy、function 必须在白名单目录。

    Function 白名单 = FunctionExecutor.catalog 中的 functions 集合；未注册 function 抛 ValueError。
    """
    if not isinstance(prop, DerivedProperty):
        raise TypeError("register 只接受 DerivedProperty 对象")
    if prop.cache_policy not in _VALID_POLICIES:
        raise ValueError(
            f"DerivedProperty cache_policy='{prop.cache_policy}' 非法，允许 {sorted(_VALID_POLICIES)}")
    _assert_name_allowed(prop.name)
    if prop.name in _REGISTRY:
        raise ValueError(f"派生属性已注册（重复）：{prop.name}")
    # Function 白名单校验（AC3 派生只能调白名单只读 Function）
    from core.ontology_loader import load_pack
    valid_fns = set(load_pack(pack).functions.keys())
    if prop.function not in valid_fns:
        raise ValueError(
            f"DerivedProperty '{prop.name}' 绑定 function='{prop.function}' 未在"
            f" {pack} 包 functions.json 声明（白名单 AC3），可用 {sorted(valid_fns)}")
    _REGISTRY[prop.name] = prop
    return prop


def _params_hash(params: dict) -> str:
    s = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _source_version_set(store, obj_type: str, health=None, pack: str = "default") -> str:
    """until_source_change 版本戳：返回 ontology 版本号 + obj_* 行计数。

    REQ-G-001：版本号/行数任一锚点不可靠时，**不得**返回常量令牌——那会让
    until_source_change 的"源变才重算"判定在异常期间恒不成立、持续复用陈旧值。
    锚点失败时改用一次性随机令牌（与任何历史令牌都不等）强制 miss 重算，并落
    version_anchor_missing 诊断（REQ-G-007）。
    注：缓存字典键是 params_hash（含 obj_pks），跨对象本就隔离，此处修的是
    "同一对象自身陈旧值不重算"，非跨对象串号。

    历史坑：此前误用不存在的 core.ontology.get_ontology_version（恒抛 ImportError），
    导致版本锚点**从未真正取到**（一直 unknown），只靠行计数稳定才没出问题；
    此处改用 REQ-001 版本时钟 core.ontology_version.current_version。
    """
    import uuid as _uuid
    from core.run_health import get_health
    health = get_health(health)

    version_ok = False
    v = None
    try:
        from core.ontology_version import current_version
        ov = current_version(getattr(store, "conn", store), pack)
        v = getattr(ov, "ontology_version", None)
        version_ok = bool(v) and v != "unknown"
    except Exception:
        version_ok = False

    count_ok = False
    cnt = 0
    try:
        rows = store.query(f"SELECT COUNT(*) AS c FROM obj_{obj_type}")
        cnt = rows[0]["c"] if rows else 0
        count_ok = True
    except Exception:
        count_ok = False

    if version_ok and count_ok:
        return f"{v}::{cnt}"

    # 至少一个锚点不可靠 → 一次性令牌强制重算，绝不复用陈旧缓存
    health.record(
        "version_anchor_missing", "warning",
        source=f"derived:obj_{obj_type}",
        reason="派生属性源版本/行数锚定失败，强制重算（不使用陈旧缓存）",
        obj_type=obj_type, version_ok=version_ok, count_ok=count_ok)
    return f"unknown::{obj_type}::{_uuid.uuid4().hex}"


def compute(store, obj_type: str, property_name: str,
            obj_pks: list[Any] | None = None, params: dict | None = None,
            *, pack: str = "default", _now_fn: Callable[[], float] = time.time,
            health=None
            ) -> dict:
    """计算一个派生属性。返回 {value, computed_at, source_version_set, params_hash}。

    - cache_policy=never（AC1）：每次重算；
    - until_source_change（AC2）：source_version_set 变化才重算；
    - ttl：时间窗口内复用缓存；
    - materialized：与 obj_{type} 对应派生列同步（无列则首次计算写临时表，ttl_seconds 内复用）。
    """
    full = f"{obj_type}.{property_name}"
    prop = get(full) or get(property_name)
    if prop is None:
        raise KeyError(f"派生属性未注册：{full}")

    _assert_name_allowed(full)  # AC5 双保险

    params = dict(params or {})
    if obj_pks:
        params.setdefault("obj_pks", list(obj_pks))
    ph = _params_hash(params)
    computed_at = _now_fn()
    src_ver = _source_version_set(store, obj_type, health=health, pack=pack)

    # 检查命中缓存
    cached_entry = prop._cache.get(ph)
    cache_hit = False
    if cached_entry is not None:
        if prop.cache_policy == "ttl":
            value, expire_at = cached_entry
            if computed_at < expire_at:
                cache_hit = True
                cached_value = value
        elif prop.cache_policy == "until_source_change":
            value, ver = cached_entry
            if ver == src_ver:
                cache_hit = True
                cached_value = value
        elif prop.cache_policy == "materialized":
            value, ver, expire = cached_entry
            if ver == src_ver and computed_at < expire:
                cache_hit = True
                cached_value = value

    if cache_hit:
        return {"value": cached_value, "computed_at": computed_at,
                "source_version_set": src_ver, "params_hash": ph, "cache": "hit"}

    # 白名单 Function 执行
    from core.functions import FunctionExecutor
    fx = FunctionExecutor(store, pack, health=health)
    out = fx.invoke(prop.function, params)
    if out.get("rows") is not None:
        value = out["rows"]
    else:
        value = out.get("result")

    # 写缓存
    if prop.cache_policy == "ttl":
        prop._cache[ph] = (value, computed_at + prop.ttl_seconds)
    elif prop.cache_policy == "until_source_change":
        prop._cache[ph] = (value, src_ver)
    elif prop.cache_policy == "materialized":
        prop._cache[ph] = (value, src_ver, computed_at + prop.ttl_seconds)
    # never 不缓存

    return {"value": value, "computed_at": computed_at,
            "source_version_set": src_ver, "params_hash": ph, "cache": "miss"}
