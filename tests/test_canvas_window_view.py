"""节点窗口读侧装配测试（WIN-06 证据窗口真实数据）。

真跑，不是静态检查。命中的观察用 dict 直接喂（_load_obs 对 dict 走
dict(o) 分支），避免为了测试去造 Observation 对象。

精度断言一律用 _rank 比较而**不硬编码 "minute"/"date"**：
派生口径归 core.time_semantics，测试若写死字符串，本体/时间语义一改
这里就假失败；用 rank 断言的是"分钟级必须排在日期级前面"这条业务口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.app import canvas_window_view as v  # noqa: E402


@pytest.fixture
def obs(monkeypatch):
    """替换观察装载：返回可控列表。"""
    store: list[dict] = []

    def _batch(case_dir, version):
        return list(store)

    def _directed(case_dir):
        return []

    monkeypatch.setattr(v, "load_case_observations", _batch)
    monkeypatch.setattr(v, "load_directed_observations", _directed)
    return store


def _o(oid, skill, facts, **kw):
    d = {
        "observation_id": oid,
        "skill_id": skill,
        "lens_name": "镜头",
        "title": f"观察{oid}",
        "subject": "",
        "basis": "判据",
        "falsification": "证伪条件",
        "facts": facts,
        "degraded": False,
        "degraded_reason": "",
    }
    d.update(kw)
    return d


def _result_node(lens_id=None, dims=None, precision="date"):
    return {
        "id": "case#c1:analysis_result:r1",
        "kind": "analysis_result",
        "props": {
            "lens_id": lens_id,
            "dims": dims or ["space"],
            "precision": precision,
        },
    }


# ------------------------------------------------------------------ #
# 基础分派
# ------------------------------------------------------------------ #
def test_non_result_kind_uses_canvas_memory(obs):
    """subject/place/event 不该查后端——发一个必然为空的请求是白白等待。"""
    node = {"id": "case#c1:subject:p1", "kind": "subject", "props": {}}
    out = v.assemble_window(case_dir="/tmp/x", version=1, node=node)
    assert out["server_sourced"] is False
    assert out["supports"] == []
    assert "画布内存数据" in out["note"]


def test_dim_of_mapping():
    assert v.dim_of("geo_accompany") == "space"
    assert v.dim_of("timeline_sequence") == "time"
    assert v.dim_of("relation_path") == "relation"
    assert v.dim_of("geo_unknown_skill") == "space"
    assert v.dim_of("") is None


# ------------------------------------------------------------------ #
# 匹配口径
# ------------------------------------------------------------------ #
def test_match_by_lens_id(obs):
    obs.append(_o("o1", "geo_accompany", [{"at": "2020-03-10 14:35"}]))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id="geo_accompany"))
    assert out["match"]["mode"] == "lens_id"
    assert [r["obs_id"] for r in out["supports"]] == ["o1"]
    assert out["supports"][0]["dim"] == "space"


def test_match_by_directed_origin(obs):
    obs.append(_o("o2", "geo_stay", [{"at": "2020-03-10"}],
                  origin={"node_id": "case#c1:analysis_result:r1"}))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id=None),
                            node_id="case#c1:analysis_result:r1")
    assert out["match"]["mode"] == "directed"


def test_match_by_observation_ids_exact_excludes_batch(obs):
    """growth 聚合节点自带 observation_ids：只认列表里的观察，
    同镜头的批量扫描观察（无本靶心 origin）不得混入（R-3）。"""
    obs.append(_o("keep", "relation_org_interest", [],
                  origin={"node_id": "case#c1:subject:p1"}))
    obs.append(_o("drop_batch", "relation_org_interest", []))
    node = _result_node(lens_id="relation_org_interest")
    node["props"]["observation_ids"] = ["keep"]
    node["props"]["target_node_id"] = "case#c1:subject:p1"
    out = v.assemble_window(case_dir="/tmp/x", version=1, node=node)
    assert out["match"]["mode"] == "observation_ids"
    assert [r["obs_id"] for r in out["supports"]] == ["keep"]


def test_match_lens_scoped_by_target_node(obs):
    """没带观察编号时：同镜头也要按 origin.node_id 靶心收窄，
    别的靶心/批量扫描的同镜头观察不混入。"""
    obs.append(_o("mine", "geo_anomaly", [],
                  origin={"node_id": "case#c1:subject:p1"}))
    obs.append(_o("other", "geo_anomaly", [],
                  origin={"node_id": "case#c1:subject:p2"}))
    obs.append(_o("batch", "geo_anomaly", []))
    node = _result_node(lens_id="geo_anomaly")
    node["props"]["target_node_id"] = "case#c1:subject:p1"
    out = v.assemble_window(case_dir="/tmp/x", version=1, node=node)
    assert out["match"]["mode"] == "lens_and_target"
    assert [r["obs_id"] for r in out["supports"]] == ["mine"]


def test_no_match_writes_reason_not_empty_list(obs):
    """匹配不上必须写明原因——空列表会被读成'确实没有支撑'。"""
    obs.append(_o("o3", "geo_accompany", [{"at": "2020-03-10 14:35"}]))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id=None))
    assert out["match"]["mode"] == "none"
    assert "无从定位" in out["match"]["reason"]
    assert out["supports"] == []


def test_never_sweep_by_dim_when_unmatched(obs):
    """有 dims 但无从定位时，**不许**退化成按维度全捞。

    否则本案所有 space 观察都会被算成这条结论的支撑——
    那正是"支撑"两个字最危险的失真。
    """
    for i in range(5):
        obs.append(_o(f"s{i}", "geo_accompany", [{"at": "2020-03-10 14:35"}]))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id=None, dims=["space"]))
    assert out["supports"] == []
    assert out["match"]["mode"] == "none"


# ------------------------------------------------------------------ #
# 红线：每条支撑带自己的精度，不继承节点精度
# ------------------------------------------------------------------ #
def test_support_precision_not_inherited(obs):
    """节点是 date 档，但支撑观察里有一条分钟级事实 → 必须显示分钟级。

    继承节点精度会让"这条结论其实有一分钟级硬证据"被抹平。
    """
    obs.append(_o("o1", "geo_accompany", [{"at": "2020-03-10 14:35"}]))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id="geo_accompany",
                                              precision="date"))
    p = out["supports"][0]["precision"]
    assert v._rank(p) < v._rank("date"), f"精度被节点 date 档污染：{p}"


def test_precision_weaker_when_only_date_facts(obs):
    obs.append(_o("o1", "geo_accompany", [{"at": "2020-03-10"}]))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id="geo_accompany"))
    p = out["supports"][0]["precision"]
    assert v._rank(p) >= v._rank("date"), f"日期级事实不该被拔高：{p}"


# ------------------------------------------------------------------ #
# 红线：排序按精度，不按事实条数
# ------------------------------------------------------------------ #
def test_sort_precision_before_count(obs):
    """12 条日期级 vs 3 条分钟级：分钟级必须在前。

    关系窗口排序踩过一次（同分退回裸次数，15 次又排回前面），
    这里守住同一条口径。
    """
    many_date = [{"at": f"2020-03-{d:02d}"} for d in range(1, 13)]
    few_minute = [{"at": f"2020-03-10 14:{m:02d}"} for m in range(35, 38)]
    obs.append(_o("many", "geo_accompany", many_date))
    obs.append(_o("few", "geo_accompany", few_minute))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id="geo_accompany"))
    ids = [r["obs_id"] for r in out["supports"]]
    assert ids[0] == "few", f"按条数排了，日期级 12 条排到了前面：{ids}"


# ------------------------------------------------------------------ #
# 截断与分维度
# ------------------------------------------------------------------ #
def test_truncated_flag(obs):
    for i in range(6):
        obs.append(_o(f"o{i}", "geo_accompany", [{"at": "2020-03-10 14:35"}]))
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id="geo_accompany"),
                            support_limit=4)
    assert out["truncated"] is True
    assert out["shown"] == 4 and out["total"] == 6


def test_by_dim_grouping(obs):
    obs.append(_o("o1", "geo_accompany", [{"at": "2020-03-10 14:35"}]))
    obs.append(_o("o2", "timeline_sequence", [{"at": "2020-03-10"}]))
    node = _result_node(lens_id="geo_accompany")
    node["props"]["lens_id"] = None
    # 两条不同 skill，无 lens_id → 走不到；改用定向匹配两条一起进来
    for o in obs:
        o["origin"] = {"node_id": "case#c1:analysis_result:r1"}
    out = v.assemble_window(case_dir="/tmp/x", version=1, node=node,
                            node_id="case#c1:analysis_result:r1")
    assert set(out["by_dim"].keys()) == {"space", "time"}


def test_weight_model_exposed(obs):
    out = v.assemble_window(case_dir="/tmp/x", version=1,
                            node=_result_node(lens_id="geo_accompany"))
    assert "weight_model" in out
