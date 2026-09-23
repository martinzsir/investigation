"""
tests/test_canvas_seed.py
RC-101 画布种子成图纯函数测试（server/app/canvas_seed.py）。

AC-101 对应：
  1. 节点数量与 fixture 映射一致（test_seed_node_mapping）；
  2. seed 幂等：同输入两次输出完全一致（test_seed_idempotent）；
  3. 每个 fact 可达至少一个 source_row（test_fact_connects_to_row）；
  4. 无 source_rows 的线索 seed 不抛异常（test_seed_without_source_rows）；
  5. 行无 URI 按 local URI 口径生成、不抛异常
     （test_unregistered_source_fallback）。
另含：无规则历史线索占位节点、PATCH 白名单纯函数校验。
"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.canvas_seed import (
    OBS_ID_PREFIX,
    build_lens_layer,
    build_observation_layer,
    fact_ref,
    reconcile_canvas,
    seed_canvas,
    sys_node_id,
    validate_doc_shape,
    validate_patch,
)
from server.app import canvas_seed as canvas_seed_mod
from server.app.clues_artifact import save_directed_observations
from server.app.evidence_builder import _make_source_ref
from core.observation import Observation


def _detail(**over) -> dict:
    """assemble_detail 产物形状（仅 seed 消费字段）。"""
    base = {
        "clue_id": "clue_1",
        "basis": "整数现金存入",
        "detail": {
            "rules": [
                {"rule_id": "R1", "依据": "整数现金存入",
                 "rule_text": "单笔现金存入金额为整数万元"},
            ],
        },
        "source_rows": [
            {"from_raw": "张卫国", "to_raw": "海州建材", "amount": 50000.0},
            {"from_raw": "张卫国", "to_raw": "海州建材", "amount": 20000.0},
        ],
    }
    base.update(over)
    return base


def _evidence(n: int = 2) -> list[dict]:
    """与 evidence_builder.build_evidence fact 栏同形状；行 URI 直接用
    _make_source_ref 生成（生产中三栏事实卡与种子行节点同源同 URI）。"""
    rows = _detail()["source_rows"]
    out = []
    for i in range(n):
        ref = _make_source_ref(rows[i], i, "clue_1")
        out.append({
            "id": f"fclue_1-{i}",
            "kind": "fact",
            "text": f"事实 {i}",
            "source_rows": [ref],
        })
    return out


def _index(doc: dict) -> dict[str, list[dict]]:
    return {
        "rule": [n for n in doc["nodes"] if n["kind"] == "rule"],
        "fact": [n for n in doc["nodes"] if n["kind"] == "fact"],
        "source_row": [n for n in doc["nodes"]
                       if n["kind"] == "source_row"],
        "verify_item": [n for n in doc["nodes"]
                        if n["kind"] == "verify_item"],
        "evidence": [n for n in doc["nodes"] if n["kind"] == "evidence"],
    }


def _rels(doc: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in doc["edges"]:
        counts[e["rel"]] = counts.get(e["rel"], 0) + 1
    return counts


class CanvasSeedTest(unittest.TestCase):
    def test_seed_node_mapping(self):
        """AC-101-1：节点数量与 fixture 映射完全一致。"""
        doc = seed_canvas(
            clue_id="clue_1",
            detail=_detail(),
            evidence=_evidence(2),
            verify_items=[
                {"item_id": "vi_sug", "kind": "suggested",
                 "text": "调取监控", "status": "建议", "origin": "suggested",
                 "channel": "external", "ref_function": ""},
                {"item_id": "vi_man", "kind": "manual",
                 "text": "人工核查项", "status": "待核查", "origin": "manual",
                 "channel": "", "ref_function": ""},
            ],
            materials=[
                {"material_id": "ev_1", "item_id": "vi_man",
                 "orig_name": "缴款单.pdf", "material_type": "缴款单",
                 "uploaded_by": "李侦查员", "uploaded_at": "2026-09-13 10:00"},
            ],
        )
        idx = _index(doc)
        self.assertEqual(1, len(idx["rule"]))
        self.assertEqual(2, len(idx["fact"]))
        self.assertEqual(2, len(idx["source_row"]))
        # M4 RC-105：status=建议 的手册建议项不再随 seed 成节点（改为
        # 规则节点「生成手册核实建议」显式产生 pb: 虚节点）；仅 vi_man 成节点
        self.assertEqual(1, len(idx["verify_item"]))
        self.assertEqual(1, len(idx["evidence"]))

        # 系统节点 id 契约：<kind>:<ref>
        self.assertEqual(sys_node_id("rule", "R1"), idx["rule"][0]["id"])
        self.assertEqual(
            sys_node_id("fact", fact_ref("clue_1", 0)), idx["fact"][0]["id"])
        for n in doc["nodes"]:
            self.assertTrue(n["system"])
            self.assertFalse(n["pinned"])
            self.assertIsInstance(n["x"], (int, float))
            self.assertIsInstance(n["y"], (int, float))

        # 系统边：rule 命中×2、fact 来源行×2、挂接×1；建议未显式生成→无手册建议
        rels = _rels(doc)
        self.assertEqual(2, rels.get("命中"))
        self.assertEqual(2, rels.get("来源行"))
        self.assertNotIn("手册建议", rels)
        self.assertEqual(1, rels.get("挂接"))
        for e in doc["edges"]:
            self.assertTrue(e["system"])

        # 已采纳核查项节点：ref=item_id、adopted=True
        man = next(n for n in idx["verify_item"] if n["ref"] == "vi_man")
        self.assertTrue(man["adopted"])

    def test_seed_idempotent(self):
        """AC-101-2：同输入两次 seed 字节一致（GET 幂等的纯函数基础）。"""
        kw = dict(clue_id="clue_1", detail=_detail(), evidence=_evidence(2),
                  verify_items=[], materials=[])
        d1 = seed_canvas(**copy.deepcopy(kw))
        d2 = seed_canvas(**copy.deepcopy(kw))
        self.assertEqual(d1, d2)
        ids1 = [n["id"] for n in d1["nodes"]]
        self.assertEqual(len(ids1), len(set(ids1)))  # id 无重复

    def test_fact_connects_to_row(self):
        """AC-101-3：每个 fact 经系统边可达至少一个 source_row。"""
        doc = seed_canvas(
            clue_id="clue_1", detail=_detail(), evidence=_evidence(2),
            verify_items=None, materials=None)
        adj: dict[str, list[str]] = {}
        for e in doc["edges"]:
            adj.setdefault(e["source"], []).append(e["target"])
        facts = [n for n in doc["nodes"] if n["kind"] == "fact"]
        rows = {n["id"] for n in doc["nodes"]
                if n["kind"] == "source_row"}
        self.assertTrue(facts)
        for f in facts:
            reached = set(adj.get(f["id"], []))
            self.assertTrue(
                reached & rows,
                f"事实 {f['id']} 不可达任何数据行节点")

    def test_seed_without_source_rows(self):
        """AC-101-4：无 source_rows（无事实）不抛异常，仅规则节点。"""
        detail = _detail(source_rows=[])
        detail["detail"] = {"rule_id": "R9", "rule_text": "无行规则"}
        doc = seed_canvas(
            clue_id="clue_x", detail=detail,
            evidence=[{"id": "p1", "kind": "pending",
                       "text": "待核实", "source_rows": []}],
            verify_items=[], materials=[])
        idx = _index(doc)
        self.assertEqual(1, len(idx["rule"]))
        self.assertEqual(0, len(idx["fact"]))
        self.assertEqual(0, len(idx["source_row"]))
        self.assertEqual([], doc["edges"])

    def test_unregistered_source_fallback(self):
        """行无 URI：按 evidence_builder local URI 同口径生成，确定性不抛异常。

        纯数据 dict 行（ingest 未登记/历史产物）——行节点 ref 形如
        「银行流水@local#row/<md516>」，与三栏事实卡引用同 URI。
        """
        rows = [{"from_raw": "甲", "to_raw": "乙", "amount": 10000.0}]
        detail = _detail(source_rows=rows)
        ev = [{
            "id": "fclue_1-0", "kind": "fact", "text": "事实",
            # 与 _make_source_ref(sr, 0, clue_id) 同口径
            "source_rows": [_make_source_ref(rows[0], 0, "clue_1")],
        }]
        d1 = seed_canvas(clue_id="clue_1", detail=detail, evidence=ev,
                         verify_items=[], materials=[])
        d2 = seed_canvas(clue_id="clue_1", detail=copy.deepcopy(detail),
                         evidence=copy.deepcopy(ev),
                         verify_items=[], materials=[])
        row_nodes = [n for n in d1["nodes"] if n["kind"] == "source_row"]
        self.assertEqual(1, len(row_nodes))
        self.assertIn("@local#row/", row_nodes[0]["ref"])
        self.assertEqual(d1, d2)
        # 事实卡引用与行节点 URI 一致（边能挂上）
        self.assertTrue(
            any(e["rel"] == "来源行"
                and e["target"] == row_nodes[0]["id"]
                for e in d1["edges"]))

    def test_historical_clue_placeholder_rule(self):
        """seed 缺规则：占位「未关联规则（历史产物）」，不报错。"""
        detail = _detail()
        detail["detail"] = {}
        doc = seed_canvas(
            clue_id="clue_old", detail=detail, evidence=[],
            verify_items=[], materials=[])
        rules = [n for n in doc["nodes"] if n["kind"] == "rule"]
        self.assertEqual(1, len(rules))
        self.assertEqual("unlinked", rules[0]["ref"])
        self.assertIn("历史产物", rules[0]["label"])
        self.assertTrue(rules[0]["props"]["historical"])

    def test_validate_patch_whitelist(self):
        """M1 PATCH：仅 x/y/pinned 可变；ref/label/props/边集合篡改拒绝。"""
        doc = seed_canvas(
            clue_id="clue_1", detail=_detail(), evidence=_evidence(1),
            verify_items=[], materials=[])
        # 合法：仅坐标/钉住
        ok_doc = copy.deepcopy(doc)
        ok_doc["nodes"][0]["x"] = 999
        ok_doc["nodes"][0]["pinned"] = True
        self.assertEqual([], validate_patch(doc, ok_doc))
        # 非法：篡改系统节点 ref
        bad_ref = copy.deepcopy(doc)
        bad_ref["nodes"][0]["ref"] = "RX"
        self.assertTrue(validate_patch(doc, bad_ref))
        # 非法：改 label
        bad_label = copy.deepcopy(doc)
        bad_label["nodes"][0]["label"] = "被篡改"
        self.assertTrue(validate_patch(doc, bad_label))
        # 非法：删节点（同时剪除其关联边以通过形状校验，直抵白名单判据）
        del_node = copy.deepcopy(doc)
        removed = del_node["nodes"][0]
        del_node["nodes"] = del_node["nodes"][1:]
        del_node["edges"] = [
            e for e in del_node["edges"]
            if removed["id"] not in (e["source"], e["target"])]
        self.assertTrue(any("不可删除" in m for m in
                            validate_patch(doc, del_node)))
        add_node = copy.deepcopy(doc)
        add_node["nodes"].append({
            "id": "cn_1", "kind": "hypothesis", "ref": "cn_1",
            "label": "人工假设", "system": False, "pinned": False,
            "x": 0, "y": 0,
        })
        self.assertTrue(any("不允许新增" in m for m in
                            validate_patch(doc, add_node)))
        # 非法：动边
        bad_edge = copy.deepcopy(doc)
        bad_edge["edges"][0]["rel"] = "推断为"
        self.assertTrue(validate_patch(doc, bad_edge))


# ----------------------------------------------------------------------
# 定向镜头（timeline_*）线索成图
# ----------------------------------------------------------------------
def _lens_rhythm_item() -> dict:
    """timeline_rhythm 镜头线索（assemble_detail item 形状）。

    call_a 在两个簇中重复出现（验去重）；共 3 个唯一事件、2 个聚集簇、
    1 个主体；另带 aggregate 引用（必须被忽略）。
    """
    call_a = {"type": "通话", "src_object": "call", "event_pk": "call_a",
              "date": "2020-01-01", "role": "主叫", "brief": "主叫→李四"}
    return {
        "clue_id": "clue_lens1",
        "title": "张卫国 的事件节奏：2 个聚集簇、常态间隔中位数 4 天",
        "skill_id": "timeline_rhythm",
        "lens_run_id": "lensrun_test",
        "basis": "",
        "source_rows": [],
        "detail": {
            "function": "timeline_rhythm",
            "hypothesis": "事件成簇，待正兵核查",
            # 镜头级节奏指标（detail 顶层，需透传到簇节点供时间轴对照判读）
            "median_gap_days": 4,
            "burst_count": 2,
            "subject": {"type": "person", "pk": "person_1",
                        "name": "张卫国"},
            "bursts": [
                {"start": "2020-01-01", "end": "2020-01-03",
                 "event_count": 2, "types": ["call", "transaction"],
                 "chain": True, "span_days": 2, "max_gap_days": 2,
                 "events": [
                     call_a,
                     {"type": "资金", "src_object": "transaction",
                      "event_pk": "txn_a", "date": "2020-01-03",
                      "role": "", "brief": "转出 5 万元"},
                 ]},
                {"start": "2020-02-01", "end": "2020-02-01",
                 "event_count": 2, "types": ["call"],
                 "events": [
                     dict(call_a),
                     {"type": "通话", "src_object": "call",
                      "event_pk": "call_b", "date": "2020-02-01",
                      "role": "被叫", "brief": "王五→本人"},
                 ]},
            ],
        },
        "evidence_refs": [
            {"kind": "node", "ref": "obj_person#person_1",
             "key_column": "person_id"},
            {"kind": "node", "ref": "obj_call#call_a",
             "key_column": "call_id"},
            {"kind": "node", "ref": "obj_transaction#txn_a",
             "key_column": "txn_id"},
            {"kind": "node", "ref": "obj_call#call_b",
             "key_column": "call_id"},
            {"kind": "aggregate", "metric": "burst_count", "value": 2},
            {"kind": "time_window", "ref": "lnk_time_window#p1",
             "key_column": "project_id"},
        ],
    }


def _lens_collision_item() -> dict:
    return {
        "clue_id": "clue_lens2",
        "title": "张卫国 在城东管网公示日前后 7 天跨类型碰撞",
        "skill_id": "timeline_cross_collision",
        "lens_run_id": "lensrun_test2",
        "basis": "",
        "source_rows": [],
        "detail": {
            "function": "timeline_cross_collision",
            "hypothesis": "公示窗口跨类型集中出现",
            "collision_index": 1,
            "anchor_date": "2020-03-25",
            "window_days": 7,
            "project": {"pk": "proj_1", "name": "城东管网改造"},
            "events": [
                {"type": "资金", "src_object": "transaction",
                 "event_pk": "txn_b", "date": "2020-03-20",
                 "role": "", "brief": "现金存入 10 万"},
                {"type": "通话", "src_object": "call",
                 "event_pk": "call_c", "date": "2020-03-26",
                 "role": "主叫", "brief": "主叫→赵六"},
            ],
        },
        "evidence_refs": [
            {"kind": "node", "ref": "obj_bid_project#proj_1",
             "key_column": "project_id"},
            {"kind": "node", "ref": "obj_transaction#txn_b",
             "key_column": "txn_id"},
            {"kind": "node", "ref": "obj_call#call_c",
             "key_column": "call_id"},
        ],
    }


def _kind_index(doc: dict) -> dict[str, list[dict]]:
    return {k: [n for n in doc["nodes"] if n["kind"] == k]
            for k in ("rule", "object", "function_result")}


def _edge_set(doc: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["rel"], e["target"]) for e in doc["edges"]}


class LensSeedTest(unittest.TestCase):
    def test_rhythm_seed_mapping(self):
        """节奏线索：skill_id 规则 + 主体对象 + 去重事件 + 聚集簇区间。"""
        doc = seed_canvas(
            clue_id="clue_lens1", detail=_lens_rhythm_item(),
            evidence=[], verify_items=[], materials=[])
        self.assertEqual([], validate_doc_shape(doc))
        idx = _kind_index(doc)
        # 规则来自顶层 skill_id，不再落「未关联规则」占位
        self.assertEqual(1, len(idx["rule"]))
        rule = idx["rule"][0]
        self.assertEqual("timeline_rhythm", rule["ref"])
        self.assertEqual("规则 timeline_rhythm", rule["label"])
        self.assertNotIn("历史产物", rule["label"])
        # 主体 1 + 唯一事件 3（call_a 跨簇去重）
        persons = [n for n in idx["object"]
                   if (n["props"] or {}).get("lens_role") == "subject"]
        events = [n for n in idx["object"]
                  if (n["props"] or {}).get("lens_layer") == "event"]
        self.assertEqual(1, len(persons))
        self.assertEqual("张卫国", persons[0]["label"])
        self.assertEqual(3, len(events))
        # ref 与 M2 expand 同口径（type:pk，不是 evidence_refs 的 obj_#）
        self.assertEqual(
            sys_node_id("object", "call:call_a"),
            next(n["id"] for n in events if n["ref"] == "call:call_a"))
        # 事件节点带业务时间 → 时间轴视角可直接分档
        call_a = next(n for n in events if n["ref"] == "call:call_a")
        self.assertEqual("2020-01-01", call_a["props"]["event_time"])
        # 2 个聚集簇区间（event_time=start）
        self.assertEqual(2, len(idx["function_result"]))
        b1 = next(n for n in idx["function_result"]
                  if n["ref"] == "burst:clue_lens1:1")
        self.assertEqual("2020-01-01", b1["props"]["start"])
        self.assertEqual("2020-01-01", b1["props"]["event_time"])
        self.assertEqual("burst", b1["props"]["interval_kind"])
        self.assertEqual("timeline_rhythm", b1["props"]["function"])
        # 链式簇标记透传到区间节点 props（供时间轴区分带宽语义）
        self.assertTrue(b1["props"]["chain"])
        self.assertEqual(2, b1["props"]["span_days"])
        self.assertEqual(2, b1["props"]["max_gap_days"])
        # 镜头级节奏指标透传（时间轴泳道组「中位间隔 vs 成簇」对照用）
        self.assertEqual(4, b1["props"]["median_gap_days"])
        self.assertEqual(2, b1["props"]["burst_count"])
        # 第二簇无链式字段（模拟旧产物）→ 保守 False
        b2_legacy = next(n for n in idx["function_result"]
                         if n["ref"] == "burst:clue_lens1:2")
        self.assertFalse(b2_legacy["props"]["chain"])

        edges = _edge_set(doc)
        rid = rule["id"]
        # 规则 ──查询自──▶ 两簇；规则 ──涉及──▶ 主体（不直连事件）
        self.assertIn((rid, "查询自", b1["id"]), edges)
        self.assertIn((rid, "涉及", persons[0]["id"]), edges)
        self.assertNotIn((rid, "涉及", call_a["id"]), edges)
        # 簇 ──涉及──▶ 事件（call_a 被两个簇各连一次）
        b2 = next(n for n in idx["function_result"]
                  if n["ref"] == "burst:clue_lens1:2")
        self.assertIn((b1["id"], "涉及", call_a["id"]), edges)
        self.assertIn((b2["id"], "涉及", call_a["id"]), edges)
        txn = next(n["id"] for n in events if n["ref"] == "transaction:txn_a")
        call_b = next(n["id"] for n in events if n["ref"] == "call:call_b")
        self.assertIn((b1["id"], "涉及", txn), edges)
        self.assertIn((b2["id"], "涉及", call_b), edges)
        # aggregate/time_window 引用不成节点
        self.assertFalse(any("aggregate" in n["id"] for n in doc["nodes"]))
        # meta 规模声明
        self.assertTrue(doc["meta"]["lens_seeded"])
        self.assertEqual({"shown": 3, "total": 3},
                         doc["meta"]["lens"]["events"])
        self.assertEqual({"shown": 2, "total": 2},
                         doc["meta"]["lens"]["intervals"])

    def test_collision_window_dates_and_edges(self):
        """碰撞线索：碰撞窗区间 start/end=anchor±window，挂项目与事件。"""
        doc = seed_canvas(
            clue_id="clue_lens2", detail=_lens_collision_item(),
            evidence=[], verify_items=[], materials=[])
        self.assertEqual([], validate_doc_shape(doc))
        idx = _kind_index(doc)
        win = next(n for n in idx["function_result"]
                   if n["ref"] == "collision:clue_lens2:1")
        self.assertEqual("2020-03-18", win["props"]["start"])
        self.assertEqual("2020-04-01", win["props"]["end"])
        self.assertEqual("collision_window", win["props"]["interval_kind"])
        self.assertEqual(7, win["props"]["window_days"])
        # 碰撞镜头产物无节奏指标，不得透传簇字段（前端不显示该摘要）
        self.assertNotIn("median_gap_days", win["props"])
        self.assertNotIn("burst_count", win["props"])
        project = next(n for n in idx["object"]
                       if n["ref"] == "bid_project:proj_1")
        self.assertEqual("城东管网改造", project["label"])
        edges = _edge_set(doc)
        rid = idx["rule"][0]["id"]
        self.assertIn((rid, "查询自", win["id"]), edges)
        self.assertIn((rid, "涉及", project["id"]), edges)
        self.assertIn((win["id"], "涉及",
                       sys_node_id("object", "transaction:txn_b")), edges)
        self.assertIn((win["id"], "涉及",
                       sys_node_id("object", "call:call_c")), edges)

    def test_event_truncation_declared(self):
        """事件超 cap：按日期截断，只连已成图事件，meta 如实声明。"""
        with unittest.mock.patch.object(
                canvas_seed_mod, "_MAX_LENS_EVENT_NODES", 2):
            doc = seed_canvas(
                clue_id="clue_lens1", detail=_lens_rhythm_item(),
                evidence=[], verify_items=[], materials=[])
        events = [n for n in doc["nodes"]
                  if (n.get("props") or {}).get("lens_layer") == "event"]
        # date 升序前二：call_a(01-01)、txn_a(01-03)；call_b(02-01) 截断
        self.assertEqual(2, len(events))
        refs = {n["ref"] for n in events}
        self.assertEqual({"call:call_a", "transaction:txn_a"}, refs)
        self.assertEqual({"shown": 2, "total": 3},
                         doc["meta"]["lens"]["events"])
        targets = {e["target"] for e in doc["edges"]
                   if e["rel"] == "涉及" and e["source"].startswith(
                       "function_result:")}
        self.assertNotIn(sys_node_id("object", "call:call_b"), targets)

    def test_interval_truncation_reserves_collision_seat(self):
        """簇数超 cap：碰撞窗保留 1 席，簇只取前 cap-1；meta 如实声明。"""
        import copy
        item = copy.deepcopy(_lens_rhythm_item())
        # 构造 5 个簇 + 碰撞窗（同 detail 形状：anchor/window/events）
        item["detail"]["bursts"] = [
            {"start": f"2020-01-{i:02d}", "end": f"2020-01-{i:02d}",
             "event_count": 1, "types": ["call"],
             "events": [{"type": "通话", "src_object": "call",
                         "event_pk": f"call_{i}", "date": f"2020-01-{i:02d}",
                         "role": "主叫", "brief": "测试"}]}
            for i in range(1, 6)
        ]
        item["detail"]["anchor_date"] = "2020-03-25"
        item["detail"]["window_days"] = 7
        item["detail"]["collision_index"] = 1
        with unittest.mock.patch.object(
                canvas_seed_mod, "_MAX_LENS_INTERVAL_NODES", 4):
            layer = build_lens_layer("clue_lens1", item)
        refs = [n["ref"] for n in layer["nodes"]
                if n["kind"] == "function_result"]
        # cap=4：3 个簇（前 3）+ 碰撞窗垫尾；第 4/5 簇截断
        self.assertEqual(
            ["burst:clue_lens1:1", "burst:clue_lens1:2",
             "burst:clue_lens1:3", "collision:clue_lens1:1"], refs)
        self.assertEqual({"shown": 4, "total": 6},
                         layer["meta"]["intervals"])
        # 留下的簇边仍按位置正确配对（第 3 簇 ──涉及──▶ call_3），
        # 碰撞窗不被 zip 误当簇
        targets = {t for _s, t, rel in layer["edges"]
                   if rel == "涉及"}
        self.assertIn(sys_node_id("object", "call:call_3"), targets)
        # seed_canvas 口径一致（meta 透传）
        with unittest.mock.patch.object(
                canvas_seed_mod, "_MAX_LENS_INTERVAL_NODES", 4):
            doc = seed_canvas(
                clue_id="clue_lens1", detail=item, evidence=[],
                verify_items=[], materials=[])
        self.assertEqual({"shown": 4, "total": 6},
                         doc["meta"]["lens"]["intervals"])
        self.assertTrue(any(
            (n.get("props") or {}).get("interval_kind") == "collision_window"
            for n in doc["nodes"]))

    def test_rhythm_legacy_artifact_without_metric_fields(self):
        """旧产物缺 median_gap_days/burst_count：不报错，簇数按实际回退。"""
        import copy
        item = copy.deepcopy(_lens_rhythm_item())
        del item["detail"]["median_gap_days"]
        del item["detail"]["burst_count"]
        layer = build_lens_layer("clue_lens1", item)
        bursts = [n for n in layer["nodes"]
                  if (n.get("props") or {}).get("interval_kind") == "burst"]
        self.assertEqual(2, len(bursts))
        for n in bursts:
            self.assertNotIn("median_gap_days", n["props"])
            # 旧产物无 burst_count → 按 detail 内实际簇数 2 回退
            self.assertEqual(2, n["props"]["burst_count"])

    def test_reconcile_backfills_placeholder_canvas(self):
        """老画布（仅 unlinked 占位）reconcile：撤占位、补镜头节点与边，幂等。"""
        placeholder = {
            "id": sys_node_id("rule", "unlinked"), "kind": "rule",
            "ref": "unlinked", "label": "未关联规则（历史产物）",
            "system": True, "pinned": False, "x": 0, "y": 0,
            "props": {"historical": True},
        }
        old = {"nodes": [placeholder], "edges": [],
               "meta": {"truncated": {"source_row": {"shown": 0, "total": 0}}}}
        layer = build_lens_layer("clue_lens1", _lens_rhythm_item())
        merged, n_nodes, n_edges = reconcile_canvas(
            old, lens_layer=layer)
        self.assertGreater(n_nodes, 0)
        self.assertGreater(n_edges, 0)
        ids = {n["id"] for n in merged["nodes"]}
        self.assertNotIn(placeholder["id"], ids)
        self.assertIn(sys_node_id("rule", "timeline_rhythm"), ids)
        self.assertIn(sys_node_id("object", "call:call_a"), ids)
        self.assertIn(
            sys_node_id("function_result", "burst:clue_lens1:1"), ids)
        self.assertTrue(merged["meta"]["lens_seeded"])
        # 既有 meta 键保留
        self.assertIn("truncated", merged["meta"])
        # 新节点均有坐标（形状合法）
        self.assertEqual([], validate_doc_shape(merged))

        # 第二次：零新增、零边、meta 不变
        again, n2, e2 = reconcile_canvas(merged, lens_layer=layer)
        self.assertEqual(0, n2)
        self.assertEqual(0, e2)
        self.assertEqual(merged["nodes"], again["nodes"])
        self.assertEqual(merged["edges"], again["edges"])
        self.assertEqual(merged["meta"], again["meta"])

    def test_non_lens_clue_marker_only(self):
        """非镜头线索：seed/reconcile 不造镜头节点，仅落幂等标记。"""
        doc = seed_canvas(
            clue_id="clue_1", detail=_detail(), evidence=_evidence(1),
            verify_items=[], materials=[])
        self.assertTrue(doc["meta"]["lens_seeded"])
        self.assertNotIn("lens", doc["meta"])
        # 老画布 reconcile 空层：0 节点 0 边，仅 meta 打标
        old = {"nodes": [dict(n) for n in doc["nodes"]],
               "edges": [dict(e) for e in doc["edges"]], "meta": {}}
        merged, n_nodes, n_edges = reconcile_canvas(
            old, lens_layer=build_lens_layer("clue_1", _detail()))
        self.assertEqual(0, n_nodes)
        self.assertEqual(0, n_edges)
        self.assertTrue(merged["meta"]["lens_seeded"])


class ObservationLayerSeedTest(unittest.TestCase):
    """build_observation_layer：obs_* 中文命名 props 透传泳道头。"""

    def test_chinese_names_propagated_to_node_props(self):
        rhythm = _lens_rhythm_item()
        collision = _lens_collision_item()
        with tempfile.TemporaryDirectory() as case_dir:
            save_directed_observations(case_dir, [
                Observation(
                    observation_id="obs_rhythm_1",
                    skill_id="timeline_rhythm",
                    lens_name="周期节奏镜头",
                    title=rhythm["title"],
                    subject="张卫国",
                    detail=rhythm["detail"],
                    evidence_refs=rhythm["evidence_refs"],
                    source="directed",
                    origin={"clue_id": "clue_lens1"},
                ),
                Observation(
                    observation_id="obs_collision_1",
                    skill_id="timeline_cross_collision",
                    lens_name="跨类型时间碰撞镜头",
                    title=collision["title"],
                    project="城东管网改造",
                    detail=collision["detail"],
                    evidence_refs=collision["evidence_refs"],
                    source="directed",
                    origin={"clue_id": "clue_lens1"},
                ),
                # 他线线索发起的观察不得并入本层
                Observation(
                    observation_id="obs_other_clue",
                    skill_id="timeline_rhythm",
                    lens_name="周期节奏镜头",
                    subject="李四",
                    detail=rhythm["detail"],
                    origin={"clue_id": "clue_other"},
                ),
                # 旧档案：无 lens_name/title/subject → props 落空串，
                # 不崩且 skill_id 仍在（前端按回退链显示）
                Observation(
                    observation_id="obs_legacy",
                    skill_id="timeline_rhythm",
                    detail=rhythm["detail"],
                    evidence_refs=rhythm["evidence_refs"],
                    origin={"clue_id": "clue_lens1"},
                ),
            ])
            layer = build_observation_layer(case_dir, "clue_lens1")

            self.assertEqual(3, layer["meta"]["expanded"])
            self.assertTrue(layer["nodes"])
            # 节点 id 全部加观察层前缀
            self.assertTrue(all(n["id"].startswith(OBS_ID_PREFIX)
                                for n in layer["nodes"]))

            def props_of(oid: str) -> list[dict]:
                return [n["props"] for n in layer["nodes"]
                        if n["props"].get("obs_of") == oid]

            rhythm_props = props_of("obs_rhythm_1")
            collision_props = props_of("obs_collision_1")
            legacy_props = props_of("obs_legacy")
            self.assertTrue(rhythm_props and collision_props and legacy_props)
            self.assertEqual([], props_of("obs_other_clue"))

            p0 = rhythm_props[0]
            self.assertEqual("周期节奏镜头", p0["obs_lens_name"])
            self.assertEqual("张卫国", p0["obs_target"])
            self.assertEqual(rhythm["title"], p0["obs_title"])
            self.assertEqual("timeline_rhythm", p0["obs_skill_id"])
            self.assertIs(True, p0["obs_layer"])
            # 同组各节点命名 props 一致
            self.assertTrue(all(p["obs_lens_name"] == "周期节奏镜头"
                                and p["obs_target"] == "张卫国"
                                for p in rhythm_props))

            # 靶心：subject 缺省时取 project
            self.assertTrue(all(p["obs_target"] == "城东管网改造"
                                for p in collision_props))
            self.assertEqual("跨类型时间碰撞镜头",
                             collision_props[0]["obs_lens_name"])

            # 旧档案缺字段 → 空串而非 KeyError，skill_id 不受影响
            self.assertEqual("", legacy_props[0]["obs_lens_name"])
            self.assertEqual("", legacy_props[0]["obs_target"])
            self.assertEqual("", legacy_props[0]["obs_title"])
            self.assertEqual("timeline_rhythm",
                             legacy_props[0]["obs_skill_id"])


if __name__ == "__main__":
    unittest.main()
