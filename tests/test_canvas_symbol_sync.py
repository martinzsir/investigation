"""前后端精度权重口径对拍。

为什么要有这个测试
------------------
`PRECISION_WEIGHT` 是整个"次数会骗人"论证的地基：date 档压到 0.2、
minute/second 给满 1.0，才让 3 次时刻级压过 15 次日期级。它现在**同时存在
于两处**——后端 core/convergence.py（算分用）与前端
frontend/src/domain/canvas-symbol.ts（画线型/线宽用）。

任一侧改动而另一侧不动，界面与算分会给出相反的答案：后端认为 3 次时刻级更重要，
前端却把它画成细线。这类分叉不报错、有输出、看不出问题，只能靠对拍抓住。

本测试从 TS 源文解析常量表（不执行前端代码），与后端字典逐项比对。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TS_PATH = ROOT / "frontend" / "src" / "domain" / "canvas-symbol.ts"

try:
    from core.convergence import (  # type: ignore
    PRECISION_WEIGHT as BACKEND,
    REPEAT_CAP,
    REPEAT_STEP,
)
except Exception as exc:  # pragma: no cover - 环境问题单独暴露
    pytest.skip(f"无法导入 core.convergence：{exc}", allow_module_level=True)


def _parse_ts_weights(text: str) -> dict[str, float]:
    """从 `PRECISION_WEIGHT: Record<Precision, number> = { ... }` 解析字面量。"""
    m = re.search(
        r"PRECISION_WEIGHT\s*:\s*Record<Precision,\s*number>\s*=\s*\{(.*?)\}",
        text,
        re.S,
    )
    assert m, "canvas-symbol.ts 中未找到 PRECISION_WEIGHT 常量表"
    body = m.group(1)
    out: dict[str, float] = {}
    for key, val in re.findall(r"(\w+)\s*:\s*([0-9.]+)", body):
        out[key] = float(val)
    return out


def test_weight_table_matches_backend() -> None:
    assert TS_PATH.exists(), f"前端符号口径文件缺失：{TS_PATH}"
    front = _parse_ts_weights(TS_PATH.read_text(encoding="utf-8"))
    back = {k: float(v) for k, v in BACKEND.items()}

    assert set(front) == set(back), (
        f"精度档集合不一致：前端 {sorted(front)} vs 后端 {sorted(back)}"
    )
    for k in sorted(back):
        assert front[k] == pytest.approx(back[k]), (
            f"{k} 档权重分叉：前端 {front[k]} vs 后端 {back[k]}"
        )


def _parse_ts_float(text: str, name: str) -> float:
    m = re.search(rf"{name}\s*=\s*([0-9.]+)", text)
    assert m, f"canvas-symbol.ts 中未找到 {name}"
    return float(m.group(1))


def test_repeat_model_matches_backend() -> None:
    """repeat 上限是"次数不骗人"的落地装置，必须与后端同参。"""
    text = TS_PATH.read_text(encoding="utf-8")
    assert _parse_ts_float(text, "REPEAT_STEP") == pytest.approx(REPEAT_STEP)
    assert _parse_ts_float(text, "REPEAT_CAP") == pytest.approx(REPEAT_CAP)


def test_date_must_be_far_below_minute() -> None:
    """红线：15 次日期级必须输给 3 次时刻级。

    裸乘（count × weight）会打平：15×0.2 = 3.0 = 3×1.0。真正拉开差距的是
    repeat 上限——后端算分也走同一式，所以这里按后端公式重算。
    """
    front = _parse_ts_weights(TS_PATH.read_text(encoding="utf-8"))
    assert front["date"] < front["minute"] / 2, "date 档权重过高，无法压住次数堆砌"

    def score(count: int, precision: str) -> float:
        return front[precision] * min(1 + REPEAT_STEP * (count - 1), REPEAT_CAP)

    assert score(15, "date") < score(3, "minute"), (
        f"15 次日期级({score(15, 'date'):.2f}) 不得盖过 3 次时刻级({score(3, 'minute'):.2f})"
    )


def test_unknown_is_not_zero() -> None:
    """未知档给 0 会把"无时间信息"当成"无证据"——应给最小值而非零。"""
    front = _parse_ts_weights(TS_PATH.read_text(encoding="utf-8"))
    assert front["unknown"] > 0, "unknown 档权重为 0 会静默吞掉无时间信息的证据"
    assert front["unknown"] < front["date"], "unknown 档不得高于 date 档"
