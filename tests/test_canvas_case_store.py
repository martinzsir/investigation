"""案件级研判画布持久化：纯函数 + 存储层测试。

每条独立可测（不依赖前序用例副作用），配反向验证——变异体必须让断言
失败，否则断言是恒真的摆设。

反向验证清单（改代码后逐个确认断言会红）：
  1. split_persistent 改成按 system 剥离 → 提升的 subject 被误剥（用例 2 红）
  2. split_persistent 不剔悬空边        → 用例 4 红（且 G6 会白屏）
  3. merge_lens_layer 不继承坐标        → 用例 6 红（重扫冲掉正兵摆的图）
  4. merge_lens_layer 不覆盖同 id       → 用例 7 红（系统层僵死）
  5. update 不做版本校验                → 用例 12 红（静默覆盖他人改动）
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from server.app import canvas_case as cc
from server.app import canvas_case_doc as cdoc
from server.app.store.state_store import (
    CanvasNotFound,
    CanvasVersionConflict,
    StateStore,
)


def _lens_node(nid="case#C1:analysis_result:a", **kw):
    """镜头重建层节点（带标记）。"""
    n = {"id": nid, "kind": "analysis_result", "system": True,
         "label": "异常轨迹", "props": {"generated_by": cc.GENERATED_BY_LENS}}
    n.update(kw)
    return n


def _subject_node(nid="case#C1:subject:person_bbb"):
    """提升产生的主体节点：system=True 但**没有**镜头标记——必须保留。"""
    return {"id": nid, "kind": "subject", "system": True,
            "label": "张卫国", "props": {"person_pk": "person_bbb",
                                    "anchored": True}}


def _manual_hyp(nid="case#C1:hypothesis:H6"):
    return {"id": nid, "kind": "hypothesis", "system": False,
            "label": "H6 私下接触", "props": {}}


# ======================================================================
# 一、split_persistent：什么该落库
# ======================================================================
def test_lens_result_stripped():
    doc = {"nodes": [_lens_node()], "edges": []}
    out, st = cdoc.split_persistent(doc)
    assert out["nodes"] == []
    assert st["nodes"] == 1


def test_promoted_subject_kept():
    """R-1：提升的 subject 是 system=True，但**不能**按 system 剥离。

    它是人工动作的结果，档案里没有——剥了刷新就丢，正是要治的病。
    """
    doc = {"nodes": [_subject_node()], "edges": []}
    out, st = cdoc.split_persistent(doc)
    assert [n["id"] for n in out["nodes"]] == ["case#C1:subject:person_bbb"]
    assert st["nodes"] == 0


def test_manual_hypothesis_kept():
    out, st = cdoc.split_persistent({"nodes": [_manual_hyp()], "edges": []})
    assert len(out["nodes"]) == 1 and st["nodes"] == 0


def test_dangling_edges_removed():
    """R-2：引用已剥离节点的边必须剔除——G6 会因悬空引用整图白屏。"""
    doc = {"nodes": [_lens_node(), _subject_node()],
           "edges": [{"id": "e1", "source": "case#C1:subject:person_bbb",
                      "target": "case#C1:analysis_result:a"}]}
    out, st = cdoc.split_persistent(doc)
    assert out["edges"] == []
    assert st["edges"] == 1


def test_kept_edge_survives():
    doc = {"nodes": [_subject_node(), _manual_hyp()],
           "edges": [{"id": "e2", "source": "case#C1:subject:person_bbb",
                      "target": "case#C1:hypothesis:H6", "rel": "推断为"}]}
    out, st = cdoc.split_persistent(doc)
    assert len(out["edges"]) == 1 and st["edges"] == 0


# ======================================================================
# 二、merge_lens_layer：合并
# ======================================================================
def test_layout_preserved_on_remerge():
    """R-3：重扫不该把正兵摆好的坐标冲回默认位置。

    重建层节点**显式带空坐标**（x=None）——这才是真实形态：档案里没有
    坐标这一维，重建出来的节点坐标就是空。若只测"growth 不含 x 键"，
    update 不删键、坐标自然还在，断言会恒真，测不出 R-3 有没有实现。
    """
    persistent = {"nodes": [dict(_lens_node(), x=120, y=80, pinned=True)],
                  "edges": []}
    growth = {"nodes": [dict(_lens_node(), x=None, y=None)],
              "edges": [], "hypothesis_nodes": [], "infer_edges": []}
    out = cdoc.merge_lens_layer(persistent, growth)
    n = out["nodes"][0]
    assert (n["x"], n["y"], n["pinned"]) == (120, 80, True)


def test_growth_overwrites_same_id():
    """R-4：同 id 以重建层为准，否则系统层永远停在旧版本（僵死）。"""
    persistent = {"nodes": [dict(_lens_node(), label="旧标签")], "edges": []}
    growth = {"nodes": [_lens_node(nid="case#C1:analysis_result:a",
                                   label="新标签")],
              "edges": [], "hypothesis_nodes": [], "infer_edges": []}
    out = cdoc.merge_lens_layer(persistent, growth)
    assert len(out["nodes"]) == 1
    assert out["nodes"][0]["label"] == "新标签"


def test_manual_nodes_survive_merge():
    persistent = {"nodes": [_subject_node(), _manual_hyp()], "edges": []}
    growth = {"nodes": [_lens_node()], "edges": [],
              "hypothesis_nodes": [dict(_manual_hyp(), system=True)],
              "infer_edges": []}
    out = cdoc.merge_lens_layer(persistent, growth)
    ids = {n["id"] for n in out["nodes"]}
    assert "case#C1:subject:person_bbb" in ids
    assert "case#C1:hypothesis:H6" in ids
    assert "case#C1:analysis_result:a" in ids


def test_edges_dedup_by_id():
    e = {"id": "e1", "source": "a", "target": "b"}
    persistent = {"nodes": [{"id": "a"}, {"id": "b"}],
                  "edges": [dict(e, rel="旧")]}
    growth = {"nodes": [], "edges": [dict(e, rel="新")],
              "hypothesis_nodes": [], "infer_edges": []}
    out = cdoc.merge_lens_layer(persistent, growth)
    assert len(out["edges"]) == 1 and out["edges"][0]["rel"] == "新"


# ======================================================================
# 二·补：合并时悬空重建边剔除（靶心主体未提升的典型现场）
# ======================================================================
def test_merge_drops_dangling_growth_edge_with_count():
    """重建边端点不在合并后节点集合里 → 剔除并计数，不让 G6 白屏。"""
    growth = {
        "nodes": [_lens_node()],
        "edges": [{"id": "gx", "source": "case#C1:subject:missing",
                   "target": "case#C1:analysis_result:a"}],
        "hypothesis_nodes": [], "infer_edges": [],
    }
    sink: dict = {}
    out = cdoc.merge_lens_layer({"nodes": [], "edges": []}, growth,
                                stats_sink=sink)
    assert out["edges"] == []
    assert sink["edges_dropped"] == 1
    assert [n["id"] for n in out["nodes"]] == ["case#C1:analysis_result:a"]


def test_merge_kept_edge_between_existing_endpoints_not_counted():
    growth = {
        "nodes": [_lens_node(), _subject_node()],
        "edges": [{"id": "gx", "source": "case#C1:subject:person_bbb",
                   "target": "case#C1:analysis_result:a"}],
        "hypothesis_nodes": [], "infer_edges": [],
    }
    sink: dict = {}
    out = cdoc.merge_lens_layer({"nodes": [], "edges": []}, growth,
                                stats_sink=sink)
    assert len(out["edges"]) == 1
    assert sink["edges_dropped"] == 0


# ======================================================================
# 二·补：重建层坐标经 meta 收割/复活（保存重进、隐藏再显示不丢位）
# ======================================================================
def test_lens_layout_harvested_on_split():
    doc = {"nodes": [_lens_node(x=300, y=400, pinned=True)], "edges": []}
    out, st = cdoc.split_persistent(doc)
    assert st["layout_harvested"] == 1
    layout = out["meta"][cdoc.META_LENS_LAYOUT]
    assert layout["case#C1:analysis_result:a"] == \
        {"x": 300, "y": 400, "pinned": True}


def test_lens_layout_reapplied_from_meta_on_merge():
    """重建节点不在持久文档里：坐标靠 meta.lens_layout 复活。"""
    persistent = {"nodes": [], "edges": [],
                  "meta": {cdoc.META_LENS_LAYOUT: {
                      "case#C1:analysis_result:a": {"x": 321, "y": 654}}}}
    growth = {"nodes": [dict(_lens_node(), x=None, y=None)], "edges": [],
              "hypothesis_nodes": [], "infer_edges": []}
    out = cdoc.merge_lens_layer(persistent, growth)
    n = out["nodes"][0]
    assert (n["x"], n["y"]) == (321, 654)


def test_meta_layout_never_overrides_manual_node_coords():
    """同 id 人工节点自带坐标优先：meta 是给重建节点用的，不抢人工位。"""
    persistent = {
        "nodes": [{"id": "n1", "kind": "subject", "x": 9, "y": 9}],
        "edges": [],
        "meta": {cdoc.META_LENS_LAYOUT: {"n1": {"x": 1, "y": 1}}},
    }
    out = cdoc.merge_lens_layer(persistent,
                                {"nodes": [], "edges": []})
    assert (out["nodes"][0]["x"], out["nodes"][0]["y"]) == (9, 9)


def test_split_then_merge_roundtrip_keeps_lens_coords():
    """完整往返：摆位 → split 收割 → merge 重建 → 位置原样回来。"""
    doc = {"nodes": [_lens_node(x=77, y=88, pinned=True), _subject_node()],
           "edges": []}
    persistent, _ = cdoc.split_persistent(doc)
    growth = {"nodes": [dict(_lens_node())], "edges": [],
              "hypothesis_nodes": [], "infer_edges": []}
    out = cdoc.merge_lens_layer(persistent, growth)
    n = next(n for n in out["nodes"]
             if n["id"] == "case#C1:analysis_result:a")
    assert (n["x"], n["y"], n["pinned"]) == (77, 88, True)


# ======================================================================
# 二·补：渐进式揭示集 meta 读写（fail-closed：脏值降级空集）
# ======================================================================
def test_revealed_parse_defaults_empty():
    assert cdoc.parse_revealed_groups(None) == set()
    assert cdoc.parse_revealed_groups({}) == set()
    assert cdoc.parse_revealed_groups({"revealed_groups": "脏"}) == set()
    assert cdoc.parse_revealed_groups({"revealed_groups": []}) == set()


def test_revealed_parse_drops_invalid_rows():
    meta = {"revealed_groups": [
        {"target": "t2", "lens": "l2"},
        {"target": "t1", "lens": "l1"},
        {"target": "", "lens": "x"},     # 空 target 丢
        "脏",                             # 非 dict 丢
        {"lens": "y"},                    # 缺 target 丢
    ]}
    assert cdoc.parse_revealed_groups(meta) == {
        ("t1", "l1"), ("t2", "l2")}


def test_revealed_serialize_sorted_roundtrip():
    s = {("t2", "l2"), ("t1", "l1")}
    raw = cdoc.serialize_revealed_groups(s)
    assert raw == [{"target": "t1", "lens": "l1"},
                   {"target": "t2", "lens": "l2"}]
    assert cdoc.parse_revealed_groups({"revealed_groups": raw}) == s


# ======================================================================
# 三、生成器标记（剥离能生效的前提）
# ======================================================================
def test_result_node_marked():
    n = cc.build_result_node(case_id="C1", result_ref="x", label="L")
    assert n["props"]["generated_by"] == cc.GENERATED_BY_LENS


def test_hypothesis_node_marked():
    n = cc.build_hypothesis_node(case_id="C1", hypothesis_id="H6")
    assert n["props"]["generated_by"] == cc.GENERATED_BY_LENS


def test_infer_edges_marked():
    n = cc.build_result_node(case_id="C1", result_ref="x", label="L",
                             assumption="H6")
    edges = cc.build_infer_edges(case_id="C1", result_nodes=[n])
    assert edges and all(e.get("generated_by") == cc.GENERATED_BY_LENS
                         for e in edges)


# ======================================================================
# 四、存储层
# ======================================================================
class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = StateStore("C1", Path(self.tmp) / "state.sqlite")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_insert_idempotent(self):
        assert self.store.insert_case_canvas(
            case_id="C1", doc={"nodes": []}, created_by="u", created_at="t")
        assert self.store.insert_case_canvas(
            case_id="C1", doc={"nodes": []}, created_by="u",
            created_at="t") is None

    def test_get_missing_returns_none(self):
        assert self.store.get_case_canvas("C1") is None

    def test_update_bumps_version(self):
        self.store.insert_case_canvas(case_id="C1", doc={"nodes": []},
                                      created_by="u", created_at="t")
        row = self.store.update_case_canvas_doc(
            "C1", {"nodes": [{"id": "n1"}]}, operator="u", updated_at="t")
        assert row["version"] == 2
        assert row["doc"]["nodes"][0]["id"] == "n1"

    def test_version_conflict_reports_case_scope(self):
        """RC-205：服务端不静默覆盖他人版本；提示必须说清是哪张画布。"""
        self.store.insert_case_canvas(case_id="C1", doc={"nodes": []},
                                      created_by="u", created_at="t")
        with self.assertRaises(CanvasVersionConflict) as ei:
            self.store.update_case_canvas_doc(
                "C1", {"nodes": []}, operator="u", updated_at="t",
                expected_version=99)
        assert "case=C1" in str(ei.exception)

    def test_update_missing_raises_not_found(self):
        with self.assertRaises(CanvasNotFound):
            self.store.update_case_canvas_doc("C1", {"nodes": []},
                                              operator="u", updated_at="t")

    def test_roundtrip_persists_manual_only(self):
        """完整往返：整文档入 → 剥离落库 → 读回 → 合并后人工层仍在。"""
        doc = {"nodes": [_lens_node(), _subject_node(), _manual_hyp()],
               "edges": [{"id": "e2",
                          "source": "case#C1:subject:person_bbb",
                          "target": "case#C1:hypothesis:H6"}]}
        persistent, stripped = cdoc.split_persistent(doc)
        assert stripped["nodes"] == 1
        self.store.insert_case_canvas(case_id="C1", doc=persistent,
                                      created_by="u", created_at="t")
        row = self.store.get_case_canvas("C1")
        ids = {n["id"] for n in row["doc"]["nodes"]}
        assert "case#C1:analysis_result:a" not in ids   # 镜头层没进库
        assert "case#C1:subject:person_bbb" in ids
        assert len(row["doc"]["edges"]) == 1


# ----------------------------------------------------------------------
# 裸函数用例收集：项目 runner 是 unittest（见 run_tests.py GROUPS）。
# ----------------------------------------------------------------------
def load_tests(loader, standard_tests, pattern):
    import inspect
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn) \
                and fn.__module__ == __name__:
            standard_tests.addTest(
                unittest.FunctionTestCase(fn, description=name))
    return standard_tests
