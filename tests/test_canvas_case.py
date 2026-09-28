"""
tests/test_canvas_case.py
P1 案件级研判画布测试（PRD V1.0.0 功能 1 / REQ-P1-01~02）。

覆盖：
  域键与种子：case# 前缀识别、case_node_id 前缀、seed 空图、旧线索域不受影响；
  案件级形状校验：14 类 kind 合法、未知 kind 拒绝、线索域形状校验不含研判 kind；
  研判节点 props 校验：subject/place/event 全字段（红线 R2：重名主键留空）；
  研判边矩阵：位于/发生于/支撑/反驳/同现（同现同 kind 异节点、id 级自连硬拒）；
  API：GET 惰性 seed 幂等、POST/PATCH/DELETE 节点、研判边端点、
       analysis_result 人工添加拒绝、案件/线索域隔离、版本冲突 409。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app import canvas_case, canvas_edit, canvas_seed, canvas_writeback

NOW = "2026-09-27T10:00:00"
OP = "李侦查员"


# ======================================================================
# 纯函数：域键与种子
# ======================================================================
class CaseDomainTest(unittest.TestCase):
    def test_case_canvas_id_and_prefix(self):
        self.assertEqual("case#c1", canvas_case.case_canvas_id("c1"))
        self.assertTrue(canvas_case.is_case_canvas_id("case#c1"))
        # 旧线索快照（无前缀）不属于案件域
        self.assertFalse(canvas_case.is_case_canvas_id("clue-1"))
        self.assertFalse(canvas_case.is_case_canvas_id(None))

    def test_case_node_id_has_case_prefix(self):
        nid = canvas_case.case_node_id()
        self.assertTrue(nid.startswith("case#cn_"))
        self.assertNotEqual(nid, canvas_case.case_node_id())  # 随机不撞

    def test_seed_case_doc_is_blank(self):
        doc = canvas_case.seed_case_doc()
        self.assertEqual([], doc["nodes"])
        self.assertEqual([], doc["edges"])

    def test_case_shape_allows_research_kinds(self):
        doc = {
            "nodes": [
                {"id": "case#cn_a", "kind": "subject", "ref": "case#cn_a",
                 "label": "张卫国", "system": False, "pinned": False,
                 "x": 0, "y": 0, "props": {"person_name": "张卫国"}},
                {"id": "case#cn_b", "kind": "place", "ref": "case#cn_b",
                 "label": "莫干山路", "system": False, "pinned": False,
                 "x": 100, "y": 0,
                 "props": {"location_id": "loc-1"}},
                {"id": "case#cn_c", "kind": "analysis_result", "ref": "case#cn_c",
                 "label": "结论", "system": False, "pinned": False,
                 "x": 200, "y": 0, "props": {"lens_id": "geo_site"}},
            ],
            "edges": [],
        }
        self.assertEqual([], canvas_case.validate_case_doc_shape(doc))

    def test_case_shape_rejects_unknown_kind(self):
        doc = {"nodes": [{"id": "x", "kind": "widget", "ref": "x",
                          "label": "x", "system": False, "pinned": False,
                          "x": 0, "y": 0}], "edges": []}
        self.assertTrue(canvas_case.validate_case_doc_shape(doc))

    def test_clue_shape_still_rejects_research_kinds(self):
        """线索域形状校验零回归：研判 kind 在线索画布仍拒入（域隔离）。"""
        doc = {"nodes": [{"id": "x", "kind": "subject", "ref": "x",
                          "label": "x", "system": False, "pinned": False,
                          "x": 0, "y": 0}], "edges": []}
        self.assertTrue(canvas_seed.validate_doc_shape(doc))
        # 线索域人工节点类型校验同样不含研判 kind
        with self.assertRaises(canvas_edit.CanvasEditError):
            canvas_edit.validate_node_props("subject", {"person_name": "张"})


# ======================================================================
# 纯函数：研判节点 props 校验
# ======================================================================
class CaseNodePropsTest(unittest.TestCase):
    def test_subject_ok(self):
        p = canvas_case.validate_case_node_props(
            "subject", {"person_name": "  张卫国 "})
        self.assertEqual("张卫国", p["person_name"])
        self.assertNotIn("person_ambiguous", p)

    def test_subject_ambiguous_pk_must_be_empty(self):
        """红线 R2：重名待裁决时主键留空，系统不自裁。"""
        p = canvas_case.validate_case_node_props(
            "subject", {"person_name": "张卫国", "person_ambiguous": True})
        self.assertTrue(p["person_ambiguous"])
        self.assertNotIn("person_pk", p)
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "subject", {"person_name": "张卫国", "person_ambiguous": "true",
                            "person_pk": "p1"})

    def test_subject_normalizes_ambiguous_and_keeps_pk(self):
        p = canvas_case.validate_case_node_props(
            "subject", {"person_name": "张卫国", "person_ambiguous": "false",
                        "person_pk": "p1"})
        self.assertIs(False, p["person_ambiguous"])
        self.assertEqual("p1", p["person_pk"])

    def test_subject_name_required_and_limited(self):
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props("subject", {})
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "subject", {"person_name": "张" * 51})

    def test_place_location_or_coords(self):
        p = canvas_case.validate_case_node_props(
            "place", {"location_id": "loc-1"})
        self.assertEqual("loc-1", p["location_id"])
        p = canvas_case.validate_case_node_props(
            "place", {"lng": 120.1, "lat": 30.2,
                      "coord_precision": "门牌级", "std_address": "莫干山路 111 号"})
        self.assertEqual(120.1, p["lng"])
        self.assertEqual("门牌级", p["coord_precision"])
        # 二选一必填 / 成对 / 范围 / 枚举
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props("place", {})
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props("place", {"lng": 120.1})
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props("place", {"lng": 200, "lat": 1})
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "place", {"location_id": "x", "coord_precision": "街道"})

    def test_event_time_rules(self):
        p = canvas_case.validate_case_node_props(
            "event", {"title": "三次到访", "time_start": "2026-03-10 14:35"})
        self.assertEqual("date", p["time_precision"])  # 缺省降档
        p = canvas_case.validate_case_node_props(
            "event", {"title": "窗口", "time_start": "2026-03-10T14:35:00",
                      "time_end": "2026-03-10 16:40",
                      "time_precision": "minute"})
        self.assertEqual("minute", p["time_precision"])
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props("event", {"title": "缺时间"})
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "event", {"title": "倒挂", "time_start": "2026-03-11 10:00",
                          "time_end": "2026-03-10 10:00"})
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "event", {"title": "脏精度", "time_start": "2026-03-10",
                          "time_precision": "hour"})

    def test_hypothesis_delegates_to_clue_domain(self):
        p = canvas_case.validate_case_node_props(
            "hypothesis", {"title": "H6 私下接触", "content": "两人存在非公务接触"})
        self.assertEqual("H6 私下接触", p["title"])
        with self.assertRaises(canvas_edit.CanvasEditError):
            canvas_case.validate_case_node_props(
                "hypothesis", {"title": "", "content": "x"})

    def test_hypothesis_assumption_id_optional(self):
        """P3：假设编号可选（镜头回写挂「支撑」边的匹配键）。"""
        p = canvas_case.validate_case_node_props(
            "hypothesis", {"title": "私下接触", "content": "x",
                           "assumption_id": " H6 "})
        self.assertEqual("H6", p["assumption_id"])
        # 不传 / 空白 → 不带该字段
        for props in ({"title": "H", "content": "x"},
                      {"title": "H", "content": "x", "assumption_id": "  "}):
            p2 = canvas_case.validate_case_node_props("hypothesis", props)
            self.assertNotIn("assumption_id", p2)
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "hypothesis", {"title": "H", "content": "x",
                               "assumption_id": "H" * 17})

    def test_analysis_result_not_manually_addable(self):
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.validate_case_node_props(
                "analysis_result", {"lens_id": "geo_site"})


# ======================================================================
# 纯函数：研判边矩阵 + 受控变更
# ======================================================================
CASE_RELS = ["位于", "发生于", "支撑", "反驳", "同现"]


def case_node(node_id: str, kind: str) -> dict:
    return {"id": node_id, "kind": kind, "ref": node_id, "label": kind,
            "system": False, "pinned": False, "x": 0, "y": 0,
            "props": {"person_name": "x", "location_id": "x", "title": "x",
                      "content": "x", "lens_id": "x"}}


class ResearchEdgeMatrixTest(unittest.TestCase):
    def test_located_ok_and_direction(self):
        self.assertEqual((True, ""), canvas_edit.can_connect("subject", "place", "位于"))
        self.assertEqual((True, ""), canvas_edit.can_connect("analysis_result", "place", "位于"))
        ok, reason = canvas_edit.can_connect("fact", "place", "位于")
        self.assertFalse(ok)
        ok, _ = canvas_edit.can_connect("place", "subject", "位于")
        self.assertFalse(ok)

    def test_occurred(self):
        self.assertEqual((True, ""), canvas_edit.can_connect("subject", "event", "发生于"))
        ok, _ = canvas_edit.can_connect("place", "event", "发生于")
        self.assertFalse(ok)

    def test_support_refute_only_from_analysis_result(self):
        for rel in ("支撑", "反驳"):
            self.assertEqual((True, ""),
                             canvas_edit.can_connect("analysis_result", "hypothesis", rel))
            ok, _ = canvas_edit.can_connect("subject", "hypothesis", rel)
            self.assertFalse(ok)
            ok, _ = canvas_edit.can_connect("analysis_result", "place", rel)
            self.assertFalse(ok)

    def test_cooccur_same_kind_different_nodes(self):
        self.assertEqual((True, ""), canvas_edit.can_connect("subject", "subject", "同现"))
        ok, _ = canvas_edit.can_connect("place", "place", "同现")
        self.assertFalse(ok)

    def test_note_and_unknown_rels_rejected(self):
        ok, _ = canvas_edit.can_connect("note", "place", "位于")
        self.assertFalse(ok)
        ok, _ = canvas_edit.can_connect("subject", "place", "拜访")
        self.assertFalse(ok)

    def test_allowed_rels_enumeration(self):
        self.assertEqual(["位于"], canvas_edit.allowed_rels("subject", "place"))
        self.assertEqual(["同现"], canvas_edit.allowed_rels("subject", "subject"))
        self.assertEqual(["支撑", "反驳"],
                         canvas_edit.allowed_rels("analysis_result", "hypothesis"))

    def test_manual_edge_rejects_self_node(self):
        """id 级自连硬拒：同现放开同 kind 后，同节点自连仍必须拒绝。"""
        doc = {"nodes": [case_node("s1", "subject")], "edges": []}
        with self.assertRaises(canvas_edit.CanvasEditError):
            canvas_edit.add_manual_edge(doc, source="s1", target="s1",
                                        rel="同现", note="", operator=OP, now=NOW)


class CaseDocMutationTest(unittest.TestCase):
    def _doc(self) -> dict:
        return {"nodes": [], "edges": []}

    def test_add_update_delete_case_node(self):
        doc = self._doc()
        nid = canvas_case.case_node_id()
        node = canvas_case.add_case_node(
            doc, kind="subject", props={"person_name": "张卫国", "person_ambiguous": True},
            node_id=nid, x=10, y=20, operator=OP, now=NOW)
        self.assertEqual("张卫国", node["label"])
        self.assertEqual(nid, node["ref"])
        updated = canvas_case.update_case_node(
            doc, node_id=nid, props={"person_name": "李志强"}, now=NOW)
        self.assertEqual("李志强", updated["label"])
        removed = canvas_case.delete_case_node(doc, node_id=nid)
        self.assertEqual([], removed)
        self.assertEqual([], doc["nodes"])

    def test_update_rejects_type_change_via_dirty_props(self):
        doc = self._doc()
        nid = canvas_case.case_node_id()
        canvas_case.add_case_node(doc, kind="event",
                                  props={"title": "e", "time_start": "2026-03-10"},
                                  node_id=nid, x=0, y=0, operator=OP, now=NOW)
        with self.assertRaises(canvas_case.CanvasCaseError):
            canvas_case.update_case_node(doc, node_id=nid,
                                         props={"person_name": "借编辑改类型"}, now=NOW)

    def test_system_node_locked(self):
        doc = {"nodes": [{"id": "s", "kind": "subject", "ref": "s",
                          "label": "s", "system": True, "pinned": False,
                          "x": 0, "y": 0, "props": {}}], "edges": []}
        with self.assertRaises(canvas_edit.CanvasEditError):
            canvas_case.delete_case_node(doc, node_id="s")


# ======================================================================
# P3 回写：观察 → analysis_result 节点（挂靶心 + 挂假设）+ 幂等
# ======================================================================
def _obs(observation_id: str = "obs-1", skill_id: str = "geo_accompany",
         node_id: str = "case#cn_t", **kw):
    from core.observation import Observation
    return Observation(
        observation_id=observation_id, skill_id=skill_id,
        lens_name="时空伴随镜头", title="张卫国 与 李志强 时空伴随 3 次",
        subject="张卫国", basis="轨迹同现", falsification="坐标缺失",
        source="directed",
        origin={"surface": "case_canvas", "node_id": node_id},
        operator=OP, **kw)


def _canvas_doc() -> dict:
    doc: dict = {"nodes": [], "edges": []}
    canvas_case.add_case_node(doc, kind="subject",
                              props={"person_name": "张卫国"},
                              node_id="case#cn_t", x=0, y=0,
                              operator=OP, now=NOW)
    canvas_case.add_case_node(
        doc, kind="hypothesis",
        props={"title": "私下接触", "content": "两人存在非公务接触",
               "assumption_id": "H6"},
        node_id="case#cn_h", x=100, y=0, operator=OP, now=NOW)
    return doc


class CanvasWritebackTest(unittest.TestCase):
    def test_build_writeback_doc_focal_and_hypothesis(self):
        doc = _canvas_doc()
        s = canvas_writeback.build_writeback_doc(
            doc, [_obs()], assumption_of=lambda _s: "H6",
            operator="worker", now=NOW)
        self.assertEqual(1, s["nodes_added"])
        self.assertEqual(2, s["edges_added"])
        self.assertEqual(1, s["hypothesis_linked"])
        ar = next(n for n in doc["nodes"]
                  if n["kind"] == "analysis_result")
        self.assertEqual("case#ar:obs-1", ar["id"])  # 确定性派生 id
        self.assertFalse(ar["system"])  # 用户交互对象（可连线/钉住）
        self.assertEqual("geo_accompany", ar["props"]["lens_id"])
        self.assertEqual("obs-1", ar["props"]["observation_id"])
        self.assertEqual("张卫国", ar["props"]["subject"])
        # 涉及（系统边，不可人工删）→ 靶心；支撑（人工边，可删可补挂）→ 假设
        edges = {(e["source"], e["rel"], e["target"], e["system"])
                 for e in doc["edges"]}
        self.assertIn(("case#ar:obs-1", "涉及", "case#cn_t", True), edges)
        self.assertIn(("case#ar:obs-1", "支撑", "case#cn_h", False), edges)

    def test_writeback_idempotent(self):
        """worker 重试/同靶心重跑：节点确定性 id 撞键跳过，边不重复。"""
        doc = _canvas_doc()
        for _ in range(2):
            canvas_writeback.build_writeback_doc(
                doc, [_obs()], assumption_of=lambda _s: "H6",
                operator="worker", now=NOW)
        ars = [n for n in doc["nodes"] if n["kind"] == "analysis_result"]
        self.assertEqual(1, len(ars))
        self.assertEqual(2, len(doc["edges"]))

    def test_focal_missing_still_plots_without_edges(self):
        """靶心已被删：节点仍上图，不挂边也不报错（人工可再补连线）。"""
        doc = _canvas_doc()
        s = canvas_writeback.build_writeback_doc(
            doc, [_obs(node_id="case#cn_gone")],
            assumption_of=lambda _s: "", operator="worker", now=NOW)
        self.assertEqual(1, s["nodes_added"])
        self.assertEqual(0, s["edges_added"])

    def test_no_assumption_only_focal(self):
        """未声明 assumption（PRD 开放项③）：仅挂靶心，不替正兵归因。"""
        doc = _canvas_doc()
        s = canvas_writeback.build_writeback_doc(
            doc, [_obs()], assumption_of=lambda _s: "",
            operator="worker", now=NOW)
        self.assertEqual(1, s["edges_added"])
        self.assertEqual(0, s["hypothesis_linked"])
        rels = {e["rel"] for e in doc["edges"]}
        self.assertNotIn("支撑", rels)

    def test_degraded_reason_flows_into_props(self):
        doc = _canvas_doc()
        canvas_writeback.build_writeback_doc(
            doc, [_obs(degraded=True, degraded_reason="坐标全缺")],
            assumption_of=None, operator="worker", now=NOW)
        ar = next(n for n in doc["nodes"]
                  if n["kind"] == "analysis_result")
        self.assertTrue(ar["props"]["degraded"])
        self.assertEqual("坐标全缺", ar["props"]["degraded_reason"])

    def test_writeback_observations_persists_and_idempotent(self):
        """落库封装：惰性 seed + version 推进；同观察重跑无变更不推进。"""
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp())
        try:
            state = tmp / "state.sqlite"
            r1 = canvas_writeback.writeback_observations(
                "c1", state_path=state, observations=[_obs()],
                assumption_of=lambda _s: "H6", operator="worker", now=NOW)
            self.assertEqual(1, r1["nodes_added"])
            from server.app.store.state_store import StateStore
            store = StateStore("c1", state)
            try:
                row = store.get_canvas("case#c1")
                self.assertIsNotNone(row)
                self.assertIn("analysis_result",
                              [n["kind"] for n in row["doc"]["nodes"]])
                v1 = row["version"]
            finally:
                store.close()
            r2 = canvas_writeback.writeback_observations(
                "c1", state_path=state, observations=[_obs()],
                assumption_of=lambda _s: "H6", operator="worker", now=NOW)
            self.assertEqual(1, r2["skipped"])
            self.assertEqual(0, r2["nodes_added"])
            store = StateStore("c1", state)
            try:
                self.assertEqual(v1, store.get_canvas("case#c1")["version"])
            finally:
                store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_writeback_zero_observations_is_noop(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp())
        try:
            self.assertIsNone(canvas_writeback.writeback_observations(
                "c1", state_path=tmp / "untouched.sqlite", observations=[],
                assumption_of=None, operator="worker", now=NOW))
            self.assertFalse((tmp / "untouched.sqlite").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PacksCanvasEnabledTest(unittest.TestCase):
    """P3：pack.json canvas_enabled 白名单 → 案件画布工具箱 14 镜头。"""

    CANVAS_SKILLS = {
        # geo 7
        "geo_site_profile", "geo_serial_profile", "geo_accompany",
        "geo_buffer_scan", "geo_segment", "geo_anomaly", "geo_activity_range",
        # timeline 3
        "timeline_sequence", "timeline_rhythm", "timeline_cross_collision",
        # relation 3
        "relation_neighborhood", "relation_common_neighbors", "relation_paths",
        # vlm 1（draft：工具箱可见但置灰）
        "vlm_inspect",
    }

    def test_canvas_enabled_whitelist(self):
        from core.pack_loader import discover
        from core.registry import get_registry
        discover()
        got = {s.skill_id for s in get_registry().all_specs()
               if getattr(s, "pack_id", "") != "_builtin"
               and getattr(s, "canvas_enabled", False)}
        self.assertEqual(self.CANVAS_SKILLS, got)

    def test_assumption_hook_declared(self):
        """庙算假设挂钩随 SkillSpec 装载（geo_accompany → H6）。"""
        from core.pack_loader import discover
        from core.registry import get_registry
        discover()
        reg = get_registry()
        self.assertEqual("H6", reg.skill("geo_accompany").assumption)
        self.assertEqual("", reg.skill("relation_neighborhood").assumption)


# ======================================================================
# API：案件级画布端点（域隔离 / 惰性 seed / 幂等 / 版本冲突）
# ======================================================================
def _clue():
    from core.registry import LineageClue
    return LineageClue(
        clue_id="clue-1", skill_id="xu_shi", title="张卫国整数现金存入",
        detail={"rules": [{"rule_id": "R1", "依据": "整数现金存入",
                           "rule_text": "单笔现金存入金额为整数万元"}]},
        jian_types=["生间"],
        source_rows=[{"from_raw": "张卫国", "to_raw": "海州建材",
                      "amount": 50000.0}])


class CaseCanvasApiTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from server.app.cases import CaseService
        from server.app.clues_artifact import save_case_clues
        from server.app.deps import WebContext
        from server.app.main import create_app
        from server.app.meta.models import User
        from server.app.meta.repo_sqlite import SqliteMetaRepo
        from server.app.security import hash_password
        from server.app.store import StoreFactory

        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.client = TestClient(create_app(self.ctx))
        for operator, role, pw in (("王检察官", "human", "pw-pro"),
                                   ("李侦查员", "正兵", "pw-sol")):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role, clearance=4,
                                       tenant_id="t1"))
        self.auth = self._login("李侦查员", "pw-sol")
        self.client.post("/api/v1/cases", headers=self.auth,
                         json={"case_id": "c1", "name": "测试案"})
        self.repo.set_version("c1", 1, "test")
        save_case_clues(self.factory.case_dir("c1"), 1, [_clue()])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post(
            "/api/v1/auth/login",
            json={"operator": operator, "password": password})
        self.assertEqual(200, r.status_code, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _get(self):
        return self.client.get("/api/v1/cases/c1/canvas", headers=self.auth)

    def _post_node(self, kind: str, props: dict, version: int | None = None):
        return self.client.post(
            "/api/v1/cases/c1/canvas/nodes", headers=self.auth,
            json={"kind": kind, "props": props, "x": 10, "y": 10,
                  "version": version})

    def _post_edge(self, source: str, target: str, rel: str,
                   version: int | None = None):
        return self.client.post(
            "/api/v1/cases/c1/canvas/edges", headers=self.auth,
            json={"source": source, "target": target, "rel": rel,
                  "version": version})

    # ------------------------------------------------------------------
    def test_first_get_seeds_blank_case_canvas(self):
        r = self._get()
        self.assertEqual(200, r.status_code, r.text)
        d = r.json()["data"]
        self.assertTrue(d["seeded"])
        self.assertEqual("case", d["canvas_domain"])
        self.assertEqual("case#c1", d["canvas_id"])
        self.assertEqual([], d["doc"]["nodes"])
        # 幂等：二次 GET 不再 seed
        d2 = self._get().json()["data"]
        self.assertFalse(d2["seeded"])
        self.assertEqual(1, d2["version"])

    def test_subject_node_and_research_edge(self):
        v1 = self._get().json()["data"]["version"]
        r = self._post_node("subject", {"person_name": "张卫国",
                                        "person_ambiguous": True}, version=v1)
        self.assertEqual(200, r.status_code, r.text)
        node = r.json()["data"]["node"]
        self.assertTrue(node["id"].startswith("case#cn_"))
        self.assertNotIn("person_pk", node["props"])  # 红线 R2
        v2 = r.json()["data"]["version"]
        r = self._post_node("place", {"location_id": "loc-mgs",
                                      "std_address": "拱墅区莫干山路 111 号",
                                      "coord_precision": "门牌级"}, version=v2)
        self.assertEqual(200, r.status_code, r.text)
        place_id = r.json()["data"]["node"]["id"]
        v3 = r.json()["data"]["version"]
        r = self._post_edge(node["id"], place_id, "位于", version=v3)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("位于", r.json()["data"]["edge"]["rel"])
        # 重复边拒绝
        r = self._post_edge(node["id"], place_id, "位于")
        self.assertEqual(400, r.status_code)
        # 矩阵违规拒绝（place → subject 位于）
        r = self._post_edge(place_id, node["id"], "位于")
        self.assertEqual(400, r.status_code)

    def test_analysis_result_manual_add_rejected(self):
        self._get()  # 先惰性 seed（画布未初始化时端点 404 语义）
        r = self._post_node("analysis_result", {"lens_id": "geo_site"})
        self.assertEqual(400, r.status_code)

    def test_domain_isolation_clue_endpoint_rejects_research_kind(self):
        """域隔离：线索画布端点不认研判 kind（形状/类型校验双保险）。"""
        g = self.client.get("/api/v1/cases/c1/clues/clue-1/canvas",
                            headers=self.auth)
        self.assertEqual(200, g.status_code, g.text)  # 先 seed 线索画布
        r = self.client.post(
            "/api/v1/cases/c1/clues/clue-1/canvas/nodes", headers=self.auth,
            json={"kind": "subject", "props": {"person_name": "张卫国"},
                  "x": 0, "y": 0})
        self.assertEqual(400, r.status_code)

    def test_version_conflict_409(self):
        v1 = self._get().json()["data"]["version"]
        self._post_node("subject", {"person_name": "张卫国"}, version=v1)
        # 用旧基准版本再写 → 409
        r = self._post_node("place", {"location_id": "loc-1"}, version=v1)
        self.assertEqual(409, r.status_code)


if __name__ == "__main__":
    unittest.main()
