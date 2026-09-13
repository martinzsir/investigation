"""
tests/test_canvas_infer.py
M4 画布推断纯函数测试（RC-105 手册建议节点生命周期 / RC-204 白名单查询）。

AC 对应：
  RC-105 add_suggestion_nodes：pb: 虚节点（adopted=false/虚线态）+「手册建议」边，
          幂等去重（同 playbook/同文本核查节点）；
  RC-105 plan_adoption：transition / add_manual / adopted 三态计划；
  RC-105 reconcile：pb→vi 迁移（含边端点重写/撞 id 去重）、vi 刷新、
          人工假设 created、pending 无假成功、missing/stale；
  RC-204 function_forms：白名单业务化目录（不暴露 sql/impl/impl_ref）；
  RC-204 merge_query_params：默认值合并/未声明参数拒绝/enum 拒绝/类型拒绝；
  RC-204 summarize_output / build_function_result：结果摘要（前 20 行预览）+
          function_result 节点 +「查询自」边（源缺失不建边）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app import canvas_infer as ci
from server.app.canvas_seed import sys_node_id


def _rule_node(rid: str = "R6") -> dict:
    return {"id": f"rule:{rid}", "kind": "rule", "ref": rid,
            "label": f"规则 {rid}", "system": True, "pinned": False}


def _doc_with_rule(extra_nodes: list[dict] | None = None) -> dict:
    return {"nodes": [_rule_node()] + (extra_nodes or []), "edges": []}


def _pb_item(pb_id: str = "r6_fund_tw_rerun", text: str = "复跑张卫国整数资金核查",
              **extra) -> dict:
    item = {"playbook_id": pb_id, "text": text, "kind": "suggested",
            "status": "建议", "origin": "suggested",
            "channel": "function", "ref_function": "time_window_collision",
            "falsification": "均为对公工程尾款"}
    item.update(extra)
    return item


class AddSuggestionNodesTest(unittest.TestCase):
    def test_adds_pb_nodes_and_edge(self):
        doc = _doc_with_rule()
        out = ci.add_suggestion_nodes(
            doc, rule_node_id="rule:R6",
            items=[_pb_item(), _pb_item("r6_bid_archive", "向住建局调取底档",
                                        channel="external",
                                        external={"target": "住建局招标办",
                                                  "material": "底档"},
                                        ref_function="")],
            now="2026-09-13T10:00:00")
        self.assertEqual(2, len(out["added_nodes"]))
        self.assertEqual(2, len(out["added_edges"]))
        n0 = out["added_nodes"][0]
        self.assertEqual(ci.suggestion_node_id("r6_fund_tw_rerun"), n0["id"])
        self.assertEqual("pb:r6_fund_tw_rerun", n0["ref"])
        self.assertEqual("verify_item", n0["kind"])
        self.assertTrue(n0["system"])
        self.assertFalse(n0["adopted"])
        self.assertEqual(ci.X_VERIFY_COL, n0["x"])
        self.assertEqual(0, n0["y"])
        self.assertEqual("建议", n0["props"]["status"])
        self.assertEqual("r6_fund_tw_rerun",
                         n0["props"]["playbook_id"])
        self.assertEqual("2026-09-13T10:00:00",
                         n0["props"]["generated_at"])
        # 同列纵向堆叠：第二个节点在下一槽
        self.assertEqual(ci.Y_GAP, out["added_nodes"][1]["y"])
        ext = out["added_nodes"][1]
        self.assertEqual({"target": "住建局招标办", "material": "底档"},
                         ext["props"]["external"])
        for e in out["added_edges"]:
            self.assertEqual("rule:R6", e["source"])
            self.assertEqual("手册建议", e["rel"])
            self.assertTrue(e["system"])
            self.assertEqual(f"e:rule:R6--手册建议--{e['target']}", e["id"])

    def test_idempotent_same_playbook_and_same_text(self):
        doc = _doc_with_rule()
        first = ci.add_suggestion_nodes(
            doc, rule_node_id="rule:R6", items=[_pb_item()], now="t")
        self.assertEqual(1, len(first["added_nodes"]))
        # 同 playbook 再来 → 跳过
        second = ci.add_suggestion_nodes(
            doc, rule_node_id="rule:R6",
            items=[_pb_item(text="复跑张卫国整数资金核查（改文案仍跳过）")],
            now="t")
        self.assertEqual([], second["added_nodes"])
        self.assertEqual("画布已存在该建议",
                         second["skipped"][0]["reason"])
        # 已采纳 vi 节点同文本 → 跳过（新 playbook）
        doc["nodes"].append({
            "id": "verify_item:vi_abc", "kind": "verify_item",
            "ref": "vi_abc", "label": "已采纳", "system": True,
            "pinned": False, "adopted": True,
            "props": {"text": "另一条建议文本"}})
        third = ci.add_suggestion_nodes(
            doc, rule_node_id="rule:R6",
            items=[_pb_item("pb_new", text="另一条建议文本")], now="t")
        self.assertEqual([], third["added_nodes"])
        self.assertEqual("画布已存在同文本核查节点",
                         third["skipped"][0]["reason"])

    def test_bad_items_skipped_or_raise(self):
        doc = _doc_with_rule()
        out = ci.add_suggestion_nodes(
            doc, rule_node_id="rule:R6",
            items=[{"playbook_id": "", "text": "x"},
                    {"playbook_id": "pb_x", "text": ""}],
            now="t")
        self.assertEqual([], out["added_nodes"])
        self.assertEqual(2, len(out["skipped"]))
        # 非规则节点/不存在节点 → 硬失败（路由转 400）
        with self.assertRaises(ci.CanvasInferError):
            ci.add_suggestion_nodes(
                {"nodes": [{"id": "fact:1", "kind": "fact"}],
                 "edges": []},
                rule_node_id="fact:1", items=[_pb_item()], now="t")
        with self.assertRaises(ci.CanvasInferError):
            ci.add_suggestion_nodes(
                _doc_with_rule(), rule_node_id="rule:RX",
                items=[_pb_item()], now="t")


class PlanAdoptionTest(unittest.TestCase):
    def _pb_node(self, **props_extra) -> dict:
        props = ci.suggestion_props(_pb_item(**props_extra))
        return {"id": ci.suggestion_node_id(props_extra.get(
                    "pb_id", "r6_fund_tw_rerun")),
                "kind": "verify_item",
                "ref": ci.playbook_ref(
                    props_extra.get("pb_id", "r6_fund_tw_rerun")),
                "label": "建议", "system": True, "pinned": False,
                "adopted": False, "props": props}

    def test_add_manual_when_state_missing(self):
        node = self._pb_node()
        doc = {"nodes": [node], "edges": []}
        plan = ci.plan_adoption(doc, node["id"], [])
        self.assertEqual("add_manual", plan["mode"])
        self.assertEqual("复跑张卫国整数资金核查", plan["text"])
        self.assertEqual("function", plan["channel"])
        self.assertEqual("time_window_collision", plan["ref_function"])
        self.assertEqual("均为对公工程尾款", plan["falsification"])
        self.assertNotIn("external", plan)
        # external 渠道项携带 external dict
        ext = self._pb_node(
            pb_id="r6_bid_archive", text="调取底档", channel="external",
            ref_function="", external={"target": "住建局", "material": "底档"})
        plan2 = ci.plan_adoption({"nodes": [ext], "edges": []},
                                  ext["id"], [])
        self.assertEqual({"target": "住建局", "material": "底档"},
                         plan2["external"])

    def test_transition_when_state_suggested(self):
        node = self._pb_node()
        state = [{"item_id": "vi_1", "text": node["props"]["text"],
                   "status": "建议"}]
        plan = ci.plan_adoption({"nodes": [node], "edges": []},
                                node["id"], state)
        self.assertEqual("transition", plan["mode"])
        self.assertEqual("vi_1", plan["item_id"])
        # 同文本已处理项 → adopted（无需任务）
        state[0]["status"] = "待核查"
        plan2 = ci.plan_adoption({"nodes": [node], "edges": []},
                                  node["id"], state)
        self.assertEqual("adopted", plan2["mode"])
        self.assertEqual("vi_1", plan2["item_id"])

    def test_vi_node_rules(self):
        vi = {"id": "verify_item:vi_9", "kind": "verify_item",
               "ref": "vi_9", "system": True, "pinned": False,
               "props": {}}
        doc = {"nodes": [vi], "edges": []}
        # 本线索建议项 → transition
        plan = ci.plan_adoption(
            doc, vi["id"],
            [{"item_id": "vi_9", "status": "建议", "text": "x"}])
        self.assertEqual("transition", plan["mode"])
        # state 无此项
        with self.assertRaises(ci.CanvasInferError):
            ci.plan_adoption(doc, vi["id"], [])
        # 非建议态
        with self.assertRaises(ci.CanvasInferError):
            ci.plan_adoption(
                doc, vi["id"],
                [{"item_id": "vi_9", "status": "待核查", "text": "x"}])

    def test_guard_errors(self):
        base = {"nodes": [self._pb_node()], "edges": []}
        with self.assertRaises(ci.CanvasInferError):
            ci.plan_adoption(base, "nope", [])
        fact = {"id": "fact:1", "kind": "fact"}
        with self.assertRaises(ci.CanvasInferError):
            ci.plan_adoption({"nodes": [fact]}, "fact:1", [])
        adopted = dict(base["nodes"][0], adopted=True)
        with self.assertRaises(ci.CanvasInferError):
            ci.plan_adoption({"nodes": [adopted]}, adopted["id"], [])
        stale = dict(base["nodes"][0], stale=True)
        with self.assertRaises(ci.CanvasInferError):
            ci.plan_adoption({"nodes": [stale]}, stale["id"], [])


class ReconcileTest(unittest.TestCase):
    def _pb_doc(self, *, pb_id="pb1", text="建议事项甲",
                 edge_rel="手册建议"):
        nid = ci.suggestion_node_id(pb_id)
        node = {"id": nid, "kind": "verify_item",
                "ref": ci.playbook_ref(pb_id), "label": text,
                "system": True, "pinned": False, "adopted": False,
                "props": {"text": text, "status": "建议",
                          "playbook_id": pb_id}}
        edge = {"id": f"e:rule:R6--{edge_rel}--{nid}",
                "source": "rule:R6", "target": nid, "rel": edge_rel,
                "system": True}
        return {"nodes": [_rule_node(), node], "edges": [edge]}, nid

    def test_pb_migrates_to_vi_and_rewrites_edges(self):
        doc, nid = self._pb_doc()
        # 再加一条边迁移后会撞 id（模拟同关系人工边已存在）→ 去重丢弃
        item_id = "vi_ab12"
        new_id = sys_node_id("verify_item", item_id)
        doc["edges"].append({"id": "e:manual", "source": "hyp:1",
                             "target": new_id, "rel": "手册建议",
                             "system": False})
        state = [{"item_id": item_id, "text": "建议事项甲",
                   "status": "待核查", "kind": "manual",
                   "origin": "manual", "channel": "external"}]
        out = ci.reconcile(doc, state_items=state,
                            targets=[{"node_id": nid}])
        self.assertTrue(out["changed"])
        r = out["results"][0]
        self.assertEqual("adopted", r["status"])
        self.assertEqual(item_id, r["item_id"])
        self.assertEqual(new_id, r["new_node_id"])
        ids = [n["id"] for n in doc["nodes"]]
        self.assertIn(new_id, ids)
        self.assertNotIn(nid, ids)
        migrated = next(n for n in doc["nodes"] if n["id"] == new_id)
        self.assertEqual(item_id, migrated["ref"])
        self.assertTrue(migrated["adopted"])
        self.assertEqual("待核查", migrated["props"]["status"])
        self.assertEqual("pb1", migrated["props"]["playbook_id"])
        # 旧 pb id 不出现在任何边；撞 id 边被去重
        for e in doc["edges"]:
            self.assertNotEqual(nid, e["source"])
            self.assertNotEqual(nid, e["target"])
        self.assertEqual(1, len([e for e in doc["edges"]
                                if e["rel"] == "手册建议"]))
        sys_edge = next(e for e in doc["edges"]
                        if e["rel"] == "手册建议" and e["system"])
        self.assertEqual("rule:R6", sys_edge["source"])
        self.assertEqual(new_id, sys_edge["target"])
        self.assertEqual(
            f"e:rule:R6--手册建议--{new_id}", sys_edge["id"])

    def test_pending_is_not_fake_success(self):
        doc, nid = self._pb_doc()
        # state 无项 / 仍是建议项 → pending，节点原样保留
        out = ci.reconcile(doc, state_items=[],
                            targets=[{"node_id": nid}])
        self.assertFalse(out["changed"])
        self.assertEqual("pending", out["results"][0]["status"])
        self.assertEqual(nid, doc["nodes"][-1]["id"])
        self.assertFalse(doc["nodes"][-1]["adopted"])
        out2 = ci.reconcile(
            doc, state_items=[{"item_id": "vi_x", "text": "建议事项甲",
                               "status": "建议"}],
            targets=[{"node_id": nid}])
        self.assertEqual("pending", out2["results"][0]["status"])

    def test_concurrent_vi_node_removes_pb(self):
        doc, nid = self._pb_doc()
        item_id = "vi_already"
        new_id = sys_node_id("verify_item", item_id)
        doc["nodes"].append({"id": new_id, "kind": "verify_item",
                            "ref": item_id, "label": "已在画布",
                            "system": True, "pinned": False, "adopted": True,
                            "props": {"text": "建议事项甲",
                                       "status": "待核查"}})
        out = ci.reconcile(
            doc, state_items=[{"item_id": item_id, "text": "建议事项甲",
                               "status": "待核查"}],
            targets=[{"node_id": nid}])
        self.assertEqual("adopted", out["results"][0]["status"])
        self.assertNotIn(nid, [n["id"] for n in doc["nodes"]])

    def test_vi_node_refresh_and_missing(self):
        vi = {"id": "verify_item:vi_1", "kind": "verify_item",
               "ref": "vi_1", "label": "旧", "system": True,
               "pinned": False, "adopted": False, "props": {}}
        doc = {"nodes": [vi], "edges": []}
        out = ci.reconcile(
            doc, state_items=[{"item_id": "vi_1", "text": "新文本",
                               "status": "待核查", "kind": "manual",
                               "origin": "manual"}],
            targets=[{"node_id": "vi_1"}])
        self.assertTrue(out["changed"])
        self.assertEqual("adopted", out["results"][0]["status"])
        self.assertTrue(vi["adopted"])
        self.assertEqual("新文本", vi["label"])
        # state 项消失 → stale + missing
        vi2 = {"id": "verify_item:vi_2", "kind": "verify_item",
                "ref": "vi_2", "system": True, "pinned": False,
                "props": {}}
        out2 = ci.reconcile({"nodes": [vi2], "edges": []},
                              state_items=[],
                              targets=[{"node_id": "vi_2"}])
        self.assertEqual("missing", out2["results"][0]["status"])
        self.assertTrue(vi2["stale"])

    def test_hypothesis_created_exists_pending(self):
        h = {"id": "cn_1", "kind": "hypothesis", "ref": "cn_1",
              "label": "假设", "system": False, "pinned": False,
              "props": {"title": "假设文本"}}
        doc = {"nodes": [h], "edges": []}
        # 缺 text 硬失败
        with self.assertRaises(ci.CanvasInferError):
            ci.reconcile(doc, state_items=[], targets=[{"node_id": "cn_1"}])
        # pending
        out = ci.reconcile(doc, state_items=[],
                            targets=[{"node_id": "cn_1",
                                       "text": "假设文本"}])
        self.assertEqual("pending", out["results"][0]["status"])
        self.assertFalse(out["changed"])
        # created
        state = [{"item_id": "vi_h1", "text": "假设文本",
                   "status": "待核查", "kind": "manual",
                   "origin": "manual"}]
        out2 = ci.reconcile(doc, state_items=state,
                             targets=[{"node_id": "cn_1",
                                        "text": "假设文本"}])
        r = out2["results"][0]
        self.assertEqual("created", r["status"])
        vn = next(n for n in doc["nodes"] if n["id"] == r["new_node_id"])
        self.assertEqual("verify_item", vn["kind"])
        self.assertEqual(ci.X_VERIFY_COL, vn["x"])
        self.assertTrue(vn["adopted"])
        # 再来一次：exists
        out3 = ci.reconcile(doc, state_items=state,
                              targets=[{"node_id": "cn_1",
                                        "text": "假设文本"}])
        self.assertEqual("exists", out3["results"][0]["status"])

    def test_unknown_node_missing(self):
        out = ci.reconcile({"nodes": [], "edges": []}, state_items=[],
                            targets=[{"node_id": "ghost"}])
        self.assertEqual("missing", out["results"][0]["status"])


# ----------------------------------------------------------------------
# RC-204：白名单 Function 查询纯函数
# ----------------------------------------------------------------------
def _fspec(name: str = "quarter_end_integer_deposits", **params):
    return NS(name=name, title="季度末整数存入聚合",
              description="季末窗口整数现金存入", output_type="rows",
              parameters=params or {
                  "round_unit": {"type": "integer", "default": 10000,
                                  "description": "整数金额单位"},
                  "cash_summary_tokens": {
                      "type": "string", "enum": ["现金存入"],
                      "default": "现金存入",
                      "description": "现金摘要标记"}})


class FunctionFormsTest(unittest.TestCase):
    def test_whitelist_filter_and_business_fields(self):
        spec = NS(functions={
            "quarter_end_integer_deposits": _fspec(),
            "jian_cross_level": NS(
                name="jian_cross_level", title="不应出现", description="",
                output_type="report",
                parameters={}, sql="SELECT 1", impl="sql",
                impl_ref="x"),
            "integer_transfer_aggregates": _fspec(
                "integer_transfer_aggregates",
                round_unit={"type": "integer", "default": 10000,
                             "description": "整数金额单位"}),
        })
        forms = ci.function_forms(spec.functions)
        names = [f["name"] for f in forms]
        self.assertEqual(["integer_transfer_aggregates",
                          "quarter_end_integer_deposits"], names)
        f = next(x for x in forms
                 if x["name"] == "quarter_end_integer_deposits")
        self.assertEqual("rows", f["output_type"])
        self.assertNotIn("sql", f)
        self.assertNotIn("impl", f)
        self.assertNotIn("impl_ref", f)
        pmap = {p["key"]: p for p in f["params"]}
        self.assertEqual(["现金存入"], pmap["cash_summary_tokens"]["enum"])
        self.assertTrue(pmap["cash_summary_tokens"]["required"])
        self.assertEqual("现金摘要标记",
                         pmap["cash_summary_tokens"]["label"])
        self.assertEqual("integer", pmap["round_unit"]["type"])

    def test_undeclared_whitelist_silently_skipped(self):
        self.assertEqual([], ci.function_forms({}))


class MergeQueryParamsTest(unittest.TestCase):
    def test_defaults_merge_and_override(self):
        spec = _fspec()
        merged = ci.merge_query_params(spec, {})
        self.assertEqual({"round_unit": 10000,
                          "cash_summary_tokens": "现金存入"}, merged)
        merged2 = ci.merge_query_params(spec, {"round_unit": 50000})
        self.assertEqual(50000, merged2["round_unit"])

    def test_unknown_enum_type_rejected(self):
        spec = _fspec()
        with self.assertRaises(ci.CanvasInferError):
            ci.merge_query_params(spec, {"bogus": 1})
        with self.assertRaises(ci.CanvasInferError):
            ci.merge_query_params(
                spec, {"cash_summary_tokens": "转账"})
        with self.assertRaises(ci.CanvasInferError):
            ci.merge_query_params(spec, {"round_unit": "1万"})
        with self.assertRaises(ci.CanvasInferError):
            ci.merge_query_params(spec, {"round_unit": True})

    def test_missing_required_rejected(self):
        spec = NS(name="x", parameters={
            "p": {"type": "integer", "description": "必填"}})
        with self.assertRaises(ci.CanvasInferError):
            ci.merge_query_params(spec, {})
        # string 无 enum 白名单 → 拒绝（自由文本防注入）
        spec2 = NS(name="y", parameters={
            "p": {"type": "string", "description": "自由文本"}})
        with self.assertRaises(ci.CanvasInferError):
            ci.merge_query_params(spec2, {"p": "任意值"})


class SummarizeOutputTest(unittest.TestCase):
    def test_rows_at_top_level(self):
        s = ci.summarize_output({"rows": [{"a": 1, "b": "x"}]})
        self.assertEqual("rows", s["kind"])
        self.assertEqual(1, s["row_count"])
        self.assertEqual(["a", "b"], s["columns"])
        self.assertEqual([{"a": 1, "b": "x"}], s["preview_rows"])

    def test_py_shape_with_meta_and_preview_cap(self):
        rows = [{"n": i} for i in range(25)]
        s = ci.summarize_output(
            {"result": {"rows": rows, "subject": "张卫国"}})
        self.assertEqual(25, s["row_count"])
        self.assertEqual(ci.PREVIEW_ROW_LIMIT, len(s["preview_rows"]))
        self.assertEqual({"subject": "张卫国"}, s["meta"])

    def test_report_and_empty(self):
        self.assertEqual("report",
                         ci.summarize_output({"result": {"hit": True}})[
                             "kind"])
        self.assertEqual("empty",
                         ci.summarize_output({"result": None})["kind"])


class BuildFunctionResultTest(unittest.TestCase):
    def _spec(self):
        return NS(name="integer_transfer_aggregates",
                   title="整数转账聚合（过桥结构）",
                   output_type="rows")

    def test_node_and_edge_with_source(self):
        doc = _doc_with_rule()
        out = {"rows": [{"from_raw": "甲", "to_raw": "乙",
                          "amt": Decimal("100000"),
                          "d": date(2026, 3, 31)}]}
        built = ci.build_function_result(
            doc, spec=self._spec(), out=out,
            params_used={"round_unit": 10000},
            source_node_id="rule:R6", operator="王检察官",
            now="2026-09-13T10:00:00", result_id="fr_abcd1234efef")
        node = built["node"]
        self.assertEqual("function_result:fr_abcd1234efef", node["id"])
        self.assertEqual("fr_abcd1234efef", node["ref"])
        self.assertEqual("function_result", node["kind"])
        self.assertTrue(node["system"])
        self.assertEqual(ci.X_RESULT_COL, node["x"])
        props = node["props"]
        self.assertEqual("integer_transfer_aggregates", props["function"])
        self.assertEqual({"round_unit": 10000}, props["params"])
        self.assertEqual("王检察官", props["executed_by"])
        self.assertEqual("2026-09-13T10:00:00", props["executed_at"])
        self.assertEqual("rows", props["kind"])
        self.assertEqual(1, props["row_count"])
        # Decimal/date 已收敛为 JSON 标量
        self.assertEqual("100000",
                         props["preview_rows"][0]["amt"])
        self.assertEqual("2026-03-31", props["preview_rows"][0]["d"])
        edge = built["edge"]
        self.assertIsNotNone(edge)
        self.assertEqual("rule:R6", edge["source"])
        self.assertEqual(node["id"], edge["target"])
        self.assertEqual("查询自", edge["rel"])
        self.assertTrue(edge["system"])

    def test_missing_source_no_edge(self):
        doc = _doc_with_rule()
        built = ci.build_function_result(
            doc, spec=self._spec(), out={"rows": []},
            params_used={}, source_node_id="rule:GHOST",
            operator="x", now="t", result_id="fr_1")
        self.assertIsNone(built["edge"])
        self.assertIsNone(ci.build_function_result(
            {"nodes": [], "edges": []}, spec=self._spec(),
            out={"rows": []}, params_used={}, source_node_id=None,
            operator="x", now="t", result_id="fr_2")["edge"])


if __name__ == "__main__":
    unittest.main()
