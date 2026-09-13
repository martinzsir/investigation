"""
tests/test_canvas_edit.py
M3 画布人工编辑纯函数测试（RC-202/203/206）。

覆盖：
  RC-203 can_connect 矩阵全量穷举（10 kind × 10 kind × 4 rel = 400 组合，
        与独立构造的真相表逐一对拍，固定唯一真相）；
  RC-202 人工节点字段校验/系统节点锁定/级联仅删人工边；
  RC-203 自连/重复边/系统边锁定；
  RC-206 mark_stale_refs 失效引用标记；快照备注校验。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from server.app import canvas_edit as ce

ALL_KINDS = [
    "rule", "fact", "object", "source_row", "source_file",
    "verify_item", "evidence", "hypothesis", "note", "function_result",
]
RELS = ["推断为", "证实", "查否", "补充说明"]

NOW = "2026-09-13T10:00:00"
OP = "李侦查员"


def expected_connect(src: str, tgt: str, rel: str) -> bool:
    """独立构造的矩阵真相（PRD RC-203 合法性矩阵表，不引用实现代码）。"""
    if rel not in RELS:
        return False
    if tgt == "note":  # 任意节点 → note 不允许（备注方向固定为源）
        return False
    if src == tgt:  # 自连
        return False
    if tgt in ("source_row", "source_file"):  # 禁入目标
        return False
    if rel == "补充说明":
        return src == "note"
    if tgt != "hypothesis":
        return False
    if rel == "推断为":
        return src in ("fact", "object")
    return src in ("verify_item", "evidence")  # 证实/查否


def sys_node(kind: str, ref: str | None = None) -> dict:
    ref = ref if ref is not None else f"{kind}_1"
    return {"id": f"{kind}:{ref}", "kind": kind, "ref": ref,
            "label": kind, "system": True, "pinned": False, "x": 0, "y": 0}


class ConnectMatrixTest(unittest.TestCase):
    """AC-203-1：kind×kind×rel 全量穷举，输出与真相表一致。"""

    def test_matrix_exhaustive(self):
        allowed = []
        for src in ALL_KINDS:
            for tgt in ALL_KINDS:
                for rel in RELS:
                    ok, reason = ce.can_connect(src, tgt, rel)
                    exp = expected_connect(src, tgt, rel)
                    self.assertEqual(
                        ok, exp,
                        msg=f"can_connect({src},{tgt},{rel})="
                            f"{ok} 与真相表 {exp} 不符")
                    if not ok:
                        self.assertTrue(reason, "拒绝必须给业务原因")
                    if ok:
                        allowed.append((src, tgt, rel))
        # 正向白名单计数固定：推断为 2 + 证实/查否各 2 + 补充说明
        # （note 源 × 8 个允许目标：排除 note 自身/2 个禁入目标 =
        #   10 - 1(note) - 2(row/file) = 7）
        self.assertEqual(2, sum(1 for t in allowed if t[2] == "推断为"))
        self.assertEqual(2, sum(1 for t in allowed if t[2] == "证实"))
        self.assertEqual(2, sum(1 for t in allowed if t[2] == "查否"))
        self.assertEqual(7, sum(1 for t in allowed if t[2] == "补充说明"))

    def test_allowed_rels_spot_checks(self):
        self.assertEqual(["推断为"],
                         ce.allowed_rels("fact", "hypothesis"))
        self.assertEqual(["证实", "查否"],
                         ce.allowed_rels("verify_item", "hypothesis"))
        self.assertEqual(["证实", "查否"],
                         ce.allowed_rels("evidence", "hypothesis"))
        self.assertEqual(["补充说明"], ce.allowed_rels("note", "fact"))
        self.assertEqual([], ce.allowed_rels("note", "note"))
        self.assertEqual([], ce.allowed_rels("fact", "source_row"))

    def test_bad_rel(self):
        ok, reason = ce.can_connect("fact", "hypothesis", "命中")
        self.assertFalse(ok)
        self.assertIn("非法", reason)


class NodeValidationTest(unittest.TestCase):
    def test_hypothesis_ok(self):
        props = ce.validate_node_props("hypothesis", {
            "title": "  存在体外循环  ", "content": "多账户过渡"})
        self.assertEqual("存在体外循环", props["title"])
        self.assertEqual("多账户过渡", props["content"])

    def test_note_ok(self):
        props = ce.validate_node_props("note", {"content": " 备注一句 "})
        self.assertNotIn("title", props)
        self.assertEqual("备注一句", props["content"])

    def test_blank_title(self):
        with self.assertRaises(ce.CanvasEditError) as ctx:
            ce.validate_node_props("hypothesis",
                                   {"title": "   ", "content": "内容"})
        self.assertEqual("请填写假设标题", str(ctx.exception))

    def test_title_too_long(self):
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_node_props("hypothesis",
                                   {"title": "题" * 51, "content": "内容"})

    def test_title_boundary_50_ok(self):
        props = ce.validate_node_props(
            "hypothesis", {"title": "题" * 50, "content": "内容"})
        self.assertEqual(50, len(props["title"]))

    def test_blank_and_long_content(self):
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_node_props("hypothesis",
                                   {"title": "标题", "content": " "})
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_node_props("note", {"content": "字" * 501})

    def test_bad_kind_and_type(self):
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_node_props("fact", {"content": "x"})
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_node_props("hypothesis", "不是对象")
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_node_props("hypothesis",
                                   {"title": 1, "content": "x"})


class EdgeNoteAndSnapshotTest(unittest.TestCase):
    def test_edge_note(self):
        self.assertEqual("", ce.validate_edge_note(None))
        self.assertEqual("", ce.validate_edge_note("   "))
        self.assertEqual("备", ce.validate_edge_note(" 备 "))
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_edge_note("x" * 201)
        with self.assertRaises(ce.CanvasEditError):
            ce.validate_edge_note(123)

    def test_snapshot_label(self):
        self.assertEqual("阶段一", ce.validate_snapshot_label(" 阶段一 "))
        for bad in ("", "   ", "x" * 101, 123, None):
            with self.assertRaises(ce.CanvasEditError):
                ce.validate_snapshot_label(bad)
        self.assertEqual(100, len(ce.validate_snapshot_label("x" * 100)))


class NodeDocMutationTest(unittest.TestCase):
    def _base_doc(self) -> dict:
        return {"nodes": [
            sys_node("rule", "R1"), sys_node("fact", "f1"),
            sys_node("object", "p1"), sys_node("source_row", "r1"),
            sys_node("source_file", "up1"), sys_node("verify_item", "vi1"),
            sys_node("evidence", "m1"),
        ], "edges": [{
            "id": "e:rule:R1--命中--fact:f1", "source": "rule:R1",
            "target": "fact:f1", "rel": "命中", "system": True,
        }]}

    def test_add_node_fields_and_collision(self):
        doc = self._base_doc()
        node = ce.add_manual_node(
            doc, kind="hypothesis",
            props={"title": "假设标题", "content": "假设内容"},
            node_id="cn_1", x=10, y=20, operator=OP, now=NOW)
        self.assertEqual("cn_1", node["id"])
        self.assertEqual("cn_1", node["ref"])
        self.assertFalse(node["system"])
        self.assertEqual("假设标题", node["label"])
        self.assertEqual(OP, node["created_by"])
        with self.assertRaises(ce.CanvasEditError):
            ce.add_manual_node(
                doc, kind="note", props={"content": "n"},
                node_id="cn_1", x=0, y=0, operator=OP, now=NOW)

    def test_add_node_rejects_bool_coordinate(self):
        doc = self._base_doc()
        with self.assertRaises(ce.CanvasEditError):
            ce.add_manual_node(
                doc, kind="note", props={"content": "n"},
                node_id="cn_2", x=True, y=0, operator=OP, now=NOW)

    def test_update_manual_node_relabels(self):
        doc = self._base_doc()
        ce.add_manual_node(
            doc, kind="hypothesis",
            props={"title": "旧标题", "content": "旧内容"},
            node_id="cn_1", x=0, y=0, operator=OP, now=NOW)
        node = ce.update_manual_node(
            doc, node_id="cn_1",
            props={"title": "新标题", "content": "新内容"}, now="T2")
        self.assertEqual("新标题", node["label"])
        self.assertEqual("T2", node["updated_at"])
        self.assertEqual(NOW, node["created_at"])

    def test_system_node_locked(self):
        doc = self._base_doc()
        with self.assertRaises(ce.CanvasEditError):
            ce.update_manual_node(
                doc, node_id="fact:f1",
                props={"content": "x"}, now=NOW)
        with self.assertRaises(ce.CanvasEditError):
            ce.delete_manual_node(doc, node_id="fact:f1")
        with self.assertRaises(ce.CanvasEditError):
            ce.delete_manual_node(doc, node_id="ghost")

    def test_delete_cascades_manual_edges_only(self):
        doc = self._base_doc()
        ce.add_manual_node(
            doc, kind="hypothesis",
            props={"title": "H", "content": "c"},
            node_id="cn_h", x=0, y=0, operator=OP, now=NOW)
        ce.add_manual_node(
            doc, kind="note", props={"content": "n"},
            node_id="cn_n", x=0, y=0, operator=OP, now=NOW)
        # 两条人工边 + 一条系统边
        ce.add_manual_edge(
            doc, source="fact:f1", target="cn_h", rel="推断为",
            note="", operator=OP, now=NOW)
        ce.add_manual_edge(
            doc, source="cn_n", target="fact:f1", rel="补充说明",
            note="", operator=OP, now=NOW)
        before_sys = [e["id"] for e in doc["edges"] if e["system"]]

        removed = ce.delete_manual_node(doc, node_id="cn_h")
        self.assertEqual(["e:fact:f1--推断为--cn_h"], removed)
        ids = {e["id"] for e in doc["edges"]}
        self.assertNotIn("e:fact:f1--推断为--cn_h", ids)
        self.assertIn("e:cn_n--补充说明--fact:f1", ids)  # 无关人工边保留
        self.assertEqual(before_sys,
                         [e["id"] for e in doc["edges"] if e["system"]])
        # 节点也删除
        self.assertNotIn("cn_h", {n["id"] for n in doc["nodes"]})


class EdgeMatrixMutationTest(unittest.TestCase):
    def _doc(self) -> dict:
        doc = {"nodes": [], "edges": []}
        for k in ("fact:f1", "object:p1", "verify_item:vi1", "evidence:m1",
                  "source_row:r1", "source_file:up1", "rule:R1",
                  "function_result:q1"):
            kind, ref = k.split(":", 1)
            doc["nodes"].append(sys_node(kind, ref))
        ce.add_manual_node(
            doc, kind="hypothesis",
            props={"title": "H", "content": "c"},
            node_id="cn_h", x=0, y=0, operator=OP, now=NOW)
        ce.add_manual_node(
            doc, kind="note", props={"content": "n"},
            node_id="cn_note", x=0, y=0, operator=OP, now=NOW)
        return doc

    def test_happy_paths(self):
        for src, rel in (("fact:f1", "推断为"), ("object:p1", "推断为"),
                         ("verify_item:vi1", "证实"),
                         ("evidence:m1", "查否")):
            doc = self._doc()
            edge = ce.add_manual_edge(
                doc, source=src, target="cn_h", rel=rel,
                note="", operator=OP, now=NOW)
            self.assertTrue(edge["id"].endswith(f"--{rel}--cn_h"))
            self.assertFalse(edge["system"])
        doc = self._doc()
        edge = ce.add_manual_edge(
            doc, source="cn_note", target="fact:f1", rel="补充说明",
            note="见备注", operator=OP, now=NOW)
        self.assertEqual("见备注", edge["note"])

    def test_illegal_and_duplicate(self):
        doc = self._doc()
        ce.add_manual_edge(
            doc, source="fact:f1", target="cn_h", rel="推断为",
            note="", operator=OP, now=NOW)
        # 重复边（同两端同关系）
        with self.assertRaisesRegex(ce.CanvasEditError, "已存在"):
            ce.add_manual_edge(
                doc, source="fact:f1", target="cn_h", rel="推断为",
                note="又一条", operator=OP, now=NOW)
        # 错误关系（fact 不可 证实 hypothesis）
        with self.assertRaisesRegex(ce.CanvasEditError, "不能建立"):
            ce.add_manual_edge(
                doc, source="fact:f1", target="cn_h", rel="证实",
                note="", operator=OP, now=NOW)
        # 任意 → note
        with self.assertRaisesRegex(ce.CanvasEditError, "起点"):
            ce.add_manual_edge(
                doc, source="fact:f1", target="cn_note", rel="补充说明",
                note="", operator=OP, now=NOW)
        # 禁入目标（数据行）
        with self.assertRaisesRegex(ce.CanvasEditError, "系统溯源连线"):
            ce.add_manual_edge(
                doc, source="cn_note", target="source_row:r1",
                rel="补充说明", note="", operator=OP, now=NOW)
        # 自连
        with self.assertRaisesRegex(ce.CanvasEditError, "自身"):
            ce.add_manual_edge(
                doc, source="cn_h", target="cn_h", rel="推断为",
                note="", operator=OP, now=NOW)
        # 端点不存在
        with self.assertRaisesRegex(ce.CanvasEditError, "端点不存在"):
            ce.add_manual_edge(
                doc, source="fact:ghost", target="cn_h", rel="推断为",
                note="", operator=OP, now=NOW)

    def test_delete_manual_and_system_locked(self):
        doc = self._doc()
        edge = ce.add_manual_edge(
            doc, source="fact:f1", target="cn_h", rel="推断为",
            note="", operator=OP, now=NOW)
        deleted = ce.delete_manual_edge(doc, edge_id=edge["id"])
        self.assertEqual(edge["id"], deleted["id"])
        # 系统边删除被拒
        doc["edges"].append({
            "id": "e:rule:R1--命中--fact:f1", "source": "rule:R1",
            "target": "fact:f1", "rel": "命中", "system": True})
        with self.assertRaisesRegex(ce.CanvasEditError, "系统推断关系"):
            ce.delete_manual_edge(
                doc, edge_id="e:rule:R1--命中--fact:f1")
        with self.assertRaisesRegex(ce.CanvasEditError, "不存在"):
            ce.delete_manual_edge(doc, edge_id="e:ghost")

    def test_incident_count(self):
        doc = self._doc()
        ce.add_manual_edge(
            doc, source="fact:f1", target="cn_h", rel="推断为",
            note="", operator=OP, now=NOW)
        ce.add_manual_edge(
            doc, source="cn_note", target="fact:f1", rel="补充说明",
            note="", operator=OP, now=NOW)
        self.assertEqual(
            2, len(ce.incident_manual_edge_ids(doc, "fact:f1")))
        self.assertEqual(
            1, len(ce.incident_manual_edge_ids(doc, "cn_h")))


class MarkStaleRefsTest(unittest.TestCase):
    def test_stale_marking(self):
        doc = {"nodes": [
            sys_node("verify_item", "vi_live"),
            sys_node("verify_item", "vi_gone"),
            sys_node("evidence", "m_live"),
            sys_node("evidence", "m_gone"),
            sys_node("fact", "f1"),
        ], "edges": []}
        stale = ce.mark_stale_refs(
            doc, live_item_ids={"vi_live"},
            live_material_ids={"m_live"})
        self.assertEqual(
            ["verify_item:vi_gone", "evidence:m_gone"], stale)
        by_id = {n["id"]: n for n in doc["nodes"]}
        self.assertTrue(by_id["verify_item:vi_gone"]["stale"])
        self.assertTrue(by_id["evidence:m_gone"]["stale"])
        self.assertNotIn("stale", by_id["verify_item:vi_live"])
        self.assertNotIn("stale", by_id["fact:f1"])

        # 复查对象恢复后 stale 清除（表达层标记可逆）
        stale2 = ce.mark_stale_refs(
            doc, live_item_ids={"vi_live", "vi_gone"},
            live_material_ids={"m_live", "m_gone"})
        self.assertEqual([], stale2)
        self.assertNotIn(
            "stale",
            {n["id"]: n for n in doc["nodes"]}["verify_item:vi_gone"])


if __name__ == "__main__":
    unittest.main()
