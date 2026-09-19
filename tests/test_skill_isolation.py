"""
tests/test_skill_isolation.py
P3 镜头契约、失败隔离与调度短路测试（研判能力插件化 v3 §3.3/§3.4）。

覆盖：
  ① handler 运行期异常被隔离：返回 [] + ctx["degraded"] + skill_failed 诊断；
  ② 单镜头故障不影响其余镜头；
  ③ enabled=false 调度短路（skill_disabled 留痕）；
  ④ 注册期/契约硬失败不被 try 吞掉（未注册/无 handler/非法规格）；
  ⑤ params 与 params_schema 双向核对；
  ⑥ LineageClue.evidence_refs 引用校验（结构/语义表/引用行）与旧记录兼容；
  ⑦ scoped_rows 作用域 fail-closed。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store
from core.access import AccessContext
from core.registry import (
    LineageClue,
    SkillRegistry,
    SkillSpec,
    scoped_rows,
    skill_invoke,
)
from core.run_health import RunHealth


# ----------------------------------------------------------------------
# 测试镜头 handler
# ----------------------------------------------------------------------

def _ok_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(skill_id="ok_lens", title="正常镜头")]


def _boom_handler(miao=None, store=None, ctx=None, params=None, health=None):
    raise RuntimeError("镜头运行期数据异常")


def _param_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(skill_id="param_lens", title="参数镜头",
                        detail={"params": dict(params or {})})]


def _ev_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(
        skill_id="ev_lens", title="证据镜头",
        evidence_refs=[
            {"kind": "node", "ref": "obj_demo#k1"},
            {"kind": "aggregate", "metric": "total", "value": 10},
            {"kind": "file", "file_uri": "file://evidence/a.pdf"},
        ])]


def _ev_dangling_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(
        skill_id="ev_bad", title="悬空证据",
        evidence_refs=[{"kind": "edge", "ref": "obj_demo#missing_key"}])]


def _ev_bad_table_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(
        skill_id="ev_bad2", title="幽灵表",
        evidence_refs=[{"kind": "node", "ref": "obj_ghost#k1"}])]


def _edge_ev_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(
        skill_id="edge_ev", title="边表证据",
        evidence_refs=[{
            "kind": "edge", "ref": "lnk_demo_edge#c001",
            "key_column": "call_id"}])]


def _edge_ev_no_keycol_handler(miao=None, store=None, ctx=None, params=None, health=None):
    return [LineageClue(
        skill_id="edge_ev2", title="边表证据无行键列",
        evidence_refs=[{"kind": "edge", "ref": "lnk_demo_edge#c001"}])]


class SkillIsolationTests(unittest.TestCase):

    def setUp(self):
        self.reg = SkillRegistry()
        self.store = Store(db_path=":memory:")
        self.store.conn.execute(
            'CREATE TABLE obj_demo ("pk" VARCHAR, label VARCHAR)')
        self.store.conn.executemany(
            'INSERT INTO obj_demo VALUES (?, ?)',
            [("k1", "甲"), ("k2", "乙")])
        self.store.conn.execute(
            "CREATE TABLE lnk_demo_edge "
            "(call_id VARCHAR, from_person VARCHAR, to_person VARCHAR)")
        self.store.conn.execute(
            "INSERT INTO lnk_demo_edge VALUES ('c001', 'p1', 'p2')")
        self.reg.register(SkillSpec(
            skill_id="ok_lens", name="正常镜头", stage="庙算",
            handler=_ok_handler))
        self.reg.register(SkillSpec(
            skill_id="boom_lens", name="爆炸镜头", stage="庙算",
            handler=_boom_handler))
        self.reg.register(SkillSpec(
            skill_id="param_lens", name="参数镜头", stage="庙算",
            params_schema={
                "depth": {"type": "integer", "required": True},
                "label": {"type": "string"},
            },
            handler=_param_handler))
        self.reg.register(SkillSpec(
            skill_id="ev_lens", name="证据镜头", stage="庙算",
            handler=_ev_handler))
        self.reg.register(SkillSpec(
            skill_id="ev_bad", name="悬空证据镜头", stage="庙算",
            handler=_ev_dangling_handler))
        self.reg.register(SkillSpec(
            skill_id="ev_bad2", name="幽灵表镜头", stage="庙算",
            handler=_ev_bad_table_handler))
        self.reg.register(SkillSpec(
            skill_id="edge_ev", name="边表证据镜头", stage="庙算",
            handler=_edge_ev_handler))
        self.reg.register(SkillSpec(
            skill_id="edge_ev2", name="边表证据无行键镜头", stage="庙算",
            handler=_edge_ev_no_keycol_handler))

    def tearDown(self):
        self.store.close()

    # ---- ① 失败隔离 ----

    def test_runtime_exception_isolated(self):
        ctx: dict = {}
        health = RunHealth(self.store)
        clues = skill_invoke(self.reg, "boom_lens", ctx=ctx, health=health)
        self.assertEqual(clues, [])
        self.assertEqual(len(ctx.get("degraded", [])), 1)
        self.assertEqual(ctx["degraded"][0]["skill_id"], "boom_lens")
        self.assertIn("RuntimeError", ctx["degraded"][0]["error_type"])
        rows = health.rows()
        failed = [r for r in rows if r["kind"] == "skill_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["severity"], "warning")
        self.assertEqual(failed[0]["source"], "boom_lens")

    def test_failed_lens_does_not_affect_others(self):
        ctx: dict = {}
        skill_invoke(self.reg, "boom_lens", ctx=ctx)
        clues = skill_invoke(self.reg, "ok_lens", ctx=ctx)
        self.assertEqual(len(clues), 1)
        self.assertEqual(clues[0].skill_id, "ok_lens")

    # ---- ③ enabled 短路 ----

    def test_disabled_short_circuit(self):
        self.reg.register(SkillSpec(
            skill_id="off_lens", name="停用镜头", stage="庙算",
            enabled=False, handler=_boom_handler))  # 即便 handler 会炸也不应触达
        health = RunHealth(self.store)
        clues = skill_invoke(self.reg, "off_lens", health=health)
        self.assertEqual(clues, [])
        kinds = [r["kind"] for r in health.rows()]
        self.assertIn("skill_disabled", kinds)
        self.assertNotIn("skill_failed", kinds)

    # ---- ④ 硬失败不被吞 ----

    def test_unregistered_skill_raises(self):
        with self.assertRaises(KeyError):
            skill_invoke(self.reg, "nobody_home")

    def test_missing_handler_raises(self):
        self.reg.register(SkillSpec(
            skill_id="nohandler", name="空壳", stage="庙算", handler=None))
        with self.assertRaises(RuntimeError):
            skill_invoke(self.reg, "nohandler")

    def test_invalid_spec_rejected_at_registration(self):
        with self.assertRaises(ValueError):
            self.reg.register(SkillSpec(
                skill_id="bad_draft", name="缺外设声明", stage="庙算",
                mode="draft", handler=_ok_handler))
        with self.assertRaises(ValueError):
            self.reg.register(SkillSpec(
                skill_id="bad_det", name="确定性镜头带外设", stage="庙算",
                external_services=["vlm:x"], timeout_ms=1, result_ttl_s=1,
                handler=_ok_handler))
        with self.assertRaises(ValueError):
            self.reg.register(SkillSpec(
                skill_id="bad_mode", name="非法模式", stage="庙算",
                mode="magic", handler=_ok_handler))
        with self.assertRaises(ValueError):
            self.reg.register(SkillSpec(
                skill_id="bad_param", name="非法参数类型", stage="庙算",
                params_schema={"x": {"type": "float"}}, handler=_ok_handler))

    def test_draft_spec_with_peripherals_ok(self):
        spec = self.reg.register(SkillSpec(
            skill_id="draft_ok", name="合法草案", stage="庙算",
            mode="draft", external_services=["vlm:qwen-vl"],
            timeout_ms=3000, result_ttl_s=3600, handler=_ok_handler))
        self.assertEqual(spec.mode, "draft")

    # ---- ⑤ 参数核对 ----

    def test_undeclared_param_rejected(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "param_lens", params={"bogus": 1})

    def test_required_param_missing_rejected(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "param_lens", params={"label": "x"})

    def test_declared_params_pass(self):
        clues = skill_invoke(self.reg, "param_lens",
                             params={"depth": 3, "label": "x"})
        self.assertEqual(len(clues), 1)
        self.assertEqual(clues[0].detail["params"]["depth"], 3)

    # ---- ⑥ evidence_refs 契约 ----

    def test_evidence_refs_validated_ok(self):
        clues = skill_invoke(self.reg, "ev_lens", store=self.store)
        self.assertEqual(len(clues), 1)
        # L1 特征落盘后可反序列化（get_feature 返回 {"value","version"} 包装）
        raw = self.store.get_feature(f"_clue:{clues[0].clue_id}", "lineage")
        rebuilt = LineageClue.from_dict(raw["value"])
        self.assertEqual(len(rebuilt.evidence_refs), 3)

    def test_evidence_refs_dangling_row_hard_fails(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "ev_bad", store=self.store)

    def test_evidence_refs_missing_table_hard_fails(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "ev_bad2", store=self.store)

    def test_edge_evidence_with_key_column_ok(self):
        clues = skill_invoke(self.reg, "edge_ev", store=self.store)
        self.assertEqual(len(clues), 1)
        raw = self.store.get_feature(f"_clue:{clues[0].clue_id}", "lineage")
        self.assertEqual(raw["value"]["evidence_refs"][0]["key_column"],
                         "call_id")

    def test_edge_evidence_missing_key_column_hard_fails(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "edge_ev2", store=self.store)

    def test_evidence_refs_requires_store(self):
        with self.assertRaises(ValueError):
            skill_invoke(self.reg, "ev_lens", store=None)

    def test_from_dict_legacy_record(self):
        d = LineageClue(skill_id="legacy", title="旧记录").to_dict()
        del d["evidence_refs"]
        rebuilt = LineageClue.from_dict(d)
        self.assertEqual(rebuilt.evidence_refs, [])
        self.assertEqual(rebuilt.skill_id, "legacy")

    # ---- ⑦ 作用域读通道 ----

    def test_scoped_rows_fail_closed(self):
        spec = SkillSpec(skill_id="scope_lens", name="作用域镜头",
                         stage="庙算", scope_reads=["demo"],
                         handler=_ok_handler)
        access = AccessContext(operator="tester", role="system")
        rows = scoped_rows(spec, access, self.store, "demo")
        self.assertEqual(len(rows), 2)
        with self.assertRaises(PermissionError):
            scoped_rows(spec, access, self.store, "person")
        # 零作用域规格读什么都拒（缺省最小权限）
        zero = SkillSpec(skill_id="zero_lens", name="零权镜头",
                         stage="庙算", handler=_ok_handler)
        with self.assertRaises(PermissionError):
            scoped_rows(zero, access, self.store, "demo")


if __name__ == "__main__":
    unittest.main()
