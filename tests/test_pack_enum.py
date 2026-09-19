"""
tests/test_pack_enum.py
P3 镜头包自枚举测试（研判能力插件化 v3 §3.6）。

覆盖：
  ① packs/*/pack.json 放入即挂载（impl:func 按路径加载、注册期引用校验）；
  ② 拔出目录重新枚举即消失（对账注销），内置技能不受影响；
  ③ 坏包（引用不存在的底座类型 / draft 缺外设声明）只拒该包、不连坐；
  ④ 重复枚举幂等；packs 目录不存在不报错；
  ⑤ 经包挂载的镜头可被 skill_invoke 正常调度，scope.reads 生效；
  ⑥ registry_bootstrap 内置 5 技能在新契约下零改动注册通过。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.pack_loader import discover
from core.registry import (
    LineageClue,
    SkillRegistry,
    SkillSpec,
    skill_invoke,
)

_IMPL_SOURCE = '''
from core.registry import LineageClue


def circle(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(skill_id="rel_circle", title="关系圈层")]
'''

_GOOD_PACK = {
    "pack_id": "relation_demo",
    "version": "0.1.0",
    "skills": [{
        "skill_id": "rel_circle",
        "name": "关系圈层镜头",
        "stage": "用间",
        "consumes_objects": ["org", "person"],
        "produces_dims": ["关系"],
        "params_schema": {"depth": {"type": "integer"}},
        "scope": {"reads": ["org", "person"]},
        "handler": "impl:circle",
    }],
}

_GHOST_PACK = {
    # consumes_objects 引用不存在的底座类型 → 注册期硬失败，整包拒载
    "pack_id": "ghost_pack",
    "skills": [{
        "skill_id": "ghost_lens",
        "name": "幽灵镜头",
        "stage": "用间",
        "consumes_objects": ["ghost_xyz"],
    }],
}

_BAD_DRAFT_PACK = {
    # mode=draft 但缺 external_services/timeout_ms/result_ttl_s → 拒载
    "pack_id": "bad_draft_pack",
    "skills": [{
        "skill_id": "bad_draft_lens",
        "name": "残缺草案镜头",
        "stage": "用间",
        "mode": "draft",
        "consumes_objects": ["org"],
        "handler": "impl:circle",
    }],
}


def _marker_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(skill_id="builtin_marker", title="内置")]


def _write_pack(base: Path, name: str, decl: dict,
                impl: str | None = _IMPL_SOURCE) -> Path:
    pdir = base / name
    pdir.mkdir(parents=True)
    (pdir / "pack.json").write_text(
        json.dumps(decl, ensure_ascii=False), encoding="utf-8")
    if impl is not None:
        (pdir / "impl.py").write_text(impl, encoding="utf-8")
    return pdir


class PackEnumTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.packs_dir = self.tmp / "packs"
        self.packs_dir.mkdir()
        self.reg = SkillRegistry()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discover_mounts_pack(self):
        _write_pack(self.packs_dir, "relation_demo", _GOOD_PACK)
        report = discover(self.reg, packs_dir=self.packs_dir)
        self.assertIn("relation_demo", report["loaded"])
        self.assertEqual(report["failed"], [])
        self.assertIn("rel_circle", self.reg)
        spec = self.reg.skill("rel_circle")
        self.assertEqual(spec.consumes_objects, ["org", "person"])
        self.assertEqual(spec.scope_reads, ["org", "person"])
        self.assertEqual(spec.produces_dims, ["关系"])
        self.assertTrue(callable(spec.handler))
        self.assertEqual(spec.pack_id, "relation_demo")

    def test_mounted_skill_invocable(self):
        _write_pack(self.packs_dir, "relation_demo", _GOOD_PACK)
        discover(self.reg, packs_dir=self.packs_dir)
        clues = skill_invoke(self.reg, "rel_circle")
        self.assertEqual(len(clues), 1)
        self.assertEqual(clues[0].title, "关系圈层")

    def test_bad_packs_not_connected(self):
        _write_pack(self.packs_dir, "relation_demo", _GOOD_PACK)
        _write_pack(self.packs_dir, "ghost_pack", _GHOST_PACK, impl=None)
        _write_pack(self.packs_dir, "bad_draft_pack", _BAD_DRAFT_PACK)
        report = discover(self.reg, packs_dir=self.packs_dir)
        # 好包照常挂载
        self.assertIn("relation_demo", report["loaded"])
        self.assertIn("rel_circle", self.reg)
        # 两个坏包各自进 failed，原因可辨
        failed_packs = {Path(f["path"]).parent.name for f in report["failed"]}
        self.assertEqual(failed_packs, {"ghost_pack", "bad_draft_pack"})
        self.assertNotIn("ghost_lens", self.reg)
        self.assertNotIn("bad_draft_lens", self.reg)

    def test_rescan_idempotent(self):
        _write_pack(self.packs_dir, "relation_demo", _GOOD_PACK)
        discover(self.reg, packs_dir=self.packs_dir)
        report2 = discover(self.reg, packs_dir=self.packs_dir)
        self.assertIn("relation_demo", report2["loaded"])
        self.assertEqual(report2["removed"], [])
        self.assertEqual(len([s for s in self.reg.all_specs()
                              if s.skill_id == "rel_circle"]), 1)

    def test_pack_removal_unmounts_builtin_kept(self):
        self.reg.register(SkillSpec(
            skill_id="builtin_marker", name="内置标记", stage="庙算",
            handler=_marker_handler))
        pdir = _write_pack(self.packs_dir, "relation_demo", _GOOD_PACK)
        discover(self.reg, packs_dir=self.packs_dir)
        self.assertIn("rel_circle", self.reg)
        # 拔出包目录 → 重新枚举后技能消失，内置保留
        shutil.rmtree(pdir)
        report = discover(self.reg, packs_dir=self.packs_dir)
        self.assertIn("rel_circle", report["removed"])
        self.assertNotIn("rel_circle", self.reg)
        self.assertIn("builtin_marker", self.reg)

    def test_missing_packs_dir_is_noop(self):
        report = discover(self.reg, packs_dir=self.tmp / "nonexistent")
        self.assertEqual(report["loaded"], [])
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["removed"], [])

    def test_builtin_bootstrap_validates_under_new_contract(self):
        from skills.registry_bootstrap import register_all
        reg = register_all(SkillRegistry())
        specs = reg.all_specs()
        self.assertEqual(len(specs), 5)
        for s in specs:
            self.assertEqual(s.mode, "deterministic")
            self.assertTrue(s.enabled)
            self.assertEqual(s.external_services, [])
            self.assertEqual(s.timeout_ms, 0)
            self.assertEqual(s.result_ttl_s, 0)
            self.assertEqual(s.params_schema, {})
            self.assertTrue(callable(s.handler))


if __name__ == "__main__":
    unittest.main()
