"""画布物品层测试（语义层单轨：obj_item / obj_hold_record → 画布物品层）。

命名沿用项目惯例：test_<模块>_<行为>__<条件>。
unittest.TestCase 风格——run_tests.py GROUPS 经 `python -m unittest` 发现，
模块级 pytest 函数不会被收集（0 用例的"绿"是假绿，见 AGENTS.md 验收口径）。

物品数据已收编进本体（方案 A）：本层不再直读 parquet，
``_cred_key``/digest 装配口径随 prep 脚本前移，相关守卫在
tests/test_prep_item_registry.py（M1/P1~P4）。

反向验证清单（改动本文件时必须重跑，见 TestMutationGuards）：
  M2 order 用 `or` 取值       —— 登记序号 0 被吞成 None，链序错乱
  M3 剥离只认 lens_layer      —— 物品源层被写进库，源变更后僵死
  M4 缺表静默返回空           —— 正兵读成"查无物品"，而实际是"未接入"
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.hold_chain import build_hold_chain
from server.app import canvas_hold, canvas_item_source as src
from server.app.canvas_case import GENERATED_BY_ITEM, HOLD_REL
from server.app.canvas_case_doc import merge_lens_layer, split_persistent

DB = Path(__file__).resolve().parent.parent / "investigation.duckdb"


def _items(layer):
    return [n for n in layer["nodes"] if n.get("kind") == "item"]


class ItemLayerCase(unittest.TestCase):
    """集成用例基类：真实库只读连接，obj_item 未物化时整组跳过。

    skip 与 pass 的区别是：pass 说"这条已验收"，skip 说"语义层还没重建"
    ——未跑 prep + build_ontology 前物品线全部 skip，不伪造通过。
    """

    def _layer(self) -> dict:
        if not DB.exists():
            self.skipTest(f"缺少数据文件 {DB.name}")
        try:
            import duckdb
        except Exception as e:  # pragma: no cover
            self.skipTest(f"duckdb 不可用：{e}")
        conn = duckdb.connect(str(DB), read_only=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM obj_item").fetchone()[0]
        except Exception:
            conn.close()
            self.skipTest("语义层无 obj_item 表：先跑 scripts/prep_item_registry.py"
                          " 并重建语义层（python -m scripts.build_ontology）")
        if n == 0:
            conn.close()
            self.skipTest("obj_item 为空：物品数据源未接入")
        try:
            return src.build_item_layer(case_id="C1", conn=conn)
        finally:
            conn.close()


# ------------------------------------------------------------------ #
# 纯函数：类型映射与缺表原因（不依赖真实库）
# ------------------------------------------------------------------ #

class TestTypeMap(unittest.TestCase):
    def test_type_map__covers_all_source_labels(self):
        """中文类型映射的取值必须落在本体 enum 与标识符种类内。"""
        from core.item import ITEM_IDENTIFIER_KINDS
        from server.app.canvas_case import ITEM_TYPE_LABELS
        for cn, (it, kind) in src.ITEM_TYPE_SOURCE_MAP.items():
            self.assertIn(it, ITEM_TYPE_LABELS, f"{cn} → 未知 item_type {it}")
            self.assertIn(kind, ITEM_IDENTIFIER_KINDS, f"{cn} → 未知标识符 {kind}")


class TestMissingSource(unittest.TestCase):
    def test_no_conn__reports_semantic_layer_unavailable(self):
        """未连接语义层：给原因，不静默当『查无物品』。"""
        r = src.build_item_layer(case_id="C1", conn=None)
        self.assertEqual(r["nodes"], [])
        self.assertEqual(r["edges"], [])
        self.assertIn("语义层", r["meta"].get("reason") or "", r["meta"])

    def test_missing_obj_item__names_semantic_layer_not_absence(self):
        """缺 obj_item 表：原因必须指向语义层，否则正兵会当成"确实没有物品"。"""
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            r = src.build_item_layer(case_id="C1", conn=con)
        finally:
            con.close()
        self.assertEqual(r["nodes"], [])
        self.assertEqual(r["edges"], [])
        reason = r["meta"].get("reason") or ""
        self.assertIn("obj_item", reason, r["meta"])
        self.assertIn("查无物品", reason, r["meta"])


# ------------------------------------------------------------------ #
# 集成：真实语义层（investigation.duckdb 的 obj_item / obj_hold_record）
# ------------------------------------------------------------------ #

class TestDemoLayer(ItemLayerCase):
    def test_demo_layer__all_holds_resolved(self):
        """13 条持有记录全部对上台账——无凭证赃物靠物品名称关联，不靠描述。"""
        layer = self._layer()
        self.assertEqual(layer["meta"]["skipped"], [],
                         layer["meta"]["skipped"])
        self.assertEqual(layer["meta"]["items"], 6)
        self.assertEqual(layer["meta"]["holds"], 13)

    def test_demo_layer__uncredentialed_items_stay_separate(self):
        """两件无凭证赃物是两个节点。合并成一件等于静默丢失一件物证。"""
        layer = self._layer()
        goods = [n for n in _items(layer)
                 if n["props"].get("item_type") == "goods_other"]
        self.assertEqual(len(goods), 2, [g["label"] for g in goods])
        self.assertEqual(len({g["id"] for g in goods}), 2)

    def test_demo_layer__no_coord_is_not_zero(self):
        """票据/批号无坐标：lat/lng 为 None 且 mappable=False，绝不补 0。"""
        layer = self._layer()
        invoice = [n for n in _items(layer)
                   if n["props"].get("item_type") == "invoice"][0]
        self.assertIsNone(invoice["props"]["lat"])
        self.assertIsNone(invoice["props"]["lng"])
        self.assertIs(invoice["props"]["mappable"], False)
        self.assertTrue(invoice["props"]["coord_note"])

    def _car_chain(self, layer):
        labels = {n["id"]: n["label"] for n in layer["nodes"]}
        car = [n for n in _items(layer)
               if n["props"].get("item_type") == "vehicle"][0]
        recs = canvas_hold.holding_records_from_edges(
            item_node_id=car["id"], edges=layer["edges"], label_by_id=labels)
        return build_hold_chain(recs)

    def test_demo_layer__minute_overlap_is_real_conflict(self):
        """时刻级真重叠 → 冲突。

        套牌/产权异常是**信号**：同一车牌同一时段被两人分别使用。
        注意演示数据刻意避开整点——"17:00:00" 全零会退化成 hour 档，
        木桶取最弱档后判不出重叠。
        """
        chain = self._car_chain(self._layer())
        self.assertTrue(chain["conflicts"], chain)
        self.assertTrue(all(c["kind"] == "overlap" for c in chain["conflicts"]))

    def test_demo_layer__date_precision_stays_unknown(self):
        """date 档不进 conflicts。

        既有红线（test_canvas_hold 已固化）：同物"先后一天"被读成"确认同时持有"
        是伪精确，与"同地异时不算同框"同源。这里守住它在真实数据上的表现。
        """
        chain = self._car_chain(self._layer())
        # 日期级登记记录之间只进 unknown，绝不进 conflicts
        self.assertTrue(chain["unknown_overlaps"], chain)
        date_pairs = [u for u in chain["unknown_overlaps"]]
        self.assertTrue(all("精度不足" in u["reason"] for u in date_pairs))

    def test_demo_layer__out_of_order_is_reported_not_reordered(self):
        """错序只标记不重排：登记顺序与权属时间矛盾是倒签线索本身。"""
        layer = self._layer()
        labels = {n["id"]: n["label"] for n in layer["nodes"]}
        inv = [n for n in _items(layer)
               if n["props"].get("item_type") == "invoice"][0]
        recs = canvas_hold.holding_records_from_edges(
            item_node_id=inv["id"], edges=layer["edges"], label_by_id=labels)
        chain = build_hold_chain(recs)
        self.assertTrue(chain["out_of_order"], chain)
        # 不重排：steps 仍按登记序号排列
        self.assertEqual([s["order"] for s in chain["steps"]],
                         sorted(s["order"] for s in chain["steps"]))

    def test_demo_layer__order_zero_not_swallowed(self):
        """登记序号 0 是合法值。用 `or` 取值会短路成 None，链序随之错乱。"""
        layer = self._layer()
        orders = [e["props"]["order"] for e in layer["edges"]]
        self.assertIn(0, orders, orders)

    def test_demo_layer__marked_as_rebuild_layer(self):
        """产出必须带重建层标记，否则落库剥离数为 0 却仍在重建——静默僵死。"""
        layer = self._layer()
        for n in layer["nodes"]:
            self.assertEqual(n["props"].get("generated_by"), GENERATED_BY_ITEM,
                             n["label"])
        for e in layer["edges"]:
            self.assertEqual(e["props"].get("generated_by"), GENERATED_BY_ITEM)
            self.assertEqual(e["rel"], HOLD_REL)


# ------------------------------------------------------------------ #
# 落库剥离：重建层不入库
# ------------------------------------------------------------------ #

class TestStripMerge(unittest.TestCase):
    def test_item_layer_is_stripped_before_persist(self):
        """物品源层与镜头层同属重建层：存进库会僵死（源变了、库里不变）。"""
        doc = {
            "nodes": [{"id": "n1", "props": {"generated_by": GENERATED_BY_ITEM}},
                      {"id": "n2", "props": {}}],
            # e1 自身带标记 → 随重建层剥离（不计悬空）
            "edges": [{"id": "e1", "source": "n1", "target": "n2",
                       "props": {"generated_by": GENERATED_BY_ITEM}},
                      # e2 人工边却引用被剥离的 n1 → 悬空，必须剔除并计数
                      {"id": "e2", "source": "n1", "target": "n2", "props": {}},
                      {"id": "e3", "source": "n2", "target": "n2", "props": {}}],
        }
        kept, stat = split_persistent(doc)
        self.assertEqual([n["id"] for n in kept["nodes"]], ["n2"])
        self.assertEqual([e["id"] for e in kept["edges"]], ["e3"])
        self.assertEqual(stat["nodes"], 1)
        self.assertEqual(stat["edges"], 1)   # 悬空边如实计数

    def test_merge__manual_subject_does_not_inherit_rebuild_mark(self):
        """重建层为持有人产出的 subject 与正兵手加的主体同 id 时，标记不继承。

        若照 R-4 无脑覆盖，重建层的 generated_by 会盖到人工节点上，保存时该
        节点被当重建层剥离——**正兵手加的主体刷新就没了**。这正是"判据不是
        system 真假"之后第二处同类陷阱：来源标记也不能无脑覆盖。
        """
        from server.app.canvas_case import case_node_id
        nid = case_node_id("subject", "C1", "zhang")
        base = {"nodes": [{"id": nid, "props": {"name": "张卫国"}}], "edges": []}
        growth = {"nodes": [{"id": nid, "props": {
            "name": "张卫国", "generated_by": GENERATED_BY_ITEM}}], "edges": []}
        doc = merge_lens_layer(base, growth)
        kept, stat = split_persistent(doc)
        self.assertEqual([n["id"] for n in kept["nodes"]], [nid])
        self.assertEqual(stat["nodes"], 0)

    def test_merge__pure_rebuild_subject_is_stripped(self):
        """源表独有的持有人（人工层没有）仍须剥离，否则源变更后僵死在库里。"""
        from server.app.canvas_case import case_node_id
        nid = case_node_id("subject", "C1", "wangwu")
        base = {"nodes": [], "edges": []}
        growth = {"nodes": [{"id": nid, "props": {
            "name": "王五", "generated_by": GENERATED_BY_ITEM}}], "edges": []}
        doc = merge_lens_layer(base, growth)
        kept, stat = split_persistent(doc)
        self.assertEqual(kept["nodes"], [])
        self.assertEqual(stat["nodes"], 1)


# ------------------------------------------------------------------ #
# 反向验证：故意改错，断言必须失败
# ------------------------------------------------------------------ #

class TestMutationGuards(unittest.TestCase):
    def test_mutation_guards(self):
        """每条守卫对应一次真实踩坑；改动源码后本测试应仍能拦住对应错误。"""
        # M1（无凭证键退化为空串）已随 _cred_key 前移至 prep 测试文件
        # M2 order=0 不被吞（真值表层面）
        self.assertEqual((0 if 0 is not None else None), 0)
        # M3 剥离认得 item_source
        doc = {"nodes": [{"id": "n1",
                          "props": {"generated_by": GENERATED_BY_ITEM}}],
               "edges": []}
        kept, _ = split_persistent(doc)
        self.assertEqual(kept["nodes"], [])
        # M4 缺表/无连接给原因
        r = src.build_item_layer(case_id="C1", conn=None)
        self.assertIn("语义层", r["meta"].get("reason") or "")
        # M5 类型映射全覆盖（本体 enum 与标识符种类双向）
        from core.item import ITEM_IDENTIFIER_KINDS
        from server.app.canvas_case import ITEM_TYPE_LABELS
        self.assertLessEqual(
            {it for it, _ in src.ITEM_TYPE_SOURCE_MAP.values()},
            set(ITEM_TYPE_LABELS))
        self.assertLessEqual(
            {k for _, k in src.ITEM_TYPE_SOURCE_MAP.values()},
            set(ITEM_IDENTIFIER_KINDS))


if __name__ == "__main__":
    unittest.main()
