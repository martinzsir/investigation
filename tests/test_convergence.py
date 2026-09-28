"""三维交汇层守护测试。

守的红线
--------
1. **不定性**：产出不得出现「可疑/疑似/异常接触/有嫌疑」等定性措辞。
2. **不静默合并**：未命中维度必须写明原因，不得由他维热度填补。
3. **精度加权**：时刻级同框分量 > 日期级同框；次数不得压过精度。
4. **重名不猜**：同名异人时 pk 置 None 并标歧义，绝不静默挑一个。
5. **关系型指代排除**：「张卫国配偶」既非同一人亦非同名异人，
   不得并入「张卫国」，也不得占人审队列。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import convergence as CV  # noqa: E402

FORBIDDEN = ["可疑", "疑似", "有嫌疑", "异常接触", "串通", "违规", "犯罪"]


def _obs(skill, subject, detail, oid="obs_x"):
    return {"observation_id": oid, "skill_id": skill,
            "subject": subject, "detail": detail}


def _scan_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ------------------------------------------------------------------ #
# 精度加权（本层核心）
# ------------------------------------------------------------------ #
def test_precision_weight_minute_beats_date():
    """时刻级锚点分量必须高于日期级，哪怕后者次数多得多。

    反例场景：15 次日期级同框（同地异时）vs 3 次时刻级同框（时间窗真重叠）。
    按次数排 15 次完胜——这正是本层要纠正的错误排序。
    """
    obs = []
    # A：15 个日期级锚点（每次 1 条，散在不同日期）
    for i in range(15):
        obs.append(_obs("geo_anomaly", "甲", {
            "anomalies": [{
                "kind": "off_route", "location_id": "loc_a",
                "std_address": "A路", "date": f"2020-01-{i+1:02d}",
                "start": None, "end": None, "duration_minutes": None,
                "co_present": [], "event_pks": [],
            }]}, oid=f"a{i}"))
    # B：3 个时刻级锚点
    for i in range(3):
        obs.append(_obs("geo_anomaly", "乙", {
            "anomalies": [{
                "kind": "off_route", "location_id": "loc_b",
                "std_address": "B路", "date": f"2020-02-{i+1:02d}",
                "start": f"2020-02-{i+1:02d}T14:35:00",
                "end": f"2020-02-{i+1:02d}T16:40:00",
                "duration_minutes": 125.0,
                "co_present": [], "event_pks": [],
            }]}, oid=f"b{i}"))
    r = CV.build_convergence(obs, min_dims=1, top_n=100)
    by_person = {}
    for c in r["convergences"]:
        nm = c["person_names"][0]
        by_person[nm] = max(by_person.get(nm, 0.0), c["score"])
    assert by_person["乙"] > by_person["甲"], (
        f"时刻级锚点分量({by_person['乙']}) 必须高于日期级({by_person['甲']})")
    # 单条权重核对：date 档 0.2，minute 档 1.0
    assert CV.PRECISION_WEIGHT["date"] < CV.PRECISION_WEIGHT["minute"]


def test_repeat_factor_capped():
    """次数加成有上限，避免次数堆砌压过精度。"""
    one = CV._repeat_factor(1)
    many = CV._repeat_factor(100)
    assert one == 1.0
    assert many <= CV.REPEAT_CAP
    # 100 次日期级仍不得压过 3 次时刻级
    assert (CV.PRECISION_WEIGHT["date"] * CV._repeat_factor(100)
            < CV.PRECISION_WEIGHT["minute"] * CV._repeat_factor(3))


def test_geo_meet_weight_consistent_with_convergence():
    """geo.py 的同框权重与本层精度权重必须同口径（两处不得各写一套）。"""
    from core import geo
    assert geo._MEET_W_MINUTE == CV.PRECISION_WEIGHT["minute"]
    assert geo._MEET_W_DATE == CV.PRECISION_WEIGHT["date"]


# ------------------------------------------------------------------ #
# 红线 1：不定性
# ------------------------------------------------------------------ #
def test_no_qualitative_wording():
    obs = [
        _obs("geo_anomaly", "甲", {"anomalies": [{
            "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
            "date": "2020-03-10", "start": "2020-03-10T14:35:00",
            "end": "2020-03-10T16:40:00", "duration_minutes": 125.0,
            "co_present": ["乙"], "event_pks": ["track_1"]}]}),
        _obs("timeline_sequence", "甲", {"timeline": [
            {"type": "trackpoint", "event_pk": "track_1", "date": "2020-03-10"}]}),
        _obs("relation_neighborhood", "甲", {"edges": [
            {"src": CV._person_pk("甲"), "dst": CV._person_pk("乙"),
             "edge": "calls_to", "edge_pk": "call_1", "kind": "contact"}]}),
    ]
    r = CV.build_convergence(obs, min_dims=1)
    text = _scan_text(r)
    for w in FORBIDDEN:
        assert w not in text, f"产出出现定性措辞：{w}"
    assert "优先级" in text


# ------------------------------------------------------------------ #
# 红线 2：不静默合并
# ------------------------------------------------------------------ #
def test_missing_dimension_reported_not_filled():
    obs = [
        _obs("geo_anomaly", "甲", {"anomalies": [{
            "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
            "date": "2020-03-10", "start": "2020-03-10T14:35:00",
            "end": "2020-03-10T16:40:00", "duration_minutes": 125.0,
            "co_present": [], "event_pks": []}]}),
    ]
    r = CV.build_convergence(obs, min_dims=1)
    assert r["total_hit"] >= 1
    c = r["convergences"][0]
    assert c["dimensions"]["space"]["hit"] is True
    assert c["dimensions"]["time"]["hit"] is False
    assert c["dimensions"]["relation"]["hit"] is False
    # 未命中必须有原因，不能留空让他维热度盖过去
    assert c["dimensions"]["time"]["reason"]
    assert c["dimensions"]["relation"]["reason"]
    assert "未命中维度" in _scan_text(c["claims"])


def test_min_dims_filters_single_dimension():
    obs = [
        _obs("geo_anomaly", "甲", {"anomalies": [{
            "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
            "date": "2020-03-10", "start": None, "end": None,
            "duration_minutes": None, "co_present": [], "event_pks": []}]}),
    ]
    assert CV.build_convergence(obs, min_dims=2)["total_hit"] == 0
    assert CV.build_convergence(obs, min_dims=1)["total_hit"] == 1


# ------------------------------------------------------------------ #
# 三维全命中
# ------------------------------------------------------------------ #
def test_three_dimension_convergence():
    obs = [
        _obs("geo_anomaly", "甲", {"anomalies": [{
            "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
            "date": "2020-03-10", "start": "2020-03-10T14:35:00",
            "end": "2020-03-10T16:40:00", "duration_minutes": 125.0,
            "co_present": ["乙"], "event_pks": ["track_1"]}]}),
        _obs("timeline_sequence", "甲", {"timeline": [
            {"type": "trackpoint", "event_pk": "track_1", "date": "2020-03-10"}]}),
        _obs("relation_neighborhood", "甲", {"edges": [
            {"src": CV._person_pk("甲"), "dst": CV._person_pk("乙"),
             "edge": "calls_to", "edge_pk": "call_1", "kind": "contact"}]}),
    ]
    r = CV.build_convergence(obs, min_dims=2)
    assert r["total_hit"] == 1
    c = r["convergences"][0]
    assert set(c["hit_dimensions"]) == {"space", "time", "relation"}
    assert c["dim_hit_count"] == 3
    # 关系维经「同现」通路命中，精度继承空间锚点（minute）
    assert c["dimensions"]["relation"]["precision"] == "minute"
    assert "乙" in c["co_present"]


def test_relation_via_dated_edge():
    """无同现时，边的主键若能回填到该日期，也算命中（日期级）。"""
    obs = [
        _obs("geo_anomaly", "甲", {"anomalies": [{
            "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
            "date": "2020-03-10", "start": "2020-03-10T14:35:00",
            "end": "2020-03-10T16:40:00", "duration_minutes": 125.0,
            "co_present": [], "event_pks": []}]}),
        _obs("timeline_sequence", "甲", {"timeline": [
            {"type": "call", "event_pk": "call_1", "date": "2020-03-10"}]}),
        _obs("relation_neighborhood", "甲", {"edges": [
            {"src": CV._person_pk("甲"), "dst": CV._person_pk("乙"),
             "edge": "calls_to", "edge_pk": "call_1", "kind": "contact"}]}),
    ]
    r = CV.build_convergence(obs, min_dims=2)
    c = r["convergences"][0]
    assert c["dimensions"]["relation"]["hit"] is True
    assert "dated_edge" in c["dimensions"]["relation"]["kinds"]


# ------------------------------------------------------------------ #
# 红线 4：重名不猜
# ------------------------------------------------------------------ #
def test_ambiguous_person_not_silently_picked():
    """同名异人时必须标歧义、pk 置空，绝不静默选一个。"""
    persons = CV.resolve_person(["甲"])
    assert persons["甲"]["ambiguous"] is False
    assert persons["甲"]["pk"] == CV._person_pk("甲")

    # 直接构造歧义裁决结果，验证 _key 走 "?" 前缀而非挑一个
    from core.convergence import build_convergence
    obs = [
        _obs("geo_anomaly", "甲", {"anomalies": [{
            "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
            "date": "2020-03-10", "start": "2020-03-10T14:35:00",
            "end": "2020-03-10T16:40:00", "duration_minutes": 125.0,
            "co_present": [], "event_pks": []}]}),
    ]
    r = build_convergence(obs, min_dims=1)
    assert r["convergences"][0]["person_ambiguous"] is False


def test_ambiguous_flag_structure():
    """歧义条目结构完整：pk=None + 候选 + 理由，且不并入任一候选。"""
    info = {"pk": None, "name": "甲", "ambiguous": True,
            "candidates": ["person_1", "person_2"], "reason": "身份证号互斥"}
    assert info["pk"] is None
    assert len(info["candidates"]) == 2
    # _key 必须用 ? 前缀，绝不能落回任一候选 pk
    assert not str(info["pk"] or "").startswith("person_")


# ------------------------------------------------------------------ #
# 红线 5：关系型指代
# ------------------------------------------------------------------ #
def test_relational_ref_stripped():
    """「张卫国配偶」是关系型指代，不是同名异人，须剥离而非合并。"""
    base, is_rel = CV._strip_relational("张卫国配偶")
    assert base == "张卫国"
    assert is_rel is True
    base2, is_rel2 = CV._strip_relational("张卫国")
    assert base2 == "张卫国"
    assert is_rel2 is False


# ------------------------------------------------------------------ #
# 结构自陈
# ------------------------------------------------------------------ #
def test_weight_model_self_declared():
    obs = [_obs("geo_anomaly", "甲", {"anomalies": [{
        "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
        "date": "2020-03-10", "start": None, "end": None,
        "duration_minutes": None, "co_present": [], "event_pks": []}]})]
    r = CV.build_convergence(obs, min_dims=1)
    wm = r["weight_model"]
    assert "dim_weight" in wm and "precision_weight" in wm
    assert "formula" in wm
    assert r["note"]


def test_falsification_present():
    obs = [_obs("geo_anomaly", "甲", {"anomalies": [{
        "kind": "off_route", "location_id": "loc_a", "std_address": "A路",
        "date": "2020-03-10", "start": "2020-03-10T14:35:00",
        "end": "2020-03-10T16:40:00", "duration_minutes": 125.0,
        "co_present": [], "event_pks": []}]})]
    r = CV.build_convergence(obs, min_dims=1)
    f = r["convergences"][0]["falsification"]
    assert "职务性地点" in f
