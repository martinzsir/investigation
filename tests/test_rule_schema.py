"""
tests/test_rule_schema.py
REQ-022 hit_when 枚举校验 测试。

覆盖 AC1-AC4：
  AC1: hit_when 为非法值 → 装载抛错
  AC2: hit_when 缺失 → 抛错
  AC3: 合法值（rows_nonempty / result_hit）正常装载
  AC4: 错误信息含规则 id 与字段路径
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology_loader import _load_rules, load_pack          # noqa: E402
from core.ontology import FunctionSpec                           # noqa: E402


def _make_fspec(name: str = "f1") -> FunctionSpec:
    """构造最小 FunctionSpec 供测试用。"""
    return FunctionSpec(
        name=name, title="t", inputs=("obj_person",),
        output_type="rows", impl="sql", sql="SELECT 1",
        parameters={})


def _make_rule(**overrides) -> dict:
    """构造最小合法 rule dict，可用 overrides 覆盖字段。"""
    base = {
        "id": "R1", "stage": "xu_shi", "title": "测试规则",
        "rule_text": "x" * 40,  # 超过 RULE_TEXT_MIN=30
        "function": "f1", "hit_when": "rows_nonempty",
    }
    base.update(overrides)
    return base


def _write_rules(tmpdir: Path, rules: list[dict]) -> Path:
    """把 rules 列表写入临时 rules.json，返回路径。"""
    p = tmpdir / "rules.json"
    p.write_text(
        json.dumps({"schema_version": 2, "rules": rules}, ensure_ascii=False),
        encoding="utf-8")
    return p


class TestHitWhenValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.functions = {"f1": _make_fspec()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_ac1_invalid_value_raises(self):
        """AC1: hit_when 为非法值 → 装载抛错"""
        path = _write_rules(self.tmpdir, [_make_rule(hit_when="result_hits")])
        with self.assertRaises(ValueError) as cm:
            _load_rules(path, self.functions, required=True)
        self.assertIn("hit_when", str(cm.exception))
        self.assertIn("result_hits", str(cm.exception))

    def test_ac2_missing_raises(self):
        """AC2: hit_when 缺失 → _require 抛错"""
        r = _make_rule()
        del r["hit_when"]
        path = _write_rules(self.tmpdir, [r])
        with self.assertRaises(ValueError) as cm:
            _load_rules(path, self.functions, required=True)
        self.assertIn("hit_when", str(cm.exception))

    def test_ac3_valid_values_load(self):
        """AC3: 合法值（rows_nonempty / result_hit）正常装载"""
        for v in ["rows_nonempty", "result_hit"]:
            path = _write_rules(self.tmpdir, [_make_rule(id=f"R_{v}", hit_when=v)])
            rules = _load_rules(path, self.functions, required=True)
            self.assertEqual(rules[f"R_{v}"].hit_when, v)

    def test_ac4_error_contains_rule_id_and_path(self):
        """AC4: 错误信息含规则 id 与字段路径（ctx + rid）"""
        path = _write_rules(self.tmpdir, [_make_rule(id="R9", hit_when="bad")])
        with self.assertRaises(ValueError) as cm:
            _load_rules(path, self.functions, required=True)
        msg = str(cm.exception)
        self.assertIn("R9", msg)         # 规则 id
        self.assertIn("hit_when", msg)   # 字段路径
        self.assertIn("bad", msg)        # 非法值

    def test_default_pack_rules_load(self):
        """default pack 的 rules.json 应正常装载（回归验证）"""
        pack = load_pack("default")
        self.assertTrue(len(pack.rules) > 0)
        for rid, r in pack.rules.items():
            self.assertIn(r.hit_when, {"rows_nonempty", "result_hit"})


if __name__ == "__main__":
    unittest.main()
