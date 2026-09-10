"""
tests/test_states_decl.py
R6: states.json 声明化（领域模型解耦）

验证用例：
  DM-TC-01: 装载 states.json 缺 name/重复/迁移目标未声明全部硬失败
  DM-TC-03: actions.json 引用未声明状态 → 硬失败
  DM-TC-05: 受控终态 requires_role=human → AI/机器会话无权置位
  DM-TC-08: 现有处置流程行为完全一致
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.ontology_loader import load_states, invalidate_pack_cache, load_pack
from core.registry import ClueStatus, ClueStatusMachine


def _write_states(path: Path, states, transitions) -> None:
    path.write_text(json.dumps({
        "schema_version": 2, "states": states, "transitions": transitions,
    }, ensure_ascii=False), encoding="utf-8")


class StatesDeclTests(unittest.TestCase):
    """R6: states.json 声明校验。"""

    def test_default_pack_loads(self):
        """default 包 states.json 装载成功，返回 5 个状态。"""
        decl = load_states("default")
        self.assertEqual(len(decl["states"]), 5)
        names = [s["name"] for s in decl["states"]]
        self.assertIn("已立案", names)

    def test_dm_tc_01_missing_name_fails(self):
        """DM-TC-01: states.json 缺 name → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_states(pack_dir / "states.json",
                          [{"label": "待查"}], [])
            with self.assertRaises(ValueError):
                load_states("testpack", base_dir=Path(td))

    def test_dm_tc_01_duplicate_name_fails(self):
        """DM-TC-01: 状态名重复 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_states(pack_dir / "states.json",
                          [{"name": "待查"}, {"name": "待查"}], [])
            with self.assertRaises(ValueError):
                load_states("testpack", base_dir=Path(td))

    def test_dm_tc_01_undeclared_transition_target_fails(self):
        """DM-TC-01: 迁移目标未声明 → 硬失败。"""
        with tempfile.TemporaryDirectory() as td:
            pack_dir = Path(td) / "testpack"
            pack_dir.mkdir()
            _write_states(pack_dir / "states.json",
                          [{"name": "待查"}],
                          [{"from": "待查", "to": ["未声明状态"]}])
            with self.assertRaises(ValueError):
                load_states("testpack", base_dir=Path(td))

    def test_dm_tc_05_human_only_state(self):
        """DM-TC-05: 已立案 requires_role=human，在 human_only_states 集合中。"""
        human_only = ClueStatus.human_only_states("default")
        self.assertIn("已立案", human_only)
        # 待查/查证中等不是 human-only
        self.assertNotIn("待查", human_only)
        self.assertNotIn("查证中", human_only)

    def test_dm_tc_05_terminal_state(self):
        """已立案是终态（terminal=True）。"""
        terminals = ClueStatus.terminal_states("default")
        self.assertIn("已立案", terminals)

    def test_dm_tc_08_default_transitions_match_hardcoded(self):
        """DM-TC-08: default 包迁移表与硬编码默认一致。"""
        trans = ClueStatusMachine.transitions_for("default")
        self.assertEqual(trans["待查"], {"查证中", "已排除", "已固证"})
        self.assertEqual(trans["已立案"], set())
        self.assertIn("已立案", trans["已固证"])

    def test_default_actions_target_status_valid(self):
        """default 包所有 actions 的 target_status 都在 states.json 声明内。"""
        invalidate_pack_cache()
        pack = load_pack("default")
        state_names = {s["name"] for s in load_states("default")["states"]}
        for a in pack.actions.values():
            self.assertIn(a.target_status, state_names,
                          f"action {a.name} target_status={a.target_status} 未声明")


if __name__ == "__main__":
    unittest.main()
