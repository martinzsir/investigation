"""三维交汇读面 / 端点测试。

守的红线（每条都有反向验证——改坏实现必须红）：
  1. score 不得单独呈现：每条必须带三维各自的 count/precision/weight
  2. 重名不猜：ambiguous 标出，且**不得**静默并入任一候选
  3. 裁决未生效必须可见：conn 拿不到 → diagnostics.homonym_resolution
     = unavailable，且带 effect 说明（这是最危险的静默失效）
  4. 分页不得被 core 层 top_n 截断（total 必须等于筛选后全集）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import convergence_view as CV          # noqa: E402
from server.app.clues_artifact import (                # noqa: E402
    save_case_observations,
)


def _obs(oid: str, skill: str, detail: dict, **kw) -> dict:
    d = {"observation_id": oid, "skill_id": skill, "lens_name": skill,
         "title": f"{skill} 观察", "subject": "", "project": "",
         "basis": "判据", "falsification": "证伪条件",
         "claims": [], "facts": [], "evidence_refs": [],
         "detail": detail, "param_source": "test",
         "degraded": False, "degraded_reason": "", "source": "batch"}
    d.update(kw)
    return d


def _case(observations: list[dict], version: int = 3) -> Path:
    d = Path(tempfile.mkdtemp())
    save_case_observations(d, version, observations)
    return d


# ----------------------------------------------------------------------
# 基础：三维命中与未命中如实
# ----------------------------------------------------------------------
def test_three_dim_hit_and_miss_reasons():
    obs = [
        _obs("o1", "geo_anomaly", {"subject": "张卫国", "anomalies": [
            {"kind": "off_route", "location_id": "loc_a",
             "std_address": "拱墅区莫干山路111号", "date": "2020-03-10",
             "start": "2020-03-10 14:35:00", "end": "2020-03-10 16:40:00",
             "duration_minutes": 125, "co_present": ["李志强"],
             "event_pks": ["t1"]}]}),
        _obs("o2", "timeline_sequence", {"subject": "张卫国", "events": [
            {"date": "2020-03-10", "type": "轨迹", "event_pk": "t1",
             "brief": "到访"}]}),
    ]
    case = _case(obs)
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=2)
    assert r["available"] is True
    rows = r["convergences"]
    assert len(rows) >= 1
    c = rows[0]
    assert c["hit_dimensions"] == ["space", "time"]
    # 未命中维度必须给原因，不能用他维热度盖过去
    assert c["dimensions"]["relation"]["hit"] is False
    assert c["dimensions"]["relation"]["reason"]
    assert any("未命中维度" in x for x in c["claims"])


def test_precision_weighted_not_by_count():
    """15 次 date 档不得压过 1 次 minute 档——反了就是替正兵排错序。

    时刻用**非整点**：整点会被派生为 hour 档（防"同落 00:00:00
    冒充分钟级"的既有红线），取不到 minute 档就测不到最强证据。
    """
    # 15 条日期级锚点（只有日期，无时刻）→ 同地异时
    many_anoms = [{"kind": "off_route", "location_id": "loc_m",
                   "std_address": "文三路", "date": "2020-01-01",
                   "start": "2020-01-01", "end": "2020-01-01",
                   "duration_minutes": None, "co_present": [],
                   "event_pks": []} for _ in range(15)]
    many = _obs("o_many", "geo_anomaly",
                {"subject": "王五", "anomalies": many_anoms})
    # 1 条时刻级锚点（时间窗真重叠）
    few = _obs("o_few", "geo_anomaly", {"subject": "李四", "anomalies": [
        {"kind": "off_route", "location_id": "loc_b",
         "std_address": "莫干山路", "date": "2020-03-10",
         "start": "2020-03-10 14:35:00", "end": "2020-03-10 16:40:00",
         "duration_minutes": 125, "co_present": [], "event_pks": []}]})
    case = _case([many, few])
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=1)
    dist = {c["person_names"][0]: c["dimensions"]["space"]
            for c in r["convergences"]}
    assert dist["李四"]["precision"] == "minute", dist["李四"]
    assert dist["王五"]["precision"] == "date", dist["王五"]
    assert dist["王五"]["count"] == 15
    assert dist["李四"]["count"] == 1
    # 精度权重 minute=1.0 > date=0.2，count 堆到上限也翻不过来
    assert dist["李四"]["weight"] > dist["王五"]["weight"], (
        dist["李四"]["weight"], dist["王五"]["weight"])


def test_score_never_alone():
    """红线 1：每条必须带三维各自的明细，不能只给总分。"""
    obs = [_obs("o1", "geo_anomaly", {"subject": "张卫国", "anomalies": [
        {"kind": "off_route", "location_id": "loc_a", "date": "2020-03-10",
         "start": "2020-03-10 14:35:00", "end": "2020-03-10 16:40:00",
         "duration_minutes": 125, "co_present": [], "event_pks": []}]})]
    case = _case(obs)
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=1)
    c = r["convergences"][0]
    for dim in ("space", "time", "relation"):
        assert dim in c["dimensions"], f"缺维度 {dim}"
        d = c["dimensions"][dim]
        for k in ("count", "precision", "weight", "hit"):
            assert k in d, f"{dim} 缺字段 {k}"


# ----------------------------------------------------------------------
# 筛选 / 分页
# ----------------------------------------------------------------------
def test_filters_and_pagination_total():
    obs = []
    for i in range(7):
        obs.append(_obs(f"o{i}", "geo_anomaly", {"subject": f"主体{i}",
                   "anomalies": [{"kind": "off_route",
                                  "location_id": f"loc_{i}",
                                  "date": f"2020-03-{10 + i:02d}",
                                  "start": f"2020-03-{10 + i:02d} 14:00:00",
                                  "end": f"2020-03-{10 + i:02d} 16:00:00",
                                  "duration_minutes": 120,
                                  "co_present": [], "event_pks": []}]}))
        obs.append(_obs(f"t{i}", "timeline_sequence", {"subject": f"主体{i}",
                   "events": [{"date": f"2020-03-{10 + i:02d}",
                               "type": "轨迹", "event_pk": f"e{i}"}]}))
    case = _case(obs)
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=2,
                                page=1, page_size=3)
    # 7 条全部二维命中：total 必须是 7，不能被 core top_n 截断
    assert r["total"] == 7, r["total"]
    assert r["returned"] == 3
    assert len(r["convergences"]) == 3
    r2 = CV.assemble_convergence(case_dir=case, version=3, min_dims=2,
                                 page=3, page_size=3)
    assert r2["returned"] == 1

    r3 = CV.assemble_convergence(case_dir=case, version=3, min_dims=2,
                                 person="主体3")
    assert r3["total"] == 1
    assert "主体3" in r3["convergences"][0]["person_names"]

    r4 = CV.assemble_convergence(case_dir=case, version=3, min_dims=2,
                                 date_from="2020-03-13", date_to="2020-03-14")
    assert r4["total"] == 2


def test_dim_filter():
    obs = [
        _obs("o1", "geo_anomaly", {"subject": "张三", "anomalies": [
            {"kind": "off_route", "location_id": "loc_a", "date": "2020-03-10",
             "start": "2020-03-10 14:00:00", "end": "2020-03-10 16:00:00",
             "duration_minutes": 120, "co_present": [], "event_pks": []}]}),
    ]
    case = _case(obs)
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=1,
                                dim="space")
    assert r["total"] == 1
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=1,
                                dim="relation")
    assert r["total"] == 0


# ----------------------------------------------------------------------
# 红线 3：裁决未生效必须可见（反向验证）
# ----------------------------------------------------------------------
def test_homonym_unavailable_is_visible():
    obs = [_obs("o1", "geo_anomaly", {"subject": "张卫国", "anomalies": [
        {"kind": "off_route", "location_id": "loc_a", "date": "2020-03-10",
         "start": "2020-03-10 14:00:00", "end": "2020-03-10 16:00:00",
         "duration_minutes": 120, "co_present": [], "event_pks": []}]})]
    case = _case(obs)
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=1,
                                conn=None,
                                conn_error="无法打开案件库：FileNotFoundError")
    hom = r["diagnostics"]["homonym_resolution"]
    assert hom["status"] == "unavailable", hom
    assert "FileNotFoundError" in hom["detail"]
    # 必须写明后果——否则正兵不知道重名已被静默合并
    assert "静默合并" in hom["effect"]

    r2 = CV.assemble_convergence(case_dir=case, version=3, min_dims=1,
                                 conn=object(), conn_error=None)
    assert r2["diagnostics"]["homonym_resolution"]["status"] == "ok"


# ----------------------------------------------------------------------
# 空档案 / 详情
# ----------------------------------------------------------------------
def test_empty_observations_available_false_equivalent():
    case = _case([], version=1)
    r = CV.assemble_convergence(case_dir=case, version=1, min_dims=2)
    assert r["available"] is True
    assert r["total"] == 0
    assert r["convergences"] == []


def test_detail_and_404():
    obs = [
        _obs("o1", "geo_anomaly", {"subject": "张卫国", "anomalies": [
            {"kind": "off_route", "location_id": "loc_a",
             "std_address": "莫干山路111号", "date": "2020-03-10",
             "start": "2020-03-10 14:35:00", "end": "2020-03-10 16:40:00",
             "duration_minutes": 125, "co_present": ["李志强"],
             "event_pks": ["t1"]}]}),
        _obs("o2", "timeline_sequence", {"subject": "张卫国", "events": [
            {"date": "2020-03-10", "type": "轨迹", "event_pk": "t1",
             "brief": "到访"}]}),
    ]
    case = _case(obs)
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=2)
    key = r["convergences"][0]["key"]
    d = CV.assemble_convergence_detail(case_dir=case, version=3,
                                       conv_key=key)
    assert d is not None
    assert d["convergence"]["key"] == key
    # 支撑观察按维度归好，正兵能顺着 obs_id 回档案
    assert any(x["observation_id"] == "o1" for x in d["support"]["space"])
    assert any(x["observation_id"] == "o2" for x in d["support"]["time"])
    assert CV.assemble_convergence_detail(case_dir=case, version=3,
                                          conv_key="不存在|2020-01-01") is None


def test_directed_observations_included():
    """定向深挖的观察必须进交汇——那是正兵显式发起的研判。"""
    case = _case([_obs("o1", "geo_anomaly", {"subject": "张三", "anomalies": [
        {"kind": "off_route", "location_id": "loc_a", "date": "2020-03-10",
         "start": "2020-03-10 14:00:00", "end": "2020-03-10 16:00:00",
         "duration_minutes": 120, "co_present": [], "event_pks": []}]})])
    art = Path(case) / "artifacts"
    (art / "directed_observations.json").write_text(json.dumps(
        {"observations": [_obs("d1", "timeline_sequence",
                               {"subject": "张三", "events": [
                                   {"date": "2020-03-10", "type": "轨迹",
                                    "event_pk": "e9"}]},
                               source="directed")]}, ensure_ascii=False),
        encoding="utf-8")
    r = CV.assemble_convergence(case_dir=case, version=3, min_dims=2)
    assert r["total"] == 1
    assert r["convergences"][0]["dimensions"]["time"]["hit"] is True
