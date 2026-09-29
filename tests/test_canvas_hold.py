"""物品持有链装配测试（R2/D2 时态边 + WIN-11 数据源）。

命名沿用项目惯例：test_<模块>_<行为>__<条件>。

反向验证清单（改动本文件时必须重跑，见文末 test_mutation_guards）：
  M1 边方向双向都认      —— 反向边会被误纳，持有关系被倒挂
  M2 unknown 并入 conflicts —— 伪精确，date 档被当成"确认同时持有"
  M3 空链不给数据源原因  —— 正兵读成"查无持有"，而实际是"数据源未接入"
"""

from __future__ import annotations

from server.app.canvas_hold import (
    HOLD_REL,
    build_item_hold_chain,
    holding_records_from_edges,
)


def _edge(eid, src, tgt, holder=None, start=None, end=None, rel=None, order=0):
    return {
        "id": eid,
        "source": src,
        "target": tgt,
        "rel": HOLD_REL if rel is None else rel,
        "data": {
            "holder": holder,
            "start": start,
            "end": end,
            "order": order,
        },
    }


# ------------------------------------------------------------------ #
# 抽取：方向与过滤
# ------------------------------------------------------------------ #

def test_records_from_edges__direction_is_person_to_item():
    """人是 source、物品是 target。反向边不认——方向错了是建模错，不静默纠正。"""
    edges = [
        _edge("e1", "p1", "i1", holder="王五", start="2024-01-01", end="2024-06-01"),
        _edge("e2", "i1", "p2", holder="倒挂", start="2024-01-01", end="2024-02-01"),
    ]
    recs = holding_records_from_edges(item_node_id="i1", edges=edges)
    assert [r["holder"] for r in recs] == ["王五"], recs


def test_records_from_edges__other_rel_excluded():
    edges = [
        _edge("e1", "p1", "i1", holder="王五", start="2024-01-01", end="2024-06-01"),
        _edge("e2", "p2", "i1", holder="路过", start="2024-01-01", end="2024-06-01",
              rel="涉及"),
    ]
    recs = holding_records_from_edges(item_node_id="i1", edges=edges)
    assert [r["holder"] for r in recs] == ["王五"]


def test_records_from_edges__holder_falls_back_to_node_label():
    edges = [{"id": "e1", "source": "p1", "target": "i1", "rel": HOLD_REL,
              "data": {"start": "2024-01-01", "end": "2024-06-01"}}]
    recs = holding_records_from_edges(
        item_node_id="i1", edges=edges, label_by_id={"p1": "张卫国"})
    assert recs[0]["holder"] == "张卫国"


# ------------------------------------------------------------------ #
# 空链：必须说清是"哪种空"
# ------------------------------------------------------------------ #

def test_empty_chain__reason_names_data_source_not_absence():
    """无持有边 ≠ 无人持有。原因必须指向数据源，否则正兵会当成结论。"""
    r = build_item_hold_chain(item_ref="i1", item_node_id="i1", edges=[])
    assert r["flat"] is True
    assert r["steps"] == []
    assert "数据源" in r["reason"], r["reason"]
    assert r["data_source"] == "canvas_edges"


# ------------------------------------------------------------------ #
# 时态边核心：精度决定"能不能判重叠"
# ------------------------------------------------------------------ #

def test_overlap__minute_precision_is_a_real_conflict():
    """分钟档真重叠 → 冲突。这是套牌/产权异常的信号，不是噪声。"""
    edges = [
        _edge("e1", "p1", "i1", holder="王五",
              start="2024-03-10 14:35:00", end="2024-03-10 16:40:00", order=0),
        _edge("e2", "p2", "i1", holder="李志强",
              start="2024-03-10 15:10:00", end="2024-03-10 17:20:00", order=1),
    ]
    r = build_item_hold_chain(item_ref="i1", item_node_id="i1", edges=edges)
    assert r["conflicts"], r
    assert r["unknown_overlaps"] == []


def test_overlap__date_precision_is_unknown_not_conflict():
    """date 档判不出"同时"，只能列未知。

    把这条并入 conflicts 就是伪精确——同物"先后一天"会被读成"确认同时持有"，
    与三维交汇侧"同地异时不算同框"同源。
    """
    edges = [
        _edge("e1", "p1", "i1", holder="王五",
              start="2024-03-10", end="2024-06-01", order=0),
        _edge("e2", "p2", "i1", holder="李志强",
              start="2024-03-11", end="2024-09-01", order=1),
    ]
    r = build_item_hold_chain(item_ref="i1", item_node_id="i1", edges=edges)
    assert r["conflicts"] == [], r["conflicts"]
    assert r["unknown_overlaps"], r


def test_overlap__zero_minute_is_hour_not_minute():
    """14:00:00 是 hour 档（全零不提精度），不能判"同时"。

    这条守护的是"看着像分钟档其实不是"的陷阱：整点时刻写得再全也不提精度。
    """
    edges = [
        _edge("e1", "p1", "i1", holder="王五",
              start="2024-03-10 14:00:00", end="2024-03-10 16:00:00", order=0),
        _edge("e2", "p2", "i1", holder="李志强",
              start="2024-03-10 15:00:00", end="2024-03-10 17:00:00", order=1),
    ]
    r = build_item_hold_chain(item_ref="i1", item_node_id="i1", edges=edges)
    assert r["conflicts"] == [], r["conflicts"]
    assert r["unknown_overlaps"], r


def test_out_of_order__flagged_not_reordered():
    """登记顺序与登记时间矛盾只标记、不重排——那是虚假登记的线索本身。"""
    edges = [
        _edge("e1", "p1", "i1", holder="王五",
              start="2024-06-01", end="2024-09-01", order=0),
        _edge("e2", "p2", "i1", holder="李志强",
              start="2024-01-01", end="2024-03-01", order=1),
    ]
    r = build_item_hold_chain(item_ref="i1", item_node_id="i1", edges=edges)
    assert r["out_of_order"], r
    # 链序仍按登记顺序，绝不按时间重排
    assert [s["holder"] for s in r["steps"]] == ["王五", "李志强"]


def test_all_unknown_ranges__flat_with_reason():
    """全部环节时间不可判定 → flat（列表比图清楚，是刻意选择，不是降级）。"""
    edges = [
        _edge("e1", "p1", "i1", holder="王五", order=0),
        _edge("e2", "p2", "i1", holder="李志强", order=1),
    ]
    r = build_item_hold_chain(item_ref="i1", item_node_id="i1", edges=edges)
    assert r["flat"] is True
    assert "不可判定" in r["reason"], r["reason"]


# ------------------------------------------------------------------ #
# 反向验证守卫：这些断言一旦失效，说明红线被破坏
# ------------------------------------------------------------------ #

def test_mutation_guards__documented():
    """本文件的断言对以下变异敏感（改动者请手工复验）：

    M1 把 holding_records_from_edges 的方向判断改成"source 或 target 任一命中"
       → test_records_from_edges__direction_is_person_to_item 必须失败
    M2 把 hold_conflicts 的 unknown 分支并入 conflicts
       → test_overlap__date_precision_is_unknown_not_conflict 必须失败
    M3 删掉空链的 data_source / 原因文案
       → test_empty_chain__reason_names_data_source_not_absence 必须失败
    """
    assert HOLD_REL == "持有"
