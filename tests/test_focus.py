"""core/focus.py 靶心推导器测试：自动填参不得靠人、不得全组合、不得出定性。"""
import unittest

from core import Store
from core.focus import (
    FocusSubject, auto_fill_params, resolve_focus, resolve_projects,
)
from core.pack_loader import discover
from core.registry import get_registry


def _store() -> Store:
    return Store()


class TestResolveFocus(unittest.TestCase):
    """靶心推导：四级源优先级 + 真实类型探测。"""

    def test_case_aliases_win_over_row_count(self):
        """案件登记主体优先于「哪行数据多」——否则靶心被高频噪声带偏。"""
        s = _store()
        focus = resolve_focus(s, {}, "default")
        self.assertTrue(focus, msg="应能推导出靶心")
        self.assertEqual(focus[0].source, "case_aliases")
        self.assertEqual(focus[0].name, "张卫国")

    def test_type_probed_not_auto(self):
        """同名多类实体须探测出真实类型，否则 resolve_subject 抛歧义 ValueError。"""
        s = _store()
        focus = resolve_focus(s, {}, "default")
        by_name = {f.name: f.type for f in focus}
        self.assertEqual(by_name.get("张卫国"), "person",
                         msg=f"张卫国 应探测为 person，实得 {by_name}")

    def test_projects_resolved(self):
        s = _store()
        projs = resolve_projects(s, "default")
        self.assertTrue(projs, msg="应能推导出项目锚点")
        self.assertEqual(projs[0].type, "bid_project")

    def test_deterministic(self):
        """同输入同输出（确定性红线）。"""
        s = _store()
        a = [(f.name, f.type) for f in resolve_focus(s, {}, "default")]
        b = [(f.name, f.type) for f in resolve_focus(s, {}, "default")]
        self.assertEqual(a, b)


class TestAutoFillParams(unittest.TestCase):
    """自动填参：定向镜头不再需要人填，双主体不做全组合。"""

    @classmethod
    def setUpClass(cls):
        discover()

    def _spec(self, skill_id: str):
        return get_registry().skill(skill_id)

    def test_directed_lenses_all_filled(self):
        """六个定向镜头全部能自动填出参数（此前会被批量阶段整体跳过）。"""
        s = _store()
        for sid in ("relation_neighborhood", "relation_common_neighbors",
                    "relation_paths", "timeline_sequence", "timeline_rhythm",
                    "timeline_cross_collision"):
            combos, sources = auto_fill_params(self._spec(sid), s, {}, "default")
            self.assertTrue(combos, msg=f"{sid} 应能自动填参，实得空")
            self.assertTrue(all(sources), msg=f"{sid} 缺溯源标记")

    def test_dual_subject_not_full_combo(self):
        """双主体走「靶心×关联」O(N)，不是全组合 O(N²)。"""
        s = _store()
        focus = resolve_focus(s, {}, "default")
        combos, _ = auto_fill_params(self._spec("relation_paths"), s, {}, "default")
        # 全组合 = C(N,2)；靶心×关联 = N-1。N>=2 时后者恒小
        n = len(focus)
        self.assertLessEqual(len(combos), max(n - 1, 1),
                             msg=f"组合数 {len(combos)} 超出 O(N) 上界（N={n}）")
        heads = {c["subject_a"] for c in combos}
        self.assertEqual(len(heads), 1, msg="双主体应以同一靶心为轴，不换轴")

    def test_dual_subject_carries_disambig_types(self):
        """双主体须带分体类型，否则同名歧义会让镜头被隔离成 0 线索。"""
        s = _store()
        combos, _ = auto_fill_params(self._spec("relation_paths"), s, {}, "default")
        for c in combos:
            self.assertIn("target_type_a", c, msg="缺 target_type_a（同名消歧）")
            self.assertIn("target_type_b", c, msg="缺 target_type_b（同名消歧）")

    def test_param_source_recorded(self):
        """每次自动填充都要有溯源标记，进线索 detail 可审计。"""
        s = _store()
        combos, sources = auto_fill_params(
            self._spec("timeline_sequence"), s, {}, "default")
        for c, src in zip(combos, sources):
            self.assertTrue(c.get("_param_source"), msg="缺 _param_source")
            self.assertTrue(src.startswith("focus:"), msg=f"溯源格式异常：{src}")

    def test_no_undeclared_params(self):
        """自动填出的参数必须在 params_schema 声明内（否则 _validate_params 拒）。"""
        s = _store()
        for sid in ("relation_neighborhood", "relation_paths",
                    "timeline_cross_collision"):
            spec = self._spec(sid)
            combos, _ = auto_fill_params(spec, s, {}, "default")
            for c in combos:
                for k in c:
                    if k == "_param_source":
                        continue
                    self.assertIn(k, spec.params_schema,
                                  msg=f"{sid} 填出未声明参数 {k!r}")


class TestOntologyAgnostic(unittest.TestCase):
    """本体无关（方案 B）：枚举源/类型探测不得硬编码 person/org/bid_project。

    此前 _SEMANTIC_SOURCES 硬编码三元组，换本体（如金融领域 fund/manager/
    listing）后靶心推导完全失效——实测返回 default 包残留人名、项目候选为空、
    基金名被形态判定误判为 person。现在一律读 ontology objects.json。
    """

    def test_sources_from_ontology_not_hardcoded(self):
        """枚举源来自本体 kind=entity 声明，且排除系统/运行期对象。"""
        from core.focus import _ontology_entity_sources
        src = _ontology_entity_sources("default")
        types = {t for _, t, _ in src}
        self.assertTrue(types, msg="枚举源不应为空")
        # 系统对象（线索/裁决/图像证据）不是侦查对象，不得进「查谁」候选
        for sys_obj in ("clue", "decision", "image_evidence"):
            self.assertNotIn(sys_obj, types,
                             msg=f"系统对象 {sys_obj} 不应作为靶心候选")
        # 覆盖应等于本体声明的实体型，而非硬编码的 3 个
        self.assertGreater(len(types), 3,
                           msg=f"枚举源仅 {sorted(types)}，疑似仍走硬编码")

    def test_declared_types_fall_back_when_absent(self):
        """镜头包声明的类型在本体不存在 → 回落全量实体型（不静默空集）。

        保护换本体场景：镜头包写死 person/org/account，新本体没有这些类型时，
        若返回空集则定向镜头全部无法填参（静默失效）。
        """
        from core.focus import _sources_for
        got = _sources_for("default", ["__不存在的类型__"])
        self.assertTrue(got, msg="声明类型不存在时应回落，不得返回空集")

    def test_declared_types_filter_when_present(self):
        """声明的类型存在时按声明收窄（方案 B 的声明优先）。"""
        from core.focus import _sources_for
        got = _sources_for("default", ["person", "org"])
        self.assertEqual({t for _, t, _ in got}, {"person", "org"})

    def test_probe_type_not_shape_only(self):
        """类型探测先在语义层实查，形态判定只是兜底。

        改造前「华夏成长」这类机构名在金融本体下会被形态判定误判为 person，
        因为压根没查过 obj_fund。
        """
        s = _store()
        from core.focus import _probe_type
        # 张卫国 在 default 语义层的 obj_person 里 → 应探测为 person（非 auto）
        self.assertEqual(_probe_type(s, "张卫国", "default"), "person")
        # 查无此人 → 回落形态判定（不抛异常、不崩）
        self.assertIn(_probe_type(s, "查无此人某某", "default"),
                      ("person", "org", "auto"))


class TestBatchLensTasks(unittest.TestCase):
    """调度层：定向镜头进入可调度任务，不再被整体跳过。"""

    @classmethod
    def setUpClass(cls):
        discover()

    def test_directed_lenses_schedulable(self):
        from core.pack_loader import batch_lens_tasks
        s = _store()
        tasks, unresolved, skipped = batch_lens_tasks(
            get_registry(), store=s, ctx={})
        sids = {sid for sid, _ in tasks}
        for sid in ("relation_neighborhood", "timeline_sequence",
                    "timeline_cross_collision"):
            self.assertIn(sid, sids, msg=f"{sid} 未进入可调度任务")
        self.assertEqual(unresolved, [], msg=f"存在无法调度的镜头：{unresolved}")
        self.assertIn("vlm_inspect", skipped, msg="draft 镜头不应进批量")


if __name__ == "__main__":
    unittest.main(verbosity=2)
