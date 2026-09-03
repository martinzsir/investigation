"""
tests/test_rule_r5_knowledge.py
REQ-024 R5 工商利益关联知识包参数化 测试。

覆盖：
  AC1: functions.json SQL 模板无硬编码人名（scan_hardcoded_names 零命中）
  AC2: 人名只来自 case_knowledge.json：李志强/李志强妻弟/张卫国 命中
  AC3: 过期 relation_assertions（valid_until）自动排除
  AC4: 无知识包时零命中且不报错
  AC5: 知识版本号随结果输出
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                          # noqa: E402
from core.ontology import build_ontology                        # noqa: E402
from core.functions import load_case_knowledge, FUNCTION_IMPLS  # noqa: E402
from scripts.scan_hardcoded_names import scan_functions_file    # noqa: E402


def _make_store() -> Store:
    s = Store(db_path=":memory:")
    s.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    s.execute("""INSERT INTO 工商信息 VALUES
        ('宏业建设', '李志强', '存续', NULL),
        ('A建材', '李志强妻弟', '存续', NULL),
        ('旧关联公司', '张三', '存续', '旧关联人'),
        ('无关公司', '王老五', '存续', NULL)""")
    return s


def _r5_rows(store):
    out = FUNCTION_IMPLS["org_interest_links"](store, {})
    return out["rows"], out.get("knowledge_version")


class TestR5Knowledge(unittest.TestCase):
    def test_ac1_no_hardcoded_names_in_functions(self):
        """AC1: functions.json 中无人名硬编码。"""
        hits = scan_functions_file(ROOT / "ontology" / "default" / "functions.json")
        self.assertEqual(hits, [])

    def test_ac2_persons_from_knowledge_hit(self):
        """AC2: 知识包主体/关系人命中的工商行被标记。"""
        store = _make_store()
        build_ontology(store.conn)
        rows, ver = _r5_rows(store)
        names = {r["raw_name"] for r in rows}
        self.assertIn("宏业建设", names)
        self.assertIn("A建材", names)
        self.assertNotIn("无关公司", names)
        a = next(r for r in rows if r["raw_name"] == "A建材")
        self.assertIn("李志强妻弟", a["matched_person"])

    def test_ac3_expired_assertion_excluded(self):
        """AC3: 过期断言涉及的人（旧关联人）不产生命中。"""
        store = _make_store()
        build_ontology(store.conn)
        rows, _ = _r5_rows(store)
        names = {r["raw_name"] for r in rows}
        self.assertNotIn("旧关联公司", names)
        for r in rows:
            self.assertNotIn("旧关联人", r["matched_person"])

    def test_ac4_missing_knowledge_zero_hits_no_error(self):
        """AC4: 无知识包 → 零命中、不报错。"""
        store = _make_store()
        build_ontology(store.conn)
        out = FUNCTION_IMPLS["org_interest_links"](store, {"pack": "no_such_pack"})
        self.assertEqual(out["rows"], [])

    def test_ac5_knowledge_version_attached(self):
        """AC5: 结果携带知识包版本号。"""
        kn = load_case_knowledge("default")
        self.assertTrue(kn["knowledge_version"])
        store = _make_store()
        build_ontology(store.conn)
        rows, ver = _r5_rows(store)
        self.assertEqual(ver, kn["knowledge_version"])
        self.assertTrue(all(r["knowledge_version"] == ver for r in rows))


if __name__ == "__main__":
    unittest.main()
