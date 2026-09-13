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
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app.canvas_seed import (
    fact_ref,
    seed_canvas,
    sys_node_id,
    validate_patch,
)
from server.app.evidence_builder import _make_source_ref


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


if __name__ == "__main__":
    unittest.main()
