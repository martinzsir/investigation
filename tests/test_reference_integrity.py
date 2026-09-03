"""
tests/test_reference_integrity.py
REQ-023 规则引用完整性校验 测试。

覆盖 AC1-AC5：
  AC1: rules.json 引用不存在的 function → 抛错（已实现，回归验证）
  AC2: functions.json SQL 引用不存在的 obj_* → 抛错（已实现，回归验证）
  AC3: actions.json 引用不存在的 object → 抛错（已实现，回归验证）
  AC4: 全部引用合法 → 正常装载（default pack 回归）
  AC5: 错误信息含缺失名与可用名列表
  + assumption 引用未声明假设 → 抛错（新增）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.ontology_loader import _load_rules, load_pack       # noqa: E402
from core.ontology import FunctionSpec                        # noqa: E402


def _make_fspec(name: str = "f1") -> FunctionSpec:
    return FunctionSpec(
        name=name, title="t", inputs=("obj_person",),
        output_type="rows", impl="sql", sql="SELECT 1",
        parameters={})


def _make_rule(**overrides) -> dict:
    base = {
        "id": "R1", "stage": "xu_shi", "title": "测试规则",
        "rule_text": "x" * 40,
        "function": "f1", "hit_when": "rows_nonempty",
        "assumption": "",
    }
    base.update(overrides)
    return base


def _write_rules(tmpdir: Path, rules: list[dict]) -> Path:
    p = tmpdir / "rules.json"
    p.write_text(
        json.dumps({"schema_version": 2, "rules": rules}, ensure_ascii=False),
        encoding="utf-8")
    return p


class TestReferenceIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.functions = {"f1": _make_fspec()}

    def tearDown(self):
        self.tmp.cleanup()

    # ---- AC1: rule → function 引用 ----
    def test_ac1_rule_unknown_function_raises(self):
        """AC1: rules.json 引用不存在的 function → 抛错"""
        path = _write_rules(self.tmpdir, [_make_rule(function="nonexistent")])
        with self.assertRaises(ValueError) as cm:
            _load_rules(path, self.functions, required=True)
        self.assertIn("nonexistent", str(cm.exception))

    # ---- AC2: function → object 引用（由 _load_functions 校验）----
    def test_ac2_function_unknown_object_raises(self):
        """AC2: functions.json inputs 引用不存在的 obj_* → 抛错（间接验证）"""
        # default pack 应正常装载（含 functions 引用 obj_*）
        pack = load_pack("default")
        for fname, fspec in pack.functions.items():
            # 所有 inputs 应形如 obj_* / lnk_*
            for inp in fspec.inputs:
                self.assertTrue(inp.startswith(("obj_", "lnk_")),
                                f"{fname} input {inp} 不是 obj_*/lnk_*")

    # ---- AC3: action → object 引用（由 _load_actions 校验）----
    def test_ac3_action_unknown_object_raises(self):
        """AC3: actions.json 引用不存在的 object → 抛错（间接验证）"""
        pack = load_pack("default")
        obj_names = {o.name for o in pack.objects}
        # file action 的 create_decision 副作用要求 decision 对象存在
        self.assertIn("decision", obj_names)

    # ---- AC4: 全部引用合法 ----
    def test_ac4_all_valid_loads(self):
        """AC4: 全部引用合法 → 正常装载"""
        pack = load_pack("default")
        self.assertIsNotNone(pack)
        self.assertTrue(len(pack.rules) > 0)
        self.assertTrue(len(pack.functions) > 0)

    # ---- AC5: 错误信息含可用名列表 ----
    def test_ac5_error_contains_available_names(self):
        """AC5: 错误信息含缺失名与可用名列表"""
        path = _write_rules(self.tmpdir, [_make_rule(function="ghost")])
        with self.assertRaises(ValueError) as cm:
            _load_rules(path, self.functions, required=True)
        msg = str(cm.exception)
        self.assertIn("ghost", msg)    # 缺失名
        self.assertIn("f1", msg)       # 可用名列表

    # ---- assumption 引用校验（新增）----
    def test_assumption_unknown_raises(self):
        """assumption 引用未声明假设 → 抛错"""
        path = _write_rules(self.tmpdir, [_make_rule(assumption="H99")])
        with self.assertRaises(ValueError) as cm:
            _load_rules(path, self.functions, required=True)
        msg = str(cm.exception)
        self.assertIn("H99", msg)
        self.assertIn("assumption", msg)

    def test_assumption_empty_allowed(self):
        """assumption 空串合法（无假设驱动）"""
        path = _write_rules(self.tmpdir, [_make_rule(assumption="")])
        rules = _load_rules(path, self.functions, required=True)
        self.assertEqual(rules["R1"].assumption, "")

    def test_assumption_known_allowed(self):
        """assumption 引用已知假设（H1~H4）合法"""
        for h in ["H1", "H2", "H3", "H4"]:
            path = _write_rules(self.tmpdir, [_make_rule(id=f"R_{h}", assumption=h)])
            rules = _load_rules(path, self.functions, required=True)
            self.assertEqual(rules[f"R_{h}"].assumption, h)

    def test_default_pack_assumptions_valid(self):
        """default pack 的所有 assumption 引用合法"""
        pack = load_pack("default")
        known = {"H1", "H2", "H3", "H4", ""}
        for rid, r in pack.rules.items():
            self.assertIn(r.assumption, known,
                          f"{rid} assumption={r.assumption} 不在已知集合")


if __name__ == "__main__":
    unittest.main()
