"""物品节点入图（ITM 画布层）— build_item_node / item_hold_records。

每条断言都配反向验证（变异）说明：改错代码后必须有断言失败，否则该断言
是恒真的、等于没测。
"""
import unittest

from server.app.canvas_case import (ITEM_TYPE_LABELS, build_item_node,
                                    item_hold_records)

CASE = "C1"


def _vehicle(**kw):
    base = dict(case_id=CASE, title="涉案车辆", item_type="vehicle",
                identifiers=[{"kind": "plate", "value": "浙A12345"}])
    base.update(kw)
    return build_item_node(**base)


class TestItemIdentity(unittest.TestCase):
    """身份键必须含 item_type（本体 identity_key = [item_type, digest]）。"""

    def test_t1_same_value_different_type_not_merged(self):
        """变异：id 只用 digest 不带 item_type → 两者撞成同一节点。"""
        a = build_item_node(case_id=CASE, title="车", item_type="vehicle",
                            identifiers=[{"kind": "plate", "value": "A123"}])
        b = build_item_node(case_id=CASE, title="设备", item_type="device",
                            identifiers=[{"kind": "plate", "value": "A123"}])
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["props"]["primary_digest"],
                         b["props"]["primary_digest"])   # digest 确实同形

    def test_t2_idempotent(self):
        a = _vehicle()
        b = _vehicle()
        self.assertEqual(a["id"], b["id"])

    def test_t3_kind_is_item(self):
        self.assertEqual(_vehicle()["kind"], "item")

    def test_t4_manual_node_is_not_system(self):
        """人工登记：可改可删，不得标 system=True（否则正兵删不掉）。"""
        self.assertFalse(_vehicle()["system"])


class TestNoCredential(unittest.TestCase):
    """D4：无凭证不静默合并。"""

    def test_t5_descriptors_make_distinct_entities(self):
        """变异：descriptors 不参与 digest → 两件赃物合成一个节点。"""
        a = build_item_node(case_id=CASE, title="赃物甲", item_type="goods_other",
                            descriptors={"外观": "黑色背包", "发现地": "A 处"})
        b = build_item_node(case_id=CASE, title="赃物乙", item_type="goods_other",
                            descriptors={"外观": "银色箱", "发现地": "B 处"})
        self.assertNotEqual(a["id"], b["id"])
        self.assertTrue(a["props"]["unidentified"])
        self.assertFalse(a["props"]["credentialed"])

    def test_t6_same_descriptors_same_entity(self):
        d = {"外观": "黑色背包"}
        a = build_item_node(case_id=CASE, title="X", item_type="goods_other",
                            descriptors=d)
        b = build_item_node(case_id=CASE, title="Y", item_type="goods_other",
                            descriptors=d)
        self.assertEqual(a["id"], b["id"])

    def test_t7_no_credential_no_descriptor_rejected(self):
        """变异：空 digest 时给共享 ref → 所有无凭证物品静默合并。"""
        with self.assertRaises(ValueError):
            build_item_node(case_id=CASE, title="赃物", item_type="goods_other")
        with self.assertRaises(ValueError):
            build_item_node(case_id=CASE, title="赃物", item_type="goods_other",
                            descriptors={"备注": ""})

    def test_t8_none_kind_identifier_still_needs_descriptor(self):
        with self.assertRaises(ValueError):
            build_item_node(case_id=CASE, title="赃物", item_type="goods_other",
                            identifiers=[{"kind": "none", "value": ""}])


class TestInputValidation(unittest.TestCase):
    def test_t9_unknown_item_type_rejected(self):
        """变异：未知类型不报错而回落其他 → 类型口径被污染。"""
        with self.assertRaises(ValueError):
            build_item_node(case_id=CASE, title="X", item_type="aircraft")
        for t in ITEM_TYPE_LABELS:
            build_item_node(case_id=CASE, title="X", item_type=t,
                            identifiers=[{"kind": "serial", "value": "S1"}])

    def test_t10_unknown_identifier_kind_rejected(self):
        with self.assertRaises(ValueError):
            build_item_node(case_id=CASE, title="车", item_type="vehicle",
                            identifiers=[{"kind": "chassis", "value": "X"}])


class TestNoPlaintext(unittest.TestCase):
    """R14：只存摘要不落明文。"""

    def test_t11_no_plaintext_field(self):
        """变异：build_identifier 加 value 明文字段 → 此处抓到。"""
        n = build_item_node(
            case_id=CASE, title="手机", item_type="device",
            identifiers=[{"kind": "imei", "value": "860000000000001"},
                         {"kind": "msisdn", "value": "13800138000"}])
        for i in n["props"]["identifiers"]:
            self.assertNotIn("value", i)
            self.assertIn("value_digest", i)
        blob = repr(n)
        self.assertNotIn("860000000000001", blob)
        self.assertNotIn("13800138000", blob)

    def test_t12_sensitive_kinds_flagged(self):
        n = build_item_node(case_id=CASE, title="手机", item_type="device",
                            identifiers=[{"kind": "imei", "value": "86"}])
        self.assertIn("imei", n["props"]["sensitive_kinds"])
        self.assertTrue(n["props"]["identifiers"][0]["sensitive"])

    def test_t13_nonsensitive_not_flagged(self):
        n = _vehicle()
        self.assertEqual(n["props"]["sensitive_kinds"], [])


class TestCoordinates(unittest.TestCase):
    def test_t14_missing_coord_not_zero(self):
        """变异：坐标缺失塞 0 → 物品被画到几内亚湾（0,0）。"""
        n = _vehicle()
        self.assertIsNone(n["props"]["lat"])
        self.assertIsNone(n["props"]["lng"])
        self.assertFalse(n["props"]["mappable"])
        self.assertIsNotNone(n["props"]["coord_note"])

    def test_t15_partial_coord_not_mappable(self):
        """只给一个坐标：不是"另一个为 0"，而是根本不可落点。"""
        n = _vehicle(lat=30.27)
        self.assertFalse(n["props"]["mappable"])

    def test_t16_full_coord_mappable(self):
        n = _vehicle(lat=30.27, lng=120.15)
        self.assertTrue(n["props"]["mappable"])
        self.assertAlmostEqual(n["props"]["lat"], 30.27)


class TestHoldRecords(unittest.TestCase):
    def test_t17_direction_only_into_item(self):
        """变异：反向边（item→person）也认 → 方向错了却画得出来。"""
        nodes = [{"id": "p1", "kind": "subject", "label": "张卫国"},
                 {"id": "i1", "kind": "item", "label": "浙A12345"}]
        edges = [{"source": "p1", "target": "i1",
                  "props": {"start_date": "2024-01-01", "end_date": "2025-01-01"}},
                 {"source": "i1", "target": "p1",
                  "props": {"start_date": "2024-01-01"}}]
        recs = item_hold_records(nodes, edges)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["holder"], "张卫国")
        self.assertEqual(recs[0]["item"], "浙A12345")

    def test_t18_non_item_target_ignored(self):
        nodes = [{"id": "p1", "kind": "subject", "label": "A"},
                 {"id": "p2", "kind": "subject", "label": "B"}]
        edges = [{"source": "p1", "target": "p2", "props": {}}]
        self.assertEqual(item_hold_records(nodes, edges), [])

    def test_t19_dates_carried(self):
        nodes = [{"id": "p1", "kind": "subject", "label": "A"},
                 {"id": "i1", "kind": "item", "label": "车"}]
        edges = [{"source": "p1", "target": "i1",
                  "props": {"start_date": "2024-03-10", "end_date": None}}]
        recs = item_hold_records(nodes, edges)
        self.assertEqual(recs[0]["start_date"], "2024-03-10")
        self.assertIsNone(recs[0]["end_date"])

    def test_t20_empty_graph(self):
        self.assertEqual(item_hold_records([], []), [])


if __name__ == "__main__":
    unittest.main()
