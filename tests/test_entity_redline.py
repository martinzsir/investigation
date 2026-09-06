"""红线 R-1 同名实体强证据分区测试（鲁棒性测试 B1-02/03 修复回归）。

场景：两个完全同名"李强"（涉案车手 vs 无辜教师），电话/身份证互不相同。
修复前：规范化同名 → confidence=1.0 强合并、needs_review=False、复核队列 0 候选。
修复后：同名组内强证据互斥（≥2 组互不连通）→ 强制拆为独立簇、全部 needs_review。

对照用例锁定既有行为不回归：
  - 同名同电话（共享强证据）→ 仍 1 簇强合并
  - 多写法无强证据（赵鹏场景）→ 仍 1 簇 0.9 待复核
  - 跨名共享手机号 → 仍强合并
  - 拆簇后不得经共享手机号"桥接记录"回并（_recs 精确取记录守卫）
"""
import unittest

from core.entity import _load_person_resolver

# 该加载器会做路径安全加载 + 注入 review_candidates()（与 test_review_queue 同口径）
EntityResolver = _load_person_resolver()


def liqiang_resolver():
    """两个李强：涉案车手(138) vs 无辜教师(139)，同名不同人。"""
    er = EntityResolver()
    er.ingest([
        {"name": "李强", "phone": "13800000001", "source_row_id": "车档.李强(车手)"},
        {"name": "李强", "phone": "13900000002", "source_row_id": "人员表.李强(教师)"},
    ])
    er.resolve()
    return er


class TestSameNameRedline(unittest.TestCase):
    def test_01_two_liqiang_different_phones_split(self):
        """红线：同名不同电话 → 拆两簇、全部待复核、不进映射、id 不碰撞。"""
        er = liqiang_resolver()
        liq = [c for c in er.clusters() if c.canonical_name == "李强"]
        self.assertEqual(len(liq), 2)
        self.assertTrue(all(c.needs_review for c in liq))
        self.assertEqual(len({c.entity_id for c in liq}), 2)
        review = [c for c in er.review_candidates() if c.canonical_name == "李强"]
        self.assertEqual(len(review), 2)
        self.assertNotIn("李强", er.mapping())

    def test_02_strong_merges_report_excludes_split(self):
        """report 强合并列表不含同名拆簇（无静默合并痕迹）。"""
        strong = [m for m in liqiang_resolver().report()["strong_merges"]
                  if m["canonical"] == "李强"]
        self.assertEqual(strong, [])

    def test_03_name_variant_with_conflicting_phone_splits(self):
        """写法变体（'李 强'）与 '李强' 电话互斥 → 同样拆簇（canonical 保留原始写法）。"""
        from entity_resolution import normalize_person_name
        er = EntityResolver()
        er.ingest([
            {"name": "李强", "phone": "13800000001", "source_row_id": "a"},
            {"name": "李 强", "phone": "13900000002", "source_row_id": "b"},
        ])
        cs = [c for c in er.resolve()
              if normalize_person_name(c.canonical_name) == "李强"]
        self.assertEqual(len(cs), 2)
        self.assertTrue(all(c.needs_review for c in cs))

    def test_04_id_card_conflict_also_splits(self):
        """同名不同身份证（遮蔽格式）→ 拆簇。"""
        er = EntityResolver()
        er.ingest([
            {"name": "李强", "id_card": "4403**********0001", "source_row_id": "a"},
            {"name": "李强", "id_card": "4403**********0002", "source_row_id": "b"},
        ])
        liq = [c for c in er.resolve() if c.canonical_name == "李强"]
        self.assertEqual(len(liq), 2)
        self.assertTrue(all(c.needs_review for c in liq))

    def test_05_no_key_record_in_conflict_group_goes_review(self):
        """同名组内无强证据记录：拆簇时自成单例待裁决，不自动归属。"""
        er = EntityResolver()
        er.ingest([
            {"name": "李强", "phone": "13800000001", "source_row_id": "a"},
            {"name": "李强", "phone": "13900000002", "source_row_id": "b"},
            {"name": "李强", "source_row_id": "c"},
        ])
        liq = [c for c in er.resolve() if c.canonical_name == "李强"]
        self.assertEqual(len(liq), 3)
        self.assertTrue(all(c.needs_review for c in liq))
        self.assertEqual(len({c.entity_id for c in liq}), 3)

    def test_06_no_remerge_via_bridge_records(self):
        """守卫：拆簇后不得经共享手机号桥接回并（_recs 精确取记录）。"""
        er = EntityResolver()
        er.ingest([
            {"name": "李强", "phone": "13800000001", "source_row_id": "a"},
            {"name": "李强", "phone": "13900000002", "source_row_id": "b"},
            {"name": "老李", "phone": "13800000001", "source_row_id": "c"},
            {"name": "老李", "phone": "13900000002", "source_row_id": "d"},
        ])
        liq = [c for c in er.clusters() if c.canonical_name == "李强"]
        self.assertEqual(len(liq), 2)   # 李强{138}+老李{138} / 李强{139}+老李{139}
        self.assertTrue(all(c.needs_review for c in liq))
        self.assertEqual(len({c.entity_id for c in liq}), 2)


class TestNoRegression(unittest.TestCase):
    def test_01_same_name_same_phone_still_merges(self):
        """对照：同名同电话（共享强证据）→ 仍 1 簇强合并。"""
        er = EntityResolver()
        er.ingest([
            {"name": "王秀英", "phone": "13700000001", "source_row_id": "a"},
            {"name": "王秀英", "phone": "13700000001", "source_row_id": "b"},
        ])
        cs = [c for c in er.resolve() if c.canonical_name == "王秀英"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].confidence, 1.0)
        self.assertFalse(cs[0].needs_review)

    def test_02_shared_id_card_overrides_different_phones(self):
        """同身份证 + 不同电话（一人多号）→ 1 簇强合并。"""
        er = EntityResolver()
        er.ingest([
            {"name": "李强", "id_card": "4403**********0001", "phone": "138", "source_row_id": "a"},
            {"name": "李强", "id_card": "4403**********0001", "phone": "139", "source_row_id": "b"},
        ])
        cs = [c for c in er.resolve() if c.canonical_name == "李强"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].confidence, 1.0)

    def test_03_multi_variant_no_evidence_unchanged(self):
        """对照：多写法无强证据（赵鹏场景）→ 仍 1 簇 0.9 待复核，不拆。"""
        er = EntityResolver()
        er.ingest([
            {"name": "赵鹏", "source_row_id": "人员表"},
            {"name": "赵 鹏", "source_row_id": "车档"},
            {"name": "赵鹏(男)", "source_row_id": "笔录"},
        ])
        cs = [c for c in er.resolve() if "赵鹏" in c.canonical_name]
        self.assertEqual(len(cs), 1)
        self.assertAlmostEqual(cs[0].confidence, 0.9)
        self.assertTrue(cs[0].needs_review)

    def test_04_cross_name_phone_merge_unchanged(self):
        """对照：不同名共享手机号 → 跨组强合并（既有行为）。"""
        er = EntityResolver()
        er.ingest([
            {"name": "张三", "phone": "13800000001", "source_row_id": "a"},
            {"name": "王五", "phone": "13800000001", "source_row_id": "b"},
        ])
        self.assertEqual(len(er.resolve()), 1)
        self.assertFalse(er.clusters()[0].needs_review)


class TestStoreCollection(unittest.TestCase):
    def test_collect_phone_evidence_from_store(self):
        """采集层：源表带电话列时，同名不同电话在 store 管道内也拆簇。"""
        from core import Store
        from core.registry import _resolve_person_from_store
        s = Store(db_path=":memory:")
        s.execute('CREATE TABLE "人员表" ("姓名" VARCHAR, "电话" VARCHAR, "身份证号" VARCHAR)')
        s.execute('INSERT INTO "人员表" VALUES (?, ?, ?)', ["李强", "13800000001", "x1"])
        s.execute('INSERT INTO "人员表" VALUES (?, ?, ?)', ["李强", "13900000002", "x2"])
        r = _resolve_person_from_store(s)
        r.resolve()
        liq = [c for c in r.clusters() if c.canonical_name == "李强"]
        self.assertEqual(len(liq), 2)
        self.assertTrue(all(c.needs_review for c in liq))

    def test_collect_degrades_when_no_evidence_cols(self):
        """采集层：源表无电话/身份证列 → 降级只取名字，不报错。"""
        from core import Store
        from core.registry import _resolve_person_from_store
        s = Store(db_path=":memory:")
        s.execute('CREATE TABLE "银行流水" ("主体" VARCHAR, "对方" VARCHAR)')
        s.execute('INSERT INTO "银行流水" VALUES (?, ?)', ["陈学勤", "王润芳"])
        r = _resolve_person_from_store(s)
        names = {c.canonical_name for c in r.clusters()}
        self.assertIn("陈学勤", names)
        self.assertIn("王润芳", names)


if __name__ == "__main__":
    unittest.main()
