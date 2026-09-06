"""REQ-D-007 清洗词表外置（clean_rules.json）测试。

词表从代码硬编码（贪腐词表 ORG_KEYWORDS）外置到案件包 clean_rules.json：
  - 文件缺失/空表 merge → 回落内置基线（no-op）；
  - merge（默认）：基线 + 案件词追加；
  - replace：案件词表整体替换基线（电诈词表换贪腐词表）；
  - 非字符串数组 / 非法 mode → 装载硬失败。
exclude_org_tokens 经 CleanContext 读案件词表；普通 set 回落基线（旧调用兼容）。
"""
import json
import unittest

import duckdb

from core.ontology import build_ontology
from core import ontology_loader as ol
from core.clean_ops import build_clean_context
from tests.test_one2one import _PackCtx

_OBJ = {"name": "person", "title": "人员", "pk": "person_id",
        "kind": "entity", "name_property": "name",
        "properties": {"name": "string"}}
_BIND = {"object": "person",
         "source": {"table": "PERS", "columns": {"name": "姓名"}},
         "clean": ["exclude_org_tokens"]}
_NAMES = [("张三",), ("某公司",), ("洗钱窝点",), ("客服退款",)]


def _build(clean_rules: dict | None):
    with _PackCtx([_OBJ], [_BIND]) as pc:
        if clean_rules is not None:
            (pc.d / "clean_rules.json").write_text(
                json.dumps(clean_rules, ensure_ascii=False), encoding="utf-8")
        conn = duckdb.connect(":memory:")
        conn.execute('CREATE TABLE PERS ("姓名" VARCHAR)')
        conn.executemany("INSERT INTO PERS VALUES (?)", _NAMES)
        build_ontology(conn, pack="p")
        return {r[0] for r in
                conn.execute("SELECT name FROM obj_person").fetchall()}


class TestWordlist(unittest.TestCase):
    def test_default_baseline_when_no_file(self):
        """无 clean_rules.json → 内置基线生效（贪腐词表）：某公司剔除、洗钱窝点保留。"""
        names = _build(None)
        self.assertIn("张三", names)
        self.assertNotIn("某公司", names)        # 基线词「公司」
        self.assertIn("洗钱窝点", names)          # 基线无此词

    def test_merge_appends_case_words(self):
        """merge：基线 + 案件词追加，两者同时生效。"""
        names = _build({"schema_version": 2, "mode": "merge",
                        "org_keywords": ["洗钱窝点"], "summary_tokens": []})
        self.assertIn("张三", names)
        self.assertNotIn("某公司", names)         # 基线词仍在
        self.assertNotIn("洗钱窝点", names)       # 追加电诈词生效

    def test_replace_switches_wordlist(self):
        """replace：电诈词表整体替换贪腐基线——基线词失效、电诈词生效。"""
        names = _build({"schema_version": 2, "mode": "replace",
                        "org_keywords": ["客服", "退款"], "summary_tokens": []})
        self.assertIn("张三", names)
        self.assertIn("某公司", names)            # 基线「公司」已被替换 → 不再剔除
        self.assertNotIn("客服退款", names)       # 电诈词生效

    def test_empty_tables_noop(self):
        """空词表：merge 回落基线（no-op）；replace 空表 = 仅靠 org 名单。"""
        merged = _build({"schema_version": 2, "mode": "merge",
                         "org_keywords": [], "summary_tokens": []})
        self.assertNotIn("某公司", merged)        # 基线仍生效
        replaced = _build({"schema_version": 2, "mode": "replace",
                           "org_keywords": [], "summary_tokens": []})
        self.assertIn("某公司", replaced)         # 词表空 → 不剔除
        self.assertIn("客服退款", replaced)

    def test_invalid_declaration_hard_fail(self):
        """非字符串数组 / 非法 mode → 装载硬失败；build_clean_context 同步拦截。"""
        with _PackCtx([_OBJ], [_BIND]) as pc:
            (pc.d / "clean_rules.json").write_text(
                '{"schema_version":2,"org_keywords":["好",123]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                ol.load_pack("p")
        with _PackCtx([_OBJ], [_BIND]) as pc:
            (pc.d / "clean_rules.json").write_text(
                '{"schema_version":2,"mode":"nope","org_keywords":[]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                ol.load_pack("p")
        with self.assertRaises(ValueError):
            build_clean_context(set(), {"mode": "nope"})


if __name__ == "__main__":
    unittest.main()
