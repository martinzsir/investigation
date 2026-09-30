"""
tests/test_verify_suggest.py
REQ-V-018 核查手册建议项（verify_playbooks.json → loader 硬失败 → 确定性渲染
→ 采纳/忽略生命周期 → 采纳后路由 → agent 拒绝）。

断言（实施方案 §3.8 REQ-V-018 AC1~AC7 + 计划分步实施计划 步骤 6）：
  AC1  R6 合并线索（clue_9446b1bd）→ 3 条建议（function×2 + external×1），
       r6_fund_tw_rerun 文本含「张卫国」，{project_count} 按 D1 裁决值锁 8，
       item_id 稳定（vi_ 前缀，二次供给不变）；
  AC2  采纳 → 待核查（origin 保持 suggested、text 保留），忽略 → 已忽略，
       重采纳；verify_progress：建议/已忽略独立计数、不进 pending（门禁不拦截）；
  AC3  建议经状态机：非法迁移（建议→已证实）→ VERIFY_REJECTED；
  AC4  agent 会话采纳/忽略 → VERIFY_FORBIDDEN；human 正常；
  AC5  装载校验：未知 rule_id / 未知 function / 槽位越界 / id 重复 /
       schema_version 错 → load_verify_playbooks 硬失败（各一例）；
       文件缺失 → [] 旧包零破坏；手册内 playbook 与所引规则/函数一一对齐；
  AC6  采纳后路由按钮 → 前端 spec（verify-workbench.spec.ts）；
  AC7  事实卡永不生成建议项（渲染只消费 rule 关联信息 + source_rows + h 卡）。

用例不依赖任何案件运行态产物（cases/ 不入 Git，见 .gitignore）：线索 raw 一律
自建 inline fixture，本体/手册一律走内核包（base_dir=None）。
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

from core.access import AccessContext
from core.ontology_loader import load_verify_playbooks

from server.app.clues_view import assemble_detail
from server.app.meta.models import CASE_ACTIVE, CaseRecord
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore
from server.app.evidence_builder import build_evidence
from server.app.verify_provision import (
    backfill_rule_fields,
    render_suggested,
)
from server.app.worker.tasks import (
    TASK_BUILD,
    TASK_VERIFY,
    TaskExecError,
    enqueue_task,
    handle_build,
)
from server.app.worker.verify import handle_verify

CLUE = "clue_9446b1bd"
PACK_ROOT = ROOT / "ontology"


def _write_playbook_pack(base: Path, *, playbooks: list | None = None,
                         rules: bool = True, functions: bool = True) -> Path:
    """构造最小 playbook 校验环境：rules.json + functions.json + 手册（raw 解析
    只做名字存在性校验，无需完整 objects/bindings 包）。"""
    root = base / "default"
    root.mkdir(parents=True, exist_ok=True)
    if rules:
        (root / "rules.json").write_text(
            json.dumps({"rules": [{"id": "R6"}]}, ensure_ascii=False),
            encoding="utf-8")
    if functions:
        (root / "functions.json").write_text(
            json.dumps({"functions": [
                {"name": "time_window_collision"},
                {"name": "call_frequency_spike"},
            ]}, ensure_ascii=False), encoding="utf-8")
    if playbooks is not None:
        (root / "verify_playbooks.json").write_text(
            json.dumps({"schema_version": 1, "playbooks": playbooks},
                       ensure_ascii=False), encoding="utf-8")
    return base


def _synthetic_r6_raw(clue_id: str = "clue_t6", *, rows=None,
                      rule_id: str = "R6", assumption=None) -> dict:
    """带 rule_id=R6 的合成线索（正常回填直通路径）。

    行集用语义层真实列名 owner_raw（rules.json R6 subject_column）+ amount，
    与 _subject_stats 的过滤口径同源。
    """
    return {
        "clue_id": clue_id,
        "skill_id": "time_window",
        "title": "张卫国 · 中标时间窗整数资金",
        "detail": {"rule_id": rule_id,
                   "rule_text": "中标公示 ±20 天整数资金，主体为个人。",
                   "级别": "待核实"},
        "source_rows": rows if rows is not None else [
            {"owner_raw": "张卫国", "amount": 100000},
            {"owner_raw": "张卫国", "amount": 200000},
        ],
        "assumption_chain": assumption or [],
        "status": "待查",
        "audit_log": [],
    }


def _merged_r6_raw(clue_id: str = CLUE) -> dict:
    """R6 合并线索：detail 只有 merged_from、无 rule_id（复刻 core/lineage
    合并段丢规则字段的真实产物形态），靠合并标题里的部件标题反查 rules.json 回填。

    8 行张卫国整数资金 → 手册建议槽位 {subject}=张卫国、{project_count}=8
    （D1 裁决口径）。
    """
    return {
        "clue_id": clue_id,
        "skill_id": "time_window",
        "title": "张卫国 · 中标-资金时间窗碰撞 | 某项目 · 中标-资金时间窗碰撞",
        "detail": {"merged_from": ["clue_9f21a0", "clue_3b7c44"],
                   "级别": "待核实"},
        "source_rows": [{"owner_raw": "张卫国",
                         "amount": 100000 * (i + 1)} for i in range(8)],
        "assumption_chain": ["H4"],
        "jian_types": ["反间"],
        "needs_human_review": True,
        "status": "待查",
        "audit_log": [],
        "note": "",
    }


# ----------------------------------------------------------------------
# AC5：装载校验（loader 硬失败 / 缺失回落 / 两包一致）
# ----------------------------------------------------------------------
class VerifyPlaybookLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 缺失回落 []：旧案件包/精简包零破坏 ----
    def test_missing_file_returns_empty(self):
        base = _write_playbook_pack(self.tmp / "a", playbooks=None)
        self.assertEqual(load_verify_playbooks("default", base_dir=base), [])

    # ---- 正常装载：归一化字段 + assumption 单值/数组归一 ----
    def test_valid_pack_loads_normalized(self):
        base = _write_playbook_pack(self.tmp / "b", playbooks=[
            {"id": "p1", "channel": "function",
             "match": {"rule_id": "R6", "assumption": "H1"},
             "function": "time_window_collision",
             "text": "核查 {subject}（{project_count}）"},
            {"id": "p2", "channel": "external",
             "match": {"rule_id": "R6", "assumption": ["H1", "H4"]},
             "external": {"target": "住建局招标办", "material": "底档"},
             "text": "调取 {subject} 底档"},
        ])
        pbs = load_verify_playbooks("default", base_dir=base)
        self.assertEqual([p["id"] for p in pbs], ["p1", "p2"])
        self.assertEqual(pbs[0]["assumptions"], ["H1"])
        self.assertEqual(pbs[1]["assumptions"], ["H1", "H4"])
        self.assertEqual(pbs[0]["fallback_function"], "")
        self.assertEqual(pbs[1]["external"]["target"], "住建局招标办")

    # ---- AC5：五类坏手册各一例 → 硬失败 ----
    def _assert_bad(self, playbooks, frag):
        base = _write_playbook_pack(self.tmp / "bad", playbooks=playbooks)
        with self.assertRaises(ValueError) as cm:
            load_verify_playbooks("default", base_dir=base)
        self.assertIn(frag, str(cm.exception))

    def test_bad_unknown_rule_id(self):
        self._assert_bad(
            [{"id": "x", "channel": "external", "match": {"rule_id": "R99"},
              "external": {"target": "t", "material": "m"},
              "text": "核查 {subject}"}],
            "match.rule_id 未在 rules.json 声明")

    def test_bad_unknown_function(self):
        self._assert_bad(
            [{"id": "x", "channel": "function",
              "match": {"rule_id": "R6"}, "function": "no_such_fn",
              "text": "核查 {subject}"}],
            "function 未在 functions.json 声明")

    def test_bad_slot_overflow(self):
        self._assert_bad(
            [{"id": "x", "channel": "external", "match": {"rule_id": "R6"},
              "external": {"target": "t", "material": "m"},
              "text": "核查 {subject} 与 {evil_slot}"}],
            "text 槽位越界")

    def test_bad_duplicate_id(self):
        pb = {"id": "dup", "channel": "external",
              "match": {"rule_id": "R6"},
              "external": {"target": "t", "material": "m"},
              "text": "核查"}
        self._assert_bad([pb, dict(pb)], "playbook id 重复")

    def test_bad_schema_version(self):
        base = _write_playbook_pack(self.tmp / "ver", playbooks=[])
        p = base / "default" / "verify_playbooks.json"
        p.write_text(json.dumps({"schema_version": 2, "playbooks": []},
                                ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_verify_playbooks("default", base_dir=base)
        self.assertIn("schema_version", str(cm.exception))

    def test_bad_assumption_shape(self):
        self._assert_bad(
            [{"id": "x", "channel": "function",
              "match": {"rule_id": "R6", "assumption": "假设甲"},
              "function": "time_window_collision",
              "text": "核查 {subject}"}],
            "match.assumption")

    def test_bad_external_missing_material(self):
        self._assert_bad(
            [{"id": "x", "channel": "external", "match": {"rule_id": "R6"},
              "external": {"target": "住建局招标办"},
              "text": "核查 {subject}"}],
            "external.material")

    # ---- 手册与本体同源：每条 playbook 的 rule_id / function 均已声明 ----
    def test_playbooks_align_with_ontology(self):
        """手册条目所引规则/函数必须在内核本体里存在（loader 已硬失败，此处锁
        「手册不孤儿」：每条 rule_id 都能在 rules.json 找到、每个 function
        都能在 functions.json 找到，且 external 条目不带 function）。"""
        from core.ontology_loader import load_pack
        spec = load_pack("default")
        declared_fns = {f["name"] for f in
                        json.loads((PACK_ROOT / "default" / "functions.json")
                                   .read_text(encoding="utf-8"))["functions"]}
        pbs = load_verify_playbooks("default")
        for pb in pbs:
            self.assertIn(pb["rule_id"], spec.rules, msg=pb["id"])
            if pb["channel"] == "function":
                self.assertIn(pb["function"], declared_fns, msg=pb["id"])
            else:
                self.assertEqual(pb["function"], "", msg=pb["id"])

    def test_core_pack_shape(self):
        """内核包 11 条（按声明序）：R6 function×2 + external×1；R1 function×1
        + external×2（r1_image_original_match 为 P8 图像核验 external）；
        R-GEO-1 function×1 + external×1；R-GEO-2 external×1；R-GEO-3 external×2。"""
        pbs = load_verify_playbooks("default")
        self.assertEqual([p["id"] for p in pbs],
                         ["r6_fund_tw_rerun", "r6_call_window",
                          "r6_bid_archive", "r1_quarter_end_deposit_rerun",
                          "r1_deposit_slip_archive",
                          "r1_image_original_match",
                          "rgeo1_cgt_rerun", "rgeo1_grid_visit",
                          "rgeo2_site_archive", "rgeo3_site_nature",
                          "rgeo3_relation_check"])
        self.assertEqual([p["channel"] for p in pbs],
                         ["function", "function", "external",
                          "function", "external", "external",
                          "function", "external",
                          "external", "external", "external"])
        self.assertEqual(
            [p["function"] for p in pbs],
            ["time_window_collision", "call_frequency_spike", "",
             "quarter_end_integer_deposits", "", "",
             "geo_profile_cgt", "", "", "", ""])
        self.assertEqual(pbs[2]["external"]["material"],
                         "中标项目招投标底档及资金审批联签单")
        # 槽位只落在已声明 subject_column 的规则手册条目上（R6=owner_raw、
        # R1 未声明主体列 → 其 {subject} 槽位在无主体时按 D3 留痕跳过）
        self.assertEqual([p["id"] for p in pbs
                          if "{subject}" in p["text"] or
                          "{project_count}" in p["text"]],
                         ["r6_fund_tw_rerun", "r6_call_window",
                          "r6_bid_archive", "r1_image_original_match",
                          "rgeo2_site_archive"])


# ----------------------------------------------------------------------
# AC1/AC7 + 补充：render_suggested 确定性渲染（纯函数）
# ----------------------------------------------------------------------
class VerifySuggestRenderTest(unittest.TestCase):
    def test_r6_slots_subject_and_count(self):
        """槽位：剔单位 + 金额为正整数倍过滤；并列取名称排序首者。"""
        rows = [
            {"owner_raw": "张卫国", "amount": 100000},
            {"owner_raw": "张卫国", "amount": 100000},
            {"owner_raw": "华清越", "amount": 100000},
            {"owner_raw": "华清越", "amount": 100000},
            {"owner_raw": "某公司", "amount": 100000},   # 单位后缀 → 剔
            {"owner_raw": "李四", "amount": 0},           # 非正 → 剔
            {"owner_raw": "王五", "amount": 12345},       # 非整数倍 → 剔
            {"owner_raw": "赵六", "amount": "abc"},       # 脏值 → 剔
        ]
        raw = _synthetic_r6_raw(rows=rows, assumption=["H1"])
        items, skipped = render_suggested(raw, [])
        self.assertEqual(skipped, [])
        # 并列 2:2 → 名称排序首者 = 华清越（确定性）
        first = next(i for i in items if i["channel"] == "function"
                     and "复跑" in i["text"])
        self.assertIn("华清越", first["text"])
        self.assertIn("2", first["text"])
        self.assertEqual(first["ref_function"], "time_window_collision")

    def test_rule_without_playbooks_no_suggestions(self):
        """无手册条目的规则（R2）→ 零建议零跳过（确定性空）。"""
        raw = _synthetic_r6_raw(rule_id="R2", assumption=["H1"])
        items, skipped = render_suggested(raw, [])
        self.assertEqual((items, skipped), ([], []))

    def test_r1_rule_renders_quarter_end_suggestions(self):
        """R1 已有手册条目（季末整数存款）：function×1 + external×1；
        文本无槽位（无需主体统计）、无假设约束（假设门直通）。
        P8 的 r1_image_original_match（external，{subject} 槽位）在无有效
        主体时合法 skip（D3 留痕），不落建议项。"""
        raw = _synthetic_r6_raw(rule_id="R1", assumption=["H1"])
        items, skipped = render_suggested(raw, [])
        self.assertEqual(skipped, [{"playbook_id": "r1_image_original_match",
                                    "reason": "过滤后无有效主体"}])
        self.assertEqual([i["playbook_id"] for i in items],
                         ["r1_quarter_end_deposit_rerun",
                          "r1_deposit_slip_archive"])
        self.assertEqual([i["channel"] for i in items],
                         ["function", "external"])
        self.assertEqual(items[0]["ref_function"],
                         "quarter_end_integer_deposits")
        self.assertEqual(items[1]["external"]["target"], "开户银行")

    def test_assumption_gate(self):
        """假设不交集：H1/H4 项不出，无假设约束的 external 项照出。"""
        raw = _synthetic_r6_raw(assumption=["H9"])
        items, _ = render_suggested(raw, [])
        self.assertEqual([i["channel"] for i in items], ["external"])

    def test_no_subject_skipped_with_reason(self):
        rows = [{"owner_raw": "某某公司", "amount": 100000}]
        raw = _synthetic_r6_raw(rows=rows, assumption=["H1"])
        items, skipped = render_suggested(raw, [])
        self.assertEqual(items, [])
        self.assertEqual([s["playbook_id"] for s in skipped],
                         ["r6_fund_tw_rerun", "r6_call_window",
                          "r6_bid_archive"])
        self.assertTrue(all(s["reason"] == "过滤后无有效主体"
                            for s in skipped))

    # ---- AC7：事实卡永不成为建议项 ----
    def test_fact_card_never_suggested(self):
        raw = _synthetic_r6_raw(assumption=["H1"])
        fact = {"id": "f0", "kind": "fact", "text": "事实卡文本不应成项"}
        items, _ = render_suggested(raw, [fact])
        self.assertNotIn(fact["text"], [i["text"] for i in items])
        self.assertTrue(all(i["origin"] == "suggested" for i in items))


# ----------------------------------------------------------------------
# AC1（读面回归）+ 幂等：R6 合并线索 assemble_detail 供给建议项
# ----------------------------------------------------------------------
def _write_artifact(case_dir: Path, version: int, clues: list[dict]) -> None:
    art_dir = case_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / f"clues_v{version}.json").write_text(
        json.dumps({"version": version, "clues": clues},
                   ensure_ascii=False), encoding="utf-8")


class VerifySuggestViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.case_dir = self.tmp / "c1"
        self.access = AccessContext(
            operator="王峰", role="正兵", clearance=3, case_id="c1",
            purpose="REQ-V-018 测试", network="web")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _detail(self, state_store):
        return assemble_detail(
            case_dir=self.case_dir, version=8, clue_id=CLUE,
            state_map={}, access=self.access, pack_id="default",
            base_dir=None, state_store=state_store)

    # ---- AC1：3 条建议 + 主体/计数槽位 + 稳定键 ----
    def test_merged_r6_suggested_items(self):
        _write_artifact(self.case_dir, 8, [_merged_r6_raw()])
        with StateStore("c1", self.case_dir / "state.sqlite") as st:
            data = self._detail(st)
        items = data["verify"]["items"]
        sugg = [i for i in items if i["origin"] == "suggested"]
        self.assertEqual(len(sugg), 3)
        self.assertEqual([i["status"] for i in sugg], ["建议"] * 3)
        self.assertEqual([i["channel"] for i in sugg],
                         ["function", "function", "external"])
        rerun = sugg[0]
        self.assertIn("张卫国", rerun["text"])
        self.assertIn("8", rerun["text"])  # D1 裁决：project_count=8
        self.assertEqual(rerun["ref_function"], "time_window_collision")
        self.assertEqual(rerun["external"], None)
        ext = sugg[2]
        self.assertEqual(ext["external"]["target"], "住建局招标办")
        self.assertEqual(ext["external"]["material"],
                         "中标项目招投标底档及资金审批联签单")
        # 稳定键：vi_ + sha1(clue|suggested|text)[:16]
        for it in sugg:
            self.assertEqual(it["item_id"],
                             "vi_" + StateStore.verify_item_key(
                                 CLUE, "suggested", it["text"]))
        # 建议/已忽略不进 pending（固证门禁不拦截），独立计数
        prog = data["verify"]["progress"]
        self.assertEqual(prog["suggested"], 3)
        self.assertEqual(prog["pending"], 2)  # 仅 auto 两项

    # ---- 幂等：二次详情 added=0（item_id 不变、不重复供给）----
    def test_second_detail_idempotent(self):
        _write_artifact(self.case_dir, 8, [_merged_r6_raw()])
        with StateStore("c1", self.case_dir / "state.sqlite") as st:
            first = self._detail(st)
            second = self._detail(st)
        ids1 = sorted(i["item_id"] for i in first["verify"]["items"])
        ids2 = sorted(i["item_id"] for i in second["verify"]["items"])
        self.assertEqual(ids1, ids2)
        self.assertEqual(len(ids2), 5)  # 2 auto + 3 suggested

    # ---- 回填路径：合并线索（无 rule_id）经 backfill 后同样供给建议 ----
    def test_merged_clue_backfilled_then_suggested(self):
        raw = _merged_r6_raw()
        backfilled = backfill_rule_fields(raw, pack_id="default")
        self.assertEqual(backfilled["detail"].get("rule_id"), "R6")
        # 与 clues_view 生产路径同源：证据三栏（含待核实 h 卡文本）先建一次，
        # H1 假设由 evidence 提供（该线索自身 assumption_chain 仅 H4）。
        evidence = build_evidence(
            raw_clue=backfilled, conn=None, pack_id="default",
            base_dir=None, access=None)
        items, _ = render_suggested(backfilled, evidence)
        self.assertEqual(len(items), 3)


# ----------------------------------------------------------------------
# AC2/AC3/AC4：建议生命周期（Worker 写通道：采纳/忽略/重采纳/拒绝）
# ----------------------------------------------------------------------
def _stub_builder(conn, *, pack: str, base_dir: Path, progress) -> dict:
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    progress(90.0, "compile", "完成", "stub")
    return {"objects": {}, "links": {}, "skipped": []}


def _sugg_text_fn():
    return "复跑 张卫国 的整数资金时间窗核查（function 渠道）"


def _sugg_text_ext():
    return "向住建局招标办调取 张卫国 关联中标项目招投标底档及资金审批联签单"


class VerifySuggestLifecycleTest(unittest.TestCase):
    OP = "李检察官"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="核查案"))
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=lambda cid: PACK_ROOT,
                     builder=_stub_builder,
                     auto_quality_after_build=False)
        self.state_path = self.factory.case_dir("c1") / "state.sqlite"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        with StateStore("c1", self.state_path) as st:
            st.upsert_verify_items("c1", CLUE, [
                {"kind": "suggested", "text": _sugg_text_fn(),
                 "origin": "suggested", "status": "建议",
                 "channel": "function",
                 "ref_function": "time_window_collision",
                 "falsification": "均为对公工程尾款"},
                {"kind": "suggested", "text": _sugg_text_ext(),
                 "origin": "suggested", "status": "建议",
                 "channel": "external",
                 "external": {"target": "住建局招标办",
                              "material": "招投标底档"}},
            ])
        return [
            "vi_" + StateStore.verify_item_key(CLUE, "suggested",
                                               _sugg_text_fn()),
            "vi_" + StateStore.verify_item_key(CLUE, "suggested",
                                               _sugg_text_ext()),
        ]

    def _run(self, params):
        row = enqueue_task(
            self.repo, case_id="c1", task_type=TASK_VERIFY,
            params=params, created_by=params.get("operator") or self.OP)
        return handle_verify(row, repo=self.repo, factory=self.factory)

    def _row(self, item_id):
        with StateStore("c1", self.state_path) as st:
            return st.get_verify_item(item_id)

    def _progress(self):
        with StateStore("c1", self.state_path) as st:
            return st.verify_progress(CLUE)

    # ---- AC2：采纳 → 待核查（origin 不变、text 保留）；忽略；重采纳 ----
    def test_adopt_ignore_readopt(self):
        fn_id, ext_id = self._seed()
        self.assertEqual(self._progress(),
                         {"total": 2, "concluded": 0, "pending": 0,
                          "suggested": 2, "ignored": 0,
                          "by_status": {"建议": 2}})
        r = self._run({"op": "transition", "clue_id": CLUE,
                       "item_id": fn_id, "next_status": "待核查",
                       "operator": self.OP})
        self.assertEqual(r["status"], "待核查")
        row = self._row(fn_id)
        self.assertEqual(row["origin"], "suggested")  # 语义保持 suggested
        self.assertEqual(row["text"], _sugg_text_fn())
        self.assertEqual(row["channel"], "function")
        self.assertEqual(self._progress()["pending"], 1)

        self._run({"op": "transition", "clue_id": CLUE,
                   "item_id": ext_id, "next_status": "已忽略",
                   "operator": self.OP})
        self.assertEqual(self._row(ext_id)["status"], "已忽略")
        # 重采纳
        self._run({"op": "transition", "clue_id": CLUE,
                   "item_id": ext_id, "next_status": "待核查",
                   "operator": self.OP})
        self.assertEqual(self._row(ext_id)["status"], "待核查")

    # ---- AC2 补充：采纳改写后重跑供给，item 不被覆盖（added=0）----
    def test_adopt_rewrite_then_reprovision(self):
        fn_id, _ = self._seed()
        self._run({"op": "transition", "clue_id": CLUE,
                   "item_id": fn_id, "next_status": "待核查",
                   "text": "改一改：先核对对公账户往来",
                   "operator": self.OP})
        row = self._row(fn_id)
        self.assertEqual(row["text"], "改一改：先核对对公账户往来")
        with StateStore("c1", self.state_path) as st:
            result = st.upsert_verify_items("c1", CLUE, [
                {"kind": "suggested", "text": _sugg_text_fn(),
                 "origin": "suggested", "status": "建议",
                 "channel": "function",
                 "ref_function": "time_window_collision"}])
        self.assertEqual(result["added"], 0)
        self.assertEqual(self._row(fn_id)["text"],
                         "改一改：先核对对公账户往来")

    # ---- AC2：仅建议/已忽略存在 → pending=0（固证门禁不拦截）----
    def test_suggestions_do_not_block_gate(self):
        self._seed()
        prog = self._progress()
        self.assertEqual(prog["pending"], 0)
        self.assertEqual(prog["suggested"], 2)

    # ---- AC3：非法迁移 → VERIFY_REJECTED ----
    def test_illegal_transition_rejected(self):
        fn_id, _ = self._seed()
        with self.assertRaises(TaskExecError) as cm:
            self._run({"op": "transition", "clue_id": CLUE,
                       "item_id": fn_id, "next_status": "已证实",
                       "conclusion": "跳过待核查直接裁决",
                       "operator": self.OP})
        self.assertEqual(cm.exception.code, "VERIFY_REJECTED")

    # ---- AC4：agent 拒绝；human 正常 ----
    def test_agent_forbidden_human_allowed(self):
        fn_id, ext_id = self._seed()
        for nxt in ("待核查", "已忽略"):
            with self.assertRaises(TaskExecError) as cm:
                self._run({"op": "transition", "clue_id": CLUE,
                           "item_id": fn_id, "next_status": nxt,
                           "operator": "agent:codex"})
            self.assertEqual(cm.exception.code, "VERIFY_FORBIDDEN")
        # agent 拒绝后状态未变
        self.assertEqual(self._row(fn_id)["status"], "建议")
        self._run({"op": "transition", "clue_id": CLUE,
                   "item_id": fn_id, "next_status": "待核查",
                   "operator": self.OP})
        self.assertEqual(self._row(fn_id)["status"], "待核查")


if __name__ == "__main__":
    unittest.main()
