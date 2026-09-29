"""CAN-19 研判结果挂到发起它的靶心节点下——逐条可独立验证。

每条用例只测一件事，不依赖前序用例的执行结果。
"""
from __future__ import annotations

import json

from server.app import canvas_growth as G
from server.app.canvas_case import (LENS_RESULT_REL, SYSTEM_RELS_CASE,
                                    can_overlap, case_node_id, line_style)

TARGET = case_node_id("subject", "C1", "person_zhang")
OTHER = case_node_id("subject", "C1", "person_li")

MINUTE = "2020-03-10 14:35:00"
DATE = "2020-03-10"


def _obs(oid: str, skill: str, *, node_id: str | None = None,
         at: str | None = None, run: str = "run1") -> dict:
    return {
        "observation_id": oid,
        "skill_id": skill,
        "facts": ([{"at": at}] if at else []),
        "origin": ({"node_id": node_id} if node_id else {}),
        "run_id": run,
    }


# ----------------------------------------------------------------------
# 分组：按 (靶心 × 镜头) 聚合（R-1）
# ----------------------------------------------------------------------
def test_group_aggregates_per_target_and_lens():
    obs = [_obs(f"o{i}", "geo_anomaly_track", node_id=TARGET) for i in range(3)]
    groups, _ = G.group_by_target(obs, TARGET)
    assert list(groups.keys()) == [(TARGET, "geo_anomaly_track")]
    assert len(groups[(TARGET, "geo_anomaly_track")]) == 3


def test_group_separates_different_targets():
    obs = [_obs("o1", "geo_anomaly_track", node_id=TARGET),
           _obs("o2", "geo_anomaly_track", node_id=OTHER)]
    groups, _ = G.group_by_target(obs)
    assert set(groups.keys()) == {(TARGET, "geo_anomaly_track"),
                                  (OTHER, "geo_anomaly_track")}


def test_group_separates_different_lenses():
    obs = [_obs("o1", "geo_anomaly_track", node_id=TARGET),
           _obs("o2", "timeline_cross_collision", node_id=TARGET)]
    groups, _ = G.group_by_target(obs, TARGET)
    assert len(groups) == 2


# ----------------------------------------------------------------------
# R-3：挂错比不挂更坏
# ----------------------------------------------------------------------
def test_other_target_observation_excluded_not_merged():
    obs = [_obs("o1", "geo_anomaly_track", node_id=OTHER)]
    groups, excluded = G.group_by_target(obs, TARGET)
    assert groups == {}
    assert len(excluded) == 1
    assert excluded[0]["reason"] == G.EXCLUDE_OTHER_TARGET
    assert OTHER in excluded[0]["note"]


def test_no_target_recorded_and_none_specified_reports_reason():
    groups, excluded = G.group_by_target([_obs("o1", "geo_footprint")])
    assert groups == {}
    assert excluded[0]["reason"] == G.EXCLUDE_NO_TARGET


def test_unrecorded_adopted_by_specified_target():
    """早期运行没记 node_id：由调用方指定的靶心承接，而不是整批丢弃。"""
    groups, excluded = G.group_by_target([_obs("o1", "geo_footprint")], TARGET)
    assert list(groups.keys()) == [(TARGET, "geo_footprint")]
    assert excluded == []


# ----------------------------------------------------------------------
# 挂接：结论节点挂在靶心下
# ----------------------------------------------------------------------
def test_result_node_attached_under_target():
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="geo_anomaly_track",
                             observations=[_obs("o1", "geo_anomaly_track",
                                                node_id=TARGET, at=MINUTE)])
    edge = r["edge"]
    assert edge["source"] == TARGET
    assert edge["target"] == r["node"]["id"]
    assert edge["rel"] == LENS_RESULT_REL
    assert edge["system"] is True


def test_attach_rel_is_system_only():
    """人工连不出这条边——否则人工边能冒充"系统推断"的挂接。"""
    assert LENS_RESULT_REL in SYSTEM_RELS_CASE


def test_same_target_and_lens_is_idempotent():
    a = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="geo_footprint",
                             observations=[_obs("o1", "geo_footprint",
                                                node_id=TARGET)])
    b = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="geo_footprint",
                             observations=[_obs("o1", "geo_footprint",
                                                node_id=TARGET),
                                           _obs("o2", "geo_footprint",
                                                node_id=TARGET)])
    assert a["node"]["id"] == b["node"]["id"]   # 重跑不增节点


# ----------------------------------------------------------------------
# R-2：精度取木桶最弱档，unknown 先剔除
# ----------------------------------------------------------------------
def test_precision_weakest_wins():
    """1 条 minute 不该把 12 条 date 抬成实线粗边。"""
    obs = [_obs("o1", "geo_x", node_id=TARGET, at=MINUTE)]
    obs += [_obs(f"d{i}", "geo_x", node_id=TARGET, at=DATE) for i in range(12)]
    p, breakdown = G.aggregate_precision(obs)
    assert p == "date"
    assert breakdown["date"] == 12 and breakdown["minute"] == 1


def test_unknown_does_not_swallow_real_precision():
    """unknown 是木桶里最低档，一旦参与就永远赢——必须先剔除。"""
    obs = [_obs("o1", "geo_x", node_id=TARGET),          # 无时间 → unknown
           _obs("o2", "geo_x", node_id=TARGET, at=MINUTE),
           _obs("o3", "geo_x", node_id=TARGET, at=MINUTE)]
    p, _ = G.aggregate_precision(obs)
    assert p == "minute"


def test_all_unknown_is_unknown():
    p, _ = G.aggregate_precision([_obs("o1", "geo_x", node_id=TARGET)])
    assert p == "unknown"


def test_node_precision_matches_edge_line_style():
    """卡内符号与连线必须同一档——否则"图上长得一样"又回来了。"""
    obs = [_obs("o1", "geo_x", node_id=TARGET, at=DATE)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="geo_x", observations=obs)
    p = r["node"]["props"]["precision"]
    assert p == "date"
    assert r["edge"]["precision"] == p
    assert r["edge"]["stroke_dasharray"] == line_style(p)["stroke_dasharray"]
    assert can_overlap(p) is False


def test_breakdown_carried_for_evidence_window():
    obs = [_obs("o1", "geo_x", node_id=TARGET, at=MINUTE),
           _obs("o2", "geo_x", node_id=TARGET, at=DATE)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="geo_x", observations=obs)
    bd = r["node"]["props"]["precision_breakdown"]
    assert bd == {"minute": 1, "date": 1}


# ----------------------------------------------------------------------
# 维度与假设
# ----------------------------------------------------------------------
def test_dims_merged_from_supporting_observations():
    obs = [_obs("o1", "geo_x", node_id=TARGET),
           _obs("o2", "timeline_y", node_id=TARGET),
           _obs("o3", "relation_z", node_id=TARGET)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="mixed", observations=obs)
    assert set(r["node"]["props"]["dims"]) == {"space", "time", "relation"}


def test_hypothesis_auto_attached_from_function():
    """结论一挂上画布，假设自己长出来、证据自己连上去。"""
    layer = G.build_lens_result_layers(
        case_id="C1",
        observations=[_obs("o1", "lens_comm_call_frequency",
                           node_id=TARGET, at=MINUTE)],
        function_ids={"lens_comm_call_frequency": "call_frequency_spike"})
    node = layer["nodes"][0]
    assert node["props"]["assumption"] == "H3"
    assert any(n["props"]["hypothesis_id"] == "H3"
               for n in layer["hypothesis_nodes"])
    assert len(layer["infer_edges"]) == 1


def test_pending_assumption_not_guessed():
    layer = G.build_lens_result_layers(
        case_id="C1",
        observations=[_obs("o1", "lens_jian", node_id=TARGET)],
        function_ids={"lens_jian": "jian_cross_level"})
    assert layer["nodes"][0]["props"]["assumption"] is None
    assert layer["hypothesis_nodes"] == []
    assert layer["infer_edges"] == []


def test_unknown_function_no_assumption():
    layer = G.build_lens_result_layers(
        case_id="C1", observations=[_obs("o1", "lens_x", node_id=TARGET)],
        function_ids={"lens_x": "not_a_function"})
    assert layer["nodes"][0]["props"]["assumption"] is None


def test_hypothesis_node_deduped_across_groups():
    obs = [_obs("o1", "lens_a", node_id=TARGET, at=MINUTE),
           _obs("o2", "lens_b", node_id=TARGET, at=MINUTE)]
    layer = G.build_lens_result_layers(
        case_id="C1", observations=obs,
        function_ids={"lens_a": "call_frequency_spike",
                      "lens_b": "call_frequency_spike"})
    assert len(layer["hypothesis_nodes"]) == 1
    assert len(layer["infer_edges"]) == 2


# ----------------------------------------------------------------------
# 读侧
# ----------------------------------------------------------------------
def test_missing_artifact_dir_returns_reason_not_crash(tmp_path=None):
    import tempfile
    from pathlib import Path
    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    layer = G.load_growth_layer(case_dir=str(base / "nope"),
                                case_id="C1")
    assert layer["nodes"] == []
    assert layer["meta"]["reason"]


def test_load_growth_layer_reads_directed_observations(tmp_path=None):
    import tempfile
    from pathlib import Path
    from server.app.clues_artifact import directed_observations_path
    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    path = directed_observations_path(str(base))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"observations": [
        _obs("o1", "geo_anomaly_track", node_id=TARGET, at=MINUTE)]}),
        encoding="utf-8")
    layer = G.load_growth_layer(case_dir=str(base), case_id="C1")
    assert len(layer["nodes"]) == 1
    assert layer["edges"][0]["source"] == TARGET


# ----------------------------------------------------------------------
# 元信息自反查：不靠调用方记得传（CAN-19 冒烟暴露的缺口）
# ----------------------------------------------------------------------
def test_name_and_assumption_resolved_without_caller():
    """真实路径不传 lens_name / function_id，也必须出中文名与假设。

    冒烟时的症状：标签是机器编号、假设节点不长出来。根因是元信息靠调用
    方透传，而镜头路由 → 观察落库 → 画布还原隔了好几层。
    """
    obs = [_obs(f"o{i}", "fund_overpass_two_hop", node_id=TARGET,
                at=MINUTE) for i in range(2)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="fund_overpass_two_hop",
                             observations=obs)
    props = r["node"]["props"]
    assert props["lens_name"] == "两跳过桥路径镜头", props.get("lens_name")
    assert "两跳过桥路径镜头" in r["node"]["label"]
    assert props["assumption"] == "H4", props.get("assumption")
    assert props["function_id"] == "overpass_two_hop"
    assert props["meta_source"] == "catalog"


def test_caller_value_still_wins():
    """调用方显式传入优先（便于覆盖与测试注入）。"""
    obs = [_obs("o1", "fund_overpass_two_hop", node_id=TARGET, at=MINUTE)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="fund_overpass_two_hop",
                             observations=obs, lens_name="人工改名")
    assert r["node"]["props"]["lens_name"] == "人工改名"
    assert r["node"]["props"]["meta_source"] == "caller"


def test_unregistered_lens_label_is_not_machine_id():
    """R-1：查不到名字时标签退成通用名，不把 lens_id 当标题。"""
    obs = [_obs("o1", "some_unregistered_lens", node_id=TARGET, at=MINUTE)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="some_unregistered_lens",
                             observations=obs)
    assert "some_unregistered_lens" not in r["node"]["label"], \
        "不得把机器编号当标题显示"
    assert r["node"]["props"]["unresolved_name"] is True


def test_fund_lens_gets_dim_from_registry():
    """fund_/comm_ 镜头维度不再因前缀判定落空（dim_count 恒 0）。"""
    obs = [_obs("o1", "comm_call_frequency", node_id=TARGET, at=MINUTE)]
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="comm_call_frequency",
                             observations=obs)
    assert r["node"]["props"]["dims"] == ["comm"]
    assert r["node"]["props"]["dim_count"] == 1


def test_multi_function_lens_yields_multiple_infer_edges():
    """R-2：多内核多归属 → 单值置 None + 建多条推断边，不挑一个。"""
    from core import lens_catalog as C
    real = C.load_catalog          # 注意：取函数本身，不能加括号（会存成 dict）

    def fake(base_dir=None):
        cat = dict(real())
        cat["multi_fn_lens"] = {
            "lens_id": "multi_fn_lens", "name": "多内核镜头",
            "pack_id": "test",
            "functions": ["integer_transfer_aggregates", "call_frequency_spike"],
            "dims": ["fund", "comm"], "canvas_enabled": True,
        }
        return cat

    C.load_catalog = fake            # 手工 monkeypatch（不依赖 pytest）
    try:
        obs = [_obs("o1", "multi_fn_lens", node_id=TARGET, at=MINUTE)]
        layer = G.build_lens_result_layers(case_id="C1", observations=obs,
                                           target_node_id=TARGET)
        props = layer["nodes"][0]["props"]
        assert props["assumption"] is None, "多归属不得挑一个当单值"
        assert set(props["assumptions"]) == {"H4", "H3"}
        assert len(layer["infer_edges"]) == 2, "多归属应出两条推断边"
    finally:
        C.load_catalog = real


# ----------------------------------------------------------------------
# 渐进式生成：揭示集（v3 业务推演——跑一步长一步，不一次铺满）
# ----------------------------------------------------------------------
def test_empty_revealed_builds_nothing_but_lists_all_groups():
    """空揭示集：零节点，但清单仍枚举全部组（供前端逐组揭示）。"""
    obs = [_obs("o1", "lens_comm_call_frequency", node_id=TARGET, at=MINUTE),
           _obs("o2", "geo_footprint", node_id=OTHER)]
    layer = G.build_lens_result_layers(
        case_id="C1", observations=obs, revealed=set(),
        function_ids={"lens_comm_call_frequency": "call_frequency_spike"})
    assert layer["nodes"] == []
    assert layer["edges"] == []
    assert layer["hypothesis_nodes"] == []
    assert layer["infer_edges"] == []
    assert len(layer["meta"]["groups"]) == 2
    assert all(g["revealed"] is False for g in layer["meta"]["groups"])
    assert "尚未揭示" in layer["meta"]["reason"]


def test_revealed_none_keeps_legacy_full_build():
    """revealed=None：不过滤（scope 查询/旧调用方兼容，逐组用例已覆盖）。"""
    obs = [_obs("o1", "lens_comm_call_frequency", node_id=TARGET, at=MINUTE)]
    layer = G.build_lens_result_layers(
        case_id="C1", observations=obs, revealed=None,
        function_ids={"lens_comm_call_frequency": "call_frequency_spike"})
    assert len(layer["nodes"]) == 1
    assert layer["meta"]["groups"][0]["revealed"] is True


def test_partial_reveal_only_builds_revealed_group():
    """揭示一组只长这一组：另一靶心的结论/挂边都不出现。"""
    obs = [_obs("o1", "lens_comm_call_frequency", node_id=TARGET, at=MINUTE),
           _obs("o2", "geo_footprint", node_id=OTHER, at=DATE)]
    layer = G.build_lens_result_layers(
        case_id="C1", observations=obs,
        revealed={(TARGET, "lens_comm_call_frequency")},
        function_ids={"lens_comm_call_frequency": "call_frequency_spike"})
    result_nodes = [n for n in layer["nodes"]
                    if n.get("kind") == "analysis_result"]
    assert len(result_nodes) == 1
    assert result_nodes[0]["props"]["target_node_id"] == TARGET
    assert layer["edges"] and all(e["source"] == TARGET
                                  for e in layer["edges"])
    rows = {(g["target_node_id"], g["lens_id"]): g
            for g in layer["meta"]["groups"]}
    assert rows[(TARGET, "lens_comm_call_frequency")]["revealed"] is True
    assert rows[(OTHER, "geo_footprint")]["revealed"] is False


def test_hypothesis_appears_only_when_supporting_result_revealed():
    """v3 §6：首个支撑结论被揭示，H 假设才出现；推断边只连已揭示结论。"""
    obs = [_obs("o1", "lens_comm_call_frequency", node_id=TARGET, at=MINUTE),
           _obs("o2", "lens_comm_call_frequency", node_id=OTHER, at=MINUTE)]
    # 只揭示 OTHER 靶心这一组
    layer = G.build_lens_result_layers(
        case_id="C1", observations=obs,
        revealed={(OTHER, "lens_comm_call_frequency")},
        function_ids={"lens_comm_call_frequency": "call_frequency_spike"})
    assert [h["props"]["hypothesis_id"] for h in layer["hypothesis_nodes"]] \
        == ["H3"]
    assert len(layer["infer_edges"]) == 1
    src_result = next(n for n in layer["nodes"]
                      if n["id"] == layer["infer_edges"][0]["source"])
    assert src_result["props"]["target_node_id"] == OTHER

    # 两组都揭示 → 一个 H3 节点、两条推断边（假设去重不因揭示而破）
    layer2 = G.build_lens_result_layers(
        case_id="C1", observations=obs,
        revealed={(OTHER, "lens_comm_call_frequency"),
                  (TARGET, "lens_comm_call_frequency")},
        function_ids={"lens_comm_call_frequency": "call_frequency_spike"})
    assert len(layer2["hypothesis_nodes"]) == 1
    assert len(layer2["infer_edges"]) == 2


def test_group_meta_carries_canvas_status_and_labels():
    """清单行：靶心在图状态 + 中文名 + 靶心标签（供面板提示先提升主体）。"""
    obs = [_obs("o1", "fund_overpass_two_hop", node_id=TARGET),
           _obs("o2", "fund_overpass_two_hop", node_id=OTHER)]
    layer = G.build_lens_result_layers(
        case_id="C1", observations=obs, revealed=set(),
        on_canvas_ids={TARGET}, target_labels={TARGET: "张卫国"})
    rows = {g["target_node_id"]: g for g in layer["meta"]["groups"]}
    assert rows[TARGET]["on_canvas"] is True
    assert rows[TARGET]["target_label"] == "张卫国"
    assert rows[OTHER]["on_canvas"] is False
    assert rows[OTHER]["target_label"] == ""
    # 中文名（注册表反查）且不是 lens_id 本身（R-1 标签纪律在清单同样成立）
    assert rows[TARGET]["lens_name"] == "两跳过桥路径镜头"


# ----------------------------------------------------------------------
# 线索域过滤：案件级发起的观察不混进线索画布（R-3 跨域同罪）
# ----------------------------------------------------------------------
def _obs_with_clue(oid: str, clue: str | None, *, skill="geo_footprint"):
    o = _obs(oid, skill, node_id=TARGET, at=MINUTE)
    if clue is None:
        o["origin"] = {"node_id": TARGET}
    else:
        o["origin"] = {"node_id": TARGET, "clue_id": clue}
    return o


def test_clue_filter_keeps_only_matching_clue():
    obs = [_obs_with_clue("o1", "clue_a"),
           _obs_with_clue("o2", "clue_b"),
           _obs_with_clue("o3", None)]   # 案件级发起（无 clue_id）
    layer = G.build_lens_result_layers(case_id="C1", observations=obs,
                                       clue_id="clue_a")
    assert len(layer["meta"]["groups"]) == 1
    assert layer["meta"]["groups"][0]["observation_count"] == 1


def test_clue_none_sees_everything():
    obs = [_obs_with_clue("o1", "clue_a"),
           _obs_with_clue("o2", "clue_b"),
           _obs_with_clue("o3", None)]
    layer = G.build_lens_result_layers(case_id="C1", observations=obs,
                                       clue_id=None)
    assert layer["meta"]["groups"][0]["observation_count"] == 3


def test_load_growth_layer_honors_revealed_and_clue(tmp_path=None):
    """读侧接线：空揭示零节点 + clue 域过滤同时生效。"""
    import tempfile
    from pathlib import Path
    from server.app.clues_artifact import directed_observations_path
    base = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    path = directed_observations_path(str(base))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"observations": [
        _obs_with_clue("o1", "clue_a"),
        _obs_with_clue("o2", "clue_b")]}), encoding="utf-8")
    layer = G.load_growth_layer(case_dir=str(base), case_id="C1",
                                revealed=set(), clue_id="clue_a")
    assert layer["nodes"] == []
    assert layer["meta"]["groups"][0]["observation_count"] == 1
    assert all(g["revealed"] is False for g in layer["meta"]["groups"])


# ----------------------------------------------------------------------
# §2/§4 extra 节点：地点/共现人物同样是重建层产物，漏标会泄漏进人工层
# （2026-09 实测：PATCH 整文档回传时 split_persistent 只认
# props.generated_by，漏标的 place/共现主体被当人工提升落了库）
# ----------------------------------------------------------------------
def _companion_obs(oid="oc"):
    return [{
        "observation_id": oid,
        "skill_id": "geo_accompany",
        "origin": {"node_id": TARGET},
        "run_id": "r1",
        "detail": {"companion": {
            "person_a": "张某",
            "person_b": "李某",
            "locations": ["浙江省/杭州市/西湖区/古墩路"],
        }},
    }]


def test_extra_nodes_carry_lens_generated_marker():
    from server.app.canvas_case import GENERATED_BY_LENS
    r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                             lens_id="geo_accompany",
                             observations=_companion_obs(),
                             target_label="张某", conn=None)
    extras = r.get("extra_nodes") or []
    # 地点 1 + 共现人物（李某；张某=靶心本人按 target_label 跳过）
    assert len(extras) == 2
    for n in extras:
        assert isinstance(n.get("props"), dict)
        assert n["props"]["generated_by"] == GENERATED_BY_LENS
    for e in r.get("extra_edges") or []:
        assert e["generated_by"] == GENERATED_BY_LENS


def test_companion_subject_built_with_conn_threaded():
    """conn 必须透传给 build_subject_node：否则共现人物退化成名字哈希 id，
    与人工提升的语义主键节点不同键，merge 无法按 R-4 去重。"""
    sentinel = object()
    seen = {}
    real = G.build_subject_node  # 注意：不能加括号（存成 dict 会污染后续用例）

    def fake_build_subject_node(**kw):
        seen["conn"] = kw.get("conn")
        return {"id": case_node_id("subject", "C1", "person_li"),
                "kind": "subject", "props": {"name": "李某"}}

    G.build_subject_node = fake_build_subject_node
    try:
        r = G.build_result_layer(case_id="C1", target_node_id=TARGET,
                                 lens_id="geo_accompany",
                                 observations=_companion_obs(),
                                 target_label="张某", conn=sentinel)
    finally:
        G.build_subject_node = real
    assert seen.get("conn") is sentinel
    assert any(n["id"] == case_node_id("subject", "C1", "person_li")
               for n in r["extra_nodes"])


# ----------------------------------------------------------------------
# 裸函数用例收集：项目 runner 是 unittest（见 run_tests.py GROUPS），
# 默认 loader 只收 TestCase 类；通过 load_tests 钩子把本模块 test_*
# 函数包成 FunctionTestCase，避免这些用例在组跑时静默 0 收集。
# ----------------------------------------------------------------------
def load_tests(loader, standard_tests, pattern):
    import inspect
    import unittest
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn) \
                and fn.__module__ == __name__:
            standard_tests.addTest(unittest.FunctionTestCase(fn, description=name))
    return standard_tests
