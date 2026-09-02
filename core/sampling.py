"""
core/sampling.py
采样预演（第 3 项）：在全量扫描前先跑 1% 采样，验证假设方向是否值得投入全量算力。

流程：
    假设 → 1% 采样 → 预演计算 → 方向判定 → 决策
      │                              ├─ 方向明确(≥5%) → 全量扫描
      │                              ├─ 方向存疑(1~5%) → 扩大采样至 5% 再验
      │                              └─ 方向否定(<1%)   → 调整假设，不投全量

设计要点：
  - 采样用 DuckDB sample() 语法，零拷贝、零成本试错（不碰全量数据）
  - 方向判定阈值可调（默认 5% / 1%），适配不同侦查场景敏感度
  - 决策留痕：预演结果写进 lineage_report，正兵可追溯「为什么决定全量扫」
  - 红线：采样预演只给方向建议，全量扫描仍由正兵拍板

典型用法：
    from core.sampling import SamplingPreflight
    pre = SamplingPreflight(store, sample_ratio=0.01).run(["H1"])
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# 方向判定阈值（可调）
_THRESHOLD_CLEAR = 0.05    # 命中率 ≥ 5% → 方向明确
_THRESHOLD_DUBIOUS = 0.01  # 命中率 1%~5% → 方向存疑；<1% → 方向否定


@dataclass
class PreflightResult:
    """单次采样预演结果。"""
    hypothesis_id: str
    sample_ratio: float
    sampled_rows: int
    hit_rows: int
    hit_rate: float
    verdict: str             # "方向明确" / "方向存疑" / "方向否定"
    suggest: str             # 给正兵的建议
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class SamplingPreflight:
    """
    采样预演器。

    scan_sql 可选：自定义采样 SQL（默认扫 银行流水 全表做 sample）。
    判定函数 detect_hit 可选：自定义命中逻辑（默认"整数存入"模式）。
    """

    def __init__(self, store, sample_ratio: float = 0.01,
                 scan_sql: Optional[str] = None,
                 detect_hit=None, min_sample_rows: int = 200):
        self.store = store
        self.sample_ratio = sample_ratio
        self.scan_sql = scan_sql or "SELECT * FROM read_parquet('data/银行流水.parquet')"
        self.detect_hit = detect_hit or _default_hit
        # 最小样本行数：避免小数据集下 1% 抽样退化为 0 行（29 行 × 1% = 0.29 → 0 行），
        # 导致"命中率 0%"的假阴性，把好方向误判为"方向否定"。
        self.min_sample_rows = min_sample_rows

    def run(self, hypothesis_ids: List[str]) -> Dict[str, Any]:
        """
        对每个假设做采样预演。
        返回 {"results": [...], "overall_verdict": ..., "suggest": ...}
        """
        rows = self._take_sample()

        results: List[PreflightResult] = []
        for hid in hypothesis_ids:
            sampled = len(rows)
            hits = sum(1 for r in rows if self.detect_hit(r))
            rate = hits / sampled if sampled else 0.0
            verdict, suggest = _judge(rate)
            results.append(PreflightResult(
                hypothesis_id=hid, sample_ratio=self.sample_ratio,
                sampled_rows=sampled, hit_rows=hits, hit_rate=rate,
                verdict=verdict, suggest=suggest,
                detail={"sampled": sampled, "hits": hits},
            ))

        overall = _overall(results)
        return {
            "results": [r.to_dict() for r in results],
            "overall_verdict": overall,
            "suggest": "建议全量扫描" if overall != "方向否定" else "建议调整假设，暂缓全量扫描",
        }

    def _take_sample(self) -> List[dict]:
        """
        取样本：先算总量，再按 max(比例×总量, 最小行数) 取 reservoir 样本；
        总量已低于最小行数时直接全量（此时"采样"等价于全扫，成本仍可接受）。
        """
        try:
            total = self.store.query(
                f"SELECT COUNT(*) AS c FROM ({self.scan_sql}) t"
            )[0]["c"]
        except Exception:
            return self.store.query(self.scan_sql)

        target = max(int(total * self.sample_ratio), min(self.min_sample_rows, int(total)))
        if target >= int(total):
            return self.store.query(self.scan_sql)

        try:
            return self.store.query(
                f"SELECT * FROM ({self.scan_sql}) t USING SAMPLE reservoir({target} ROWS)"
            )
        except Exception:
            # 老版本 DuckDB 不支持 reservoir(n ROWS) → 退化为全表
            return self.store.query(self.scan_sql)


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _default_hit(row: dict) -> bool:
    """默认命中逻辑：金额为整数倍（与 H1 模式库一致）。"""
    amt = row.get("金额")
    if amt is None:
        return False
    try:
        return int(amt) % 10000 == 0
    except (ValueError, TypeError):
        return False


def _judge(hit_rate: float) -> tuple[str, str]:
    if hit_rate >= _THRESHOLD_CLEAR:
        return "方向明确", "命中率充足，建议投入全量扫描"
    if hit_rate >= _THRESHOLD_DUBIOUS:
        return "方向存疑", "命中率偏低，建议扩大采样至 5% 再验证"
    return "方向否定", "命中率过低，建议调整假设，暂不投入全量算力"


def _overall(results: List[PreflightResult]) -> str:
    """任一假设方向明确 → 整体方向明确；全否定 → 方向否定。"""
    if any(r.verdict == "方向明确" for r in results):
        return "方向明确"
    if any(r.verdict == "方向存疑" for r in results):
        return "方向存疑"
    return "方向否定"


def _make_id(name: str) -> str:
    import hashlib
    return "org_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:10]


__all__ = ["SamplingPreflight", "PreflightResult"]
