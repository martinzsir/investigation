"""
ITM-05 前置验证：过桥两跳的时态能力（core/graph.py）。

背景（核查结论，不是推测）：
  overpass_two_hop（Cypher）与 overpass_two_hop_sql（SQL）**都 SELECT 了
  d1/d2**，但只拿去拼 source_rows 文案，**WHERE 里没有任何时间约束**，
  Python 侧也无 timedelta 计算。结果是：相隔三年的两笔转账也会被当成
  一条"过桥路径"——而这是 H4（财物通过第三方过桥）假设的核心证据。

本文件守三条红线：
  1. 不可判定 → 返回 None，绝不静默塞 0（与 time_conflict 同款）
  2. 不可判定的路径**保留**并标 gap_unknown，不静默丢弃
  3. max_gap_days=None 时行为不变（向后兼容），只补算事实
"""
import datetime as dt

import pytest

from core.graph import OverpassPath, overpass_gap_days, _apply_gap_filter


def _p(src="A", mid="B", dst="C"):
    return OverpassPath(source=src, bridge=mid, dest=dst,
                        amount_in=100.0, amount_out=90.0, engine="sql")


# ----------------------------------------------------------------------
# overpass_gap_days：计算
# ----------------------------------------------------------------------
def test_gap_positive_when_out_after_in():
    """出账晚于入账 → 正天数（正常过桥：钱进来再出去）。"""
    g = overpass_gap_days("2020-03-01", "2020-03-11")
    assert g is not None and abs(g - 10.0) < 1e-6


def test_gap_zero_same_day():
    """同日进出 → 0（当天过桥，最强的过桥形态）。"""
    g = overpass_gap_days("2020-03-01", "2020-03-01")
    assert g == 0.0


def test_gap_negative_when_out_before_in():
    """出账早于入账 → 负。这不是过桥，是无关的反向流水。"""
    g = overpass_gap_days("2020-03-11", "2020-03-01")
    assert g is not None and g < 0


def test_gap_accepts_datetime_and_date_objects():
    """Cypher 轨返回 date/datetime 对象，SQL 轨返回字符串——都要能算。"""
    assert overpass_gap_days(dt.date(2020, 3, 1), dt.datetime(2020, 3, 6)) == 5.0
    assert overpass_gap_days(dt.datetime(2020, 3, 1), "2020-03-06") == 5.0


def test_gap_with_minute_precision():
    """时刻级也能算：半天 = 0.5 天。"""
    g = overpass_gap_days("2020-03-01 08:00:00", "2020-03-01 20:00:00")
    assert abs(g - 0.5) < 1e-6


# ----------------------------------------------------------------------
# 红线 1：不可判定不静默塞 0
# ----------------------------------------------------------------------
@pytest.mark.parametrize("d_in,d_out", [
    (None, "2020-03-01"),
    ("2020-03-01", None),
    ("", "2020-03-01"),
    ("2020-03-01", "非法日期"),
])
def test_gap_none_when_unparsable(d_in, d_out):
    """日期缺失/不可解析 → None。绝不返回 0 冒充"当天过桥"。"""
    assert overpass_gap_days(d_in, d_out) is None


def test_gap_none_is_not_zero():
    """反向验证：若有人把 None 改成 0，此测试必须失败。"""
    g = overpass_gap_days(None, None)
    assert g is None, "不可判定必须返回 None，返回 0 会把'不知道'伪装成'当天过桥'"
    assert not (g == 0), "None 不得等价于 0"


# ----------------------------------------------------------------------
# _apply_gap_filter：过滤行为
# ----------------------------------------------------------------------
def test_default_no_filter_preserves_all():
    """max_gap_days=None → 不过滤，行为与改动前一致（向后兼容红线）。"""
    paths = [_p("A", "B", "C"), _p("D", "E", "F")]
    out = _apply_gap_filter(paths, None, ["2020-01-01", "2020-01-01"],
                            ["2023-01-01", "2020-01-02"])
    assert len(out) == 2
    # 但事实要补算出来
    assert out[0].gap_days is not None and out[0].gap_days > 1000


def test_filter_drops_too_slow_bridge():
    """相隔 3 年的两笔不算过桥：这正是改动前会误判的那类路径。"""
    paths = [_p()]
    out = _apply_gap_filter(paths, 30.0, ["2020-01-01"], ["2023-01-01"])
    assert out == []


def test_filter_keeps_fast_bridge():
    """3 天内进出 → 保留。"""
    paths = [_p()]
    out = _apply_gap_filter(paths, 30.0, ["2020-01-01"], ["2020-01-03"])
    assert len(out) == 1 and abs(out[0].gap_days - 2.0) < 1e-6


def test_filter_drops_reverse_flow():
    """出账早于入账超阈值 → 不是过桥，是反向流水。"""
    paths = [_p()]
    out = _apply_gap_filter(paths, 30.0, ["2020-01-01"], ["2019-01-01"])
    assert out == []


# ----------------------------------------------------------------------
# 红线 2：不可判定的路径保留并标注
# ----------------------------------------------------------------------
def test_unknown_gap_kept_and_flagged():
    """判不出来 → 保留 + gap_unknown=True，绝不静默丢弃。

    静默丢弃最危险：正兵看到路径数变少，会以为"确实没有过桥"，
    而实际是"数据不全，判不出来"——两件事性质完全不同。
    """
    paths = [_p()]
    out = _apply_gap_filter(paths, 30.0, [None], ["2020-01-01"])
    assert len(out) == 1, "不可判定的路径必须保留，不得静默丢弃"
    assert out[0].gap_unknown is True
    assert out[0].gap_days is None


def test_unknown_gap_kept_even_without_filter():
    """不过滤时同样要标注，便于调用方自行统计"有多少是判不出来的"。"""
    paths = [_p()]
    out = _apply_gap_filter(paths, None, ["非法"], ["2020-01-01"])
    assert len(out) == 1 and out[0].gap_unknown is True


def test_mixed_known_and_unknown():
    """已知与未知混合：各自按规则处理，不相互影响。"""
    paths = [_p("A", "B", "C"), _p("D", "E", "F"), _p("G", "H", "I")]
    out = _apply_gap_filter(paths, 30.0,
                            ["2020-01-01", None, "2020-01-01"],
                            ["2020-01-02", "2020-01-02", "2023-01-01"])
    assert len(out) == 2
    assert out[0].gap_days == 1.0
    assert out[1].gap_unknown is True


# ----------------------------------------------------------------------
# 双轨一致性：Cypher 与 SQL 共用同一过滤器
# ----------------------------------------------------------------------
def test_filter_is_shared_by_both_engines():
    """两轨都走 _apply_gap_filter，口径不可能分叉。

    这不是形式主义：此前 Cypher 与 SQL 恰好"一致地缺"时间约束，
    说明分叉风险真实存在——故共用同一个函数而非各写一遍。
    """
    import inspect
    from core import graph
    src_cypher = inspect.getsource(graph.GraphBackend.overpass_two_hop)
    src_sql = inspect.getsource(graph.overpass_two_hop_sql)
    assert "_apply_gap_filter" in src_cypher
    assert "_apply_gap_filter" in src_sql
