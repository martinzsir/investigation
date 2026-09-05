"""
tests/test_policy_version.py
REQ-G-016 policies.json / case_knowledge.json 纳入统一版本校验：
  - policies.json schema_version 必须 = 2（缺失/错误 → 装载期硬失败）
  - case_knowledge.json 存在时必须带 schema_version=2（缺失/错误硬失败）；
    文件缺失仍回落空骨架（config_missing，不报错）
  - 同步：默认包 policies.json / case_knowledge.json 均为版本 2，可正常装载
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.policy import PolicyEngine, SCHEMA_VERSION as POLICY_SCHEMA
from core.functions import load_case_knowledge


def _write_policies(path: Path, schema_version, **extra):
    payload = {
        "object_policies": [{"object": "person",
                             "roles": ["正兵"], "min_clearance": 0}],
        "link_policies": [],
        "property_policies": [],
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class PolicyVersionTests(unittest.TestCase):
    def test_missing_schema_version_hard_fails(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "policies.json"
            _write_policies(p, schema_version=None)
            with self.assertRaises(ValueError) as ctx:
                PolicyEngine("default", path=p)
            self.assertIn("schema_version", str(ctx.exception))

    def test_wrong_schema_version_hard_fails(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "policies.json"
            _write_policies(p, schema_version=99)
            with self.assertRaises(ValueError):
                PolicyEngine("default", path=p)

    def test_correct_version_loads(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "policies.json"
            _write_policies(p, schema_version=POLICY_SCHEMA)
            pe = PolicyEngine("default", path=p)
            self.assertFalse(getattr(pe, "_missing_file", False))
            self.assertIn("person", pe.object_policies)

    def test_default_pack_policies_version2(self):
        # 默认包策略文件可正常构造（回归：升版后默认包不硬失败）
        pe = PolicyEngine("default")
        self.assertFalse(getattr(pe, "_missing_file", False))

    def test_default_pack_case_knowledge_version2(self):
        kn = load_case_knowledge("default")
        self.assertEqual(kn.get("schema_version"), 2)
        self.assertTrue(kn.get("subject_aliases"))

    def test_case_knowledge_missing_file_skeleton(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("core.ontology_loader.PACK_ROOT", Path(td)):
                kn = load_case_knowledge("no_such_pack")
        self.assertIsNone(kn["knowledge_version"])  # 空骨架，不报错

    def test_case_knowledge_bad_version_hard_fails(self):
        with tempfile.TemporaryDirectory() as td:
            pack = Path(td) / "vpack"
            pack.mkdir()
            (pack / "case_knowledge.json").write_text(json.dumps({
                "knowledge_version": "x", "subject_aliases": {},
                "relation_assertions": [],
            }), encoding="utf-8")
            with mock.patch("core.ontology_loader.PACK_ROOT", Path(td)):
                with self.assertRaises(ValueError) as ctx:
                    load_case_knowledge("vpack")
            self.assertIn("schema_version", str(ctx.exception))

    def test_case_knowledge_correct_version_loads(self):
        with tempfile.TemporaryDirectory() as td:
            pack = Path(td) / "okpack"
            pack.mkdir()
            (pack / "case_knowledge.json").write_text(json.dumps({
                "schema_version": 2, "knowledge_version": "t",
                "subject_aliases": {"甲": ["甲"]}, "relation_assertions": [],
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch("core.ontology_loader.PACK_ROOT", Path(td)):
                kn = load_case_knowledge("okpack")
            self.assertEqual(kn["schema_version"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
