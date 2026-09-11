"""
tests/test_verify_item.py
核查工作区 REQ-V-001：StateStore 核查项 CRUD + ADR-V-4 稳定键。

同组后续：REQ-V-002 惰性供给用例也注册在 verifyitem 组（本文件追加）。

断言（实施方案 REQ-V-001 验收 1~5 + 方法层补充）：
  1. 同批 items 两次 upsert：第二次 added=0、total 不变（INSERT OR IGNORE 幂等）；
  2. 已有结论的项重跑 upsert：conclusion/status/operator 不被覆盖；
  3. verify_item_key 对 (clue_id,kind,text) 稳定，不同 text 不同键；
     并锁死演示案例（demoF v8 clue_9446b1bd）两个真实 item_id；
  4. verify_progress：终态三态计入 concluded，待核查/核查中计入 pending，
     建议/已忽略独立计数、不进门禁；
  5. 旧案件 state.sqlite（无核查三表/核查表缺 REQ-V-018 四列）打开即迁移可用。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext
from core.audit import AuditChain

from server.app.clues_view import assemble_detail
from server.app.meta.models import CASE_ACTIVE, CaseRecord
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory
from server.app.worker.tasks import (
    TASK_BUILD,
    TASK_VERIFY,
    TaskExecError,
    enqueue_task,
    handle_build,
)
from server.app.verify_provision import (
    backfill_rule_fields,
    provision_for_clue,
    provision_from_evidence,
)
from server.app.worker.verify import handle_verify

from core.verify_machine import (
    CONCLUSION_REQUIRED,
    ERR_CONCLUSION_REQUIRED,
    ERR_INVALID_TRANSITION,
    ERR_UNKNOWN_STATUS,
    VERIFY_STATUSES,
    VERIFY_TRANSITIONS,
    VerifyTransitionError,
    can_transition,
    legal_targets,
    validate_conclusion,
    validate_verify_transition,
)
from server.app.store.state_store import StateStore

CLUE = "clue_9446b1bd"
INFERENCE_TEXT = "中标公示 ±20 天邻接边上出现整数资金、且资金主体为个人（非对公单位）"
HYPOTHESIS_TEXT = "待验证假设：H1（收受财物（异常整数现金存入））"


class VerifyItemCRUDTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "cases" / "demoF" / "state.sqlite"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self) -> StateStore:
        return StateStore("demoF", self.db)

    # ---- AC3：ADR-V-4 稳定键 ----
    def test_verify_item_key_stable_and_distinct(self):
        k1 = StateStore.verify_item_key(CLUE, "inference", INFERENCE_TEXT)
        self.assertEqual(k1, StateStore.verify_item_key(
            CLUE, "inference", INFERENCE_TEXT))
        self.assertEqual(len(k1), 16)
        self.assertNotEqual(
            k1, StateStore.verify_item_key(CLUE, "inference", "另一段文本"))
        self.assertNotEqual(
            k1, StateStore.verify_item_key(CLUE, "manual", INFERENCE_TEXT))
        self.assertNotEqual(
            k1, StateStore.verify_item_key("clue_other", "inference",
                                           INFERENCE_TEXT))

    def test_item_id_matches_demo_script(self):
        """锁死 .trae/documents/核查工作区/演示案例-张卫国R6时间窗.md 的真实键。"""
        k_inf = StateStore.verify_item_key(CLUE, "inference", INFERENCE_TEXT)
        k_hyp = StateStore.verify_item_key(
            CLUE, "pending_hypothesis", HYPOTHESIS_TEXT)
        self.assertEqual(f"vi_{k_inf}", "vi_d0577e14036a1c2e")
        self.assertEqual(f"vi_{k_hyp}", "vi_6a7a9feea147f413")

    # ---- AC1：upsert 幂等 ----
    def test_upsert_idempotent(self):
        st = self._store()
        try:
            items = [
                {"kind": "inference", "text": INFERENCE_TEXT},
                {"kind": "pending_hypothesis", "text": HYPOTHESIS_TEXT},
            ]
            first = st.upsert_verify_items("demoF", CLUE, items)
            self.assertEqual(first, {"added": 2, "total": 2})
            second = st.upsert_verify_items("demoF", CLUE, items)
            self.assertEqual(second, {"added": 0, "total": 2})
        finally:
            st.close()

    # ---- AC2：重跑供给不覆盖人裁结论 ----
    def test_upsert_preserves_conclusion(self):
        st = self._store()
        try:
            st.upsert_verify_items(
                "demoF", CLUE, [{"kind": "inference", "text": INFERENCE_TEXT}])
            item_id = "vi_" + StateStore.verify_item_key(
                CLUE, "inference", INFERENCE_TEXT)
            moved = st.transition_verify_item(
                item_id, status="核查中", conclusion="",
                operator="王峰", updated_at="2026-09-11 10:00:00")
            self.assertEqual(moved["status"], "核查中")
            st.transition_verify_item(
                item_id, status="已证实",
                conclusion="主体后窗口 7 行复核无误",
                operator="王峰", updated_at="2026-09-11 10:05:00")
            # 重新供给（同一批 auto 项）
            st.upsert_verify_items(
                "demoF", CLUE, [{"kind": "inference", "text": INFERENCE_TEXT}])
            row = st.get_verify_item(item_id)
            self.assertEqual(row["status"], "已证实")
            self.assertEqual(row["conclusion"], "主体后窗口 7 行复核无误")
            self.assertEqual(row["operator"], "王峰")
            self.assertEqual(row["updated_at"], "2026-09-11 10:05:00")
        finally:
            st.close()

    # ---- AC4：progress 口径 ----
    def test_progress_counts(self):
        st = self._store()
        try:
            st.upsert_verify_items("demoF", CLUE, [
                {"kind": "inference", "text": "推断A"},
                {"kind": "pending_hypothesis", "text": "假设B"},
                {"kind": "manual", "text": "人工C", "origin": "manual"},
            ])
            ids = {t: f"vi_{StateStore.verify_item_key(CLUE, k, t)}"
                   for t, k in (("推断A", "inference"),
                                ("假设B", "pending_hypothesis"),
                                ("人工C", "manual"))}
            st.transition_verify_item(
                ids["推断A"], status="已证实", conclusion="c1",
                operator="王峰", updated_at="t1")
            st.transition_verify_item(
                ids["假设B"], status="已查否", conclusion="c2",
                operator="王峰", updated_at="t2")
            p = st.verify_progress(CLUE)
            self.assertEqual(p["total"], 3)
            self.assertEqual(p["concluded"], 2)
            self.assertEqual(p["pending"], 1)
            self.assertEqual(p["suggested"], 0)
            self.assertEqual(p["ignored"], 0)
            self.assertEqual(p["by_status"]["待核查"], 1)

            # 建议态与已忽略：独立计数、不进 pending/concluded
            st.upsert_verify_items("demoF", CLUE, [
                {"kind": "suggested", "text": "手册建议D",
                 "origin": "suggested", "status": "建议"},
                {"kind": "suggested", "text": "手册建议E",
                 "origin": "suggested", "status": "建议"},
            ])
            p2 = st.verify_progress(CLUE)
            self.assertEqual(p2["total"], 5)
            self.assertEqual(p2["pending"], 1)
            self.assertEqual(p2["concluded"], 2)
            self.assertEqual(p2["suggested"], 2)
            # 忽略一条建议
            eid = f"vi_{StateStore.verify_item_key(CLUE, 'suggested', '手册建议E')}"
            st.transition_verify_item(
                eid, status="已忽略", conclusion="",
                operator="王峰", updated_at="t3")
            p3 = st.verify_progress(CLUE)
            self.assertEqual(p3["pending"], 1)
            self.assertEqual(p3["suggested"], 1)
            self.assertEqual(p3["ignored"], 1)
        finally:
            st.close()

    # ---- 建议项路由字段 + external JSON 往返 ----
    def test_suggested_item_route_fields(self):
        st = self._store()
        try:
            st.upsert_verify_items("demoF", CLUE, [{
                "kind": "suggested",
                "text": "调取{subject}账户在 {project_count} 个中标公示日 ±20 天的完整流水",
                "origin": "suggested", "status": "建议",
                "channel": "external",
                "external": {"target": "宏业建设开户银行",
                             "material": "张卫国 7 个项目窗口完整流水"},
                "falsification": "窗口内无整数资金则证伪",
            }, {
                "kind": "suggested", "text": "库内复跑建议",
                "origin": "suggested", "status": "建议",
                "channel": "function", "ref_function": "overpass_two_hop",
            }])
            rows = {r["text"]: r for r in st.list_verify_items(CLUE)}
            ext = rows["调取{subject}账户在 {project_count} 个中标公示日 ±20 天的完整流水"]
            self.assertEqual(ext["status"], "建议")
            self.assertEqual(ext["channel"], "external")
            self.assertEqual(ext["external"]["target"], "宏业建设开户银行")
            self.assertEqual(ext["falsification"], "窗口内无整数资金则证伪")
            fn = rows["库内复跑建议"]
            self.assertEqual(fn["ref_function"], "overpass_two_hop")
            self.assertIsNone(fn["external"])
        finally:
            st.close()

    # ---- 人工项幂等 + 列表排序（待办在前、建议垫后）----
    def test_add_manual_idempotent_and_list_order(self):
        st = self._store()
        try:
            r1 = st.add_manual_verify_item("demoF", CLUE, "人工补充方向")
            self.assertEqual(r1["added"], 1)
            self.assertEqual(r1["kind"], "manual")
            self.assertEqual(r1["origin"], "manual")
            self.assertTrue(r1["item_id"].startswith("vi_"))
            r2 = st.add_manual_verify_item("demoF", CLUE, "人工补充方向")
            self.assertEqual(r2["added"], 0)
            self.assertEqual(r2["item_id"], r1["item_id"])

            st.upsert_verify_items("demoF", CLUE, [
                {"kind": "suggested", "text": "建议垫后",
                 "origin": "suggested", "status": "建议"},
                {"kind": "inference", "text": "推断待办"},
            ])
            # 同状态（待核查）内按创建序：manual 先于 inference 创建；
            # 建议态（rank 5）整体垫后
            texts = [r["text"] for r in st.list_verify_items(CLUE)]
            self.assertEqual(texts, ["人工补充方向", "推断待办", "建议垫后"])
        finally:
            st.close()

    # ---- transition 不存在项 → None ----
    def test_transition_missing_returns_none(self):
        st = self._store()
        try:
            self.assertIsNone(st.transition_verify_item(
                "vi_notexists", status="已证实", conclusion="x",
                operator="王峰", updated_at="t"))
        finally:
            st.close()

    # ---- AC5：旧库（无核查三表）打开即建表可用 ----
    def test_legacy_db_without_tables_is_bootstrapped(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        legacy = sqlite3.connect(str(self.db))
        try:
            legacy.execute(
                "CREATE TABLE audit_chain (seq INTEGER NOT NULL, "
                "event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
                "ontology_version TEXT NOT NULL, rule_version TEXT, "
                "function_version TEXT, params_hash TEXT, "
                "source_row_ids TEXT, operator TEXT NOT NULL, "
                "before_state TEXT, after_state TEXT, prev_hash TEXT NOT NULL, "
                "signature TEXT NOT NULL, occurred_at TEXT NOT NULL)")
            legacy.execute(
                "INSERT INTO audit_chain VALUES (0,'e1','demoF','v8',NULL,NULL,"
                "NULL,NULL,'王峰',NULL,NULL,?,?,?)",
                ["0" * 64, "s", "2026-09-11 09:00:00"])
            legacy.commit()
        finally:
            legacy.close()

        st = self._store()
        try:
            tables = {r[0] for r in st._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for t in ("clue_verify_item", "clue_evidence", "verify_request"):
                self.assertIn(t, tables)
            # 旧数据完好 + 新方法可用
            self.assertEqual(st.event_count(), 1)
            self.assertEqual(
                st.upsert_verify_items(
                    "demoF", CLUE, [{"kind": "inference", "text": INFERENCE_TEXT}]),
                {"added": 1, "total": 1})
        finally:
            st.close()

    # ---- 旧库：clue_verify_item 已存在但缺 REQ-V-018 四列 → 幂等 ALTER ----
    def test_legacy_verify_item_alter_adds_columns(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        legacy = sqlite3.connect(str(self.db))
        try:
            legacy.execute(
                "CREATE TABLE clue_verify_item ("
                "item_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
                "clue_id TEXT NOT NULL, item_key TEXT NOT NULL, "
                "kind TEXT NOT NULL, text TEXT NOT NULL, "
                "origin TEXT NOT NULL DEFAULT 'auto', "
                "status TEXT NOT NULL DEFAULT '待核查', "
                "conclusion TEXT NOT NULL DEFAULT '', "
                "operator TEXT NOT NULL DEFAULT '', "
                "updated_at TEXT NOT NULL DEFAULT '', "
                "UNIQUE(clue_id, item_key))")
            key = StateStore.verify_item_key(CLUE, "inference", INFERENCE_TEXT)
            legacy.execute(
                "INSERT INTO clue_verify_item VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [f"vi_{key}", "demoF", CLUE, key, "inference", INFERENCE_TEXT,
                 "auto", "待核查", "", "", ""])
            legacy.commit()
        finally:
            legacy.close()

        st = self._store()
        try:
            cols = {r[1] for r in st._conn.execute(
                "PRAGMA table_info(clue_verify_item)").fetchall()}
            for c in ("channel", "ref_function", "external_json",
                      "falsification"):
                self.assertIn(c, cols)
            # 再次打开不重复 ALTER、不报错（幂等）
            st._migrate_verify_item_columns()
            legacy_key = StateStore.verify_item_key(
                CLUE, "inference", INFERENCE_TEXT)
            row = st.get_verify_item(f"vi_{legacy_key}")
            self.assertEqual(row["text"], INFERENCE_TEXT)  # 旧数据保留
            self.assertEqual(row["channel"], "")
            self.assertIsNone(row["external"])
            # 迁移后的行可正常裁决
            moved = st.transition_verify_item(
                row["item_id"], status="核查中", conclusion="",
                operator="王峰", updated_at="t")
            self.assertEqual(moved["status"], "核查中")
        finally:
            st.close()

    # ---- 三表索引齐备 ----
    def test_three_tables_and_indexes(self):
        st = self._store()
        try:
            indexes = {r[0] for r in st._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
            for idx in ("idx_vi_clue", "idx_ev_clue", "idx_vr_clue"):
                self.assertIn(idx, indexes)
        finally:
            st.close()


class VerifyMachineTest(unittest.TestCase):
    """REQ-V-003：core/verify_machine 纯函数状态机。"""

    # ---- AC1：非法迁移抛 ValueError（带 INVALID_TRANSITION 码）----
    def test_illegal_transitions_raise(self):
        for cur, nxt in (("待核查", "待核查"),     # 自迁移非法
                         ("已证实", "已查否"),     # 终态间互跳非法
                         ("建议", "已证实"),       # 建议不能跳采纳直接裁决
                         ("已忽略", "核查中"),     # 已忽略只能先重新采纳
                         ("已查否", "待核查")):    # 终态只能重开到核查中
            with self.subTest(cur=cur, nxt=nxt):
                self.assertFalse(can_transition(cur, nxt))
                with self.assertRaises(VerifyTransitionError) as cm:
                    validate_verify_transition(cur, nxt)
                self.assertIsInstance(cm.exception, ValueError)
                self.assertEqual(cm.exception.code, ERR_INVALID_TRANSITION)

    def test_unknown_status_raises(self):
        for cur, nxt in (("不存在的状态", "待核查"),
                         ("待核查", "已立案"),
                         ("", "待核查")):
            with self.subTest(cur=cur, nxt=nxt):
                self.assertFalse(can_transition(cur, nxt))
                with self.assertRaises(VerifyTransitionError) as cm:
                    validate_verify_transition(cur, nxt)
                self.assertEqual(cm.exception.code, ERR_UNKNOWN_STATUS)

    # ---- AC3：合法转移全表遍历通过 ----
    def test_all_declared_edges_are_valid(self):
        edges = 0
        for cur, targets in VERIFY_TRANSITIONS.items():
            self.assertIn(cur, VERIFY_STATUSES)
            for nxt in targets:
                self.assertIn(nxt, VERIFY_STATUSES)
                validate_verify_transition(cur, nxt)  # 不抛即通过
                self.assertTrue(can_transition(cur, nxt))
                self.assertIn(nxt, legal_targets(cur))
                edges += 1
        self.assertEqual(edges, 14)  # 锁定全图边数，改表须显式更新

    def test_key_lifecycle_edges(self):
        """演示动线关键边：采纳/忽略/重新采纳/退回/三终态/翻案重开。"""
        # 建议项生命周期
        validate_verify_transition("建议", "待核查")
        validate_verify_transition("建议", "已忽略")
        validate_verify_transition("已忽略", "待核查")
        # 待核查 → 三终态 + 核查中
        for nxt in ("核查中", "已证实", "已查否", "无法核实"):
            validate_verify_transition("待核查", nxt)
        # 核查中退回
        validate_verify_transition("核查中", "待核查")
        # 终态翻案重开（侦查实务允许）
        for term in ("已证实", "已查否", "无法核实"):
            validate_verify_transition(term, "核查中")

    # ---- AC2：终态缺结论 → CONCLUSION_REQUIRED ----
    def test_conclusion_required(self):
        for status, empty in (("已证实", ""), ("已查否", "   "),
                              ("已查否", None)):
            with self.subTest(status=status):
                with self.assertRaises(VerifyTransitionError) as cm:
                    validate_conclusion(status, empty)
                self.assertEqual(cm.exception.code, ERR_CONCLUSION_REQUIRED)
        # 有结论通过
        for status in CONCLUSION_REQUIRED:
            validate_conclusion(status, "7 行复核无误")
        # 无法核实不强制（可挂起转外部调取）
        validate_conclusion("无法核实", "")
        validate_conclusion("无法核实", None)
        # 非终态不校验
        validate_conclusion("待核查", "")

    def test_transitions_table_shape(self):
        """表完备性：七态均在转移表中；三终态只允许重开到核查中。"""
        self.assertEqual(set(VERIFY_STATUSES), set(VERIFY_TRANSITIONS))
        for term in ("已证实", "已查否", "无法核实"):
            self.assertEqual(legal_targets(term), ("核查中",))
        # 建议态没有"裁决类"出口（采纳/忽略是仅有的两条边）
        self.assertEqual(set(legal_targets("建议")), {"待核查", "已忽略"})
        # 核查中可退回待核查
        self.assertIn("待核查", legal_targets("核查中"))


def _stub_builder(conn, *, pack: str, base_dir: Path, progress) -> dict:
    """桩构建器：建一张表即成功（REQ-V-004 只需版本锚点，不消费语义层）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    progress(90.0, "compile", "完成", "stub")
    return {"objects": {}, "links": {}, "skipped": []}


class VerifyWorkerTest(unittest.TestCase):
    """REQ-V-004：TASK_VERIFY Worker 写通道（校验→state 写→审计链）。"""

    OP = "李检察官"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="核查案"))
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        # 桩 BUILD 置 v1（handler 要求 ver>=1 作审计锚点；核查写本身不碰版本库）
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=lambda cid: ROOT / "ontology",
                     builder=_stub_builder,
                     auto_quality_after_build=False)
        self.state_path = self.factory.case_dir("c1") / "state.sqlite"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 辅助 ----
    def _seed(self, items, clue_id=CLUE):
        with StateStore("c1", self.state_path) as st:
            return st.upsert_verify_items("c1", clue_id, items)

    def _item_id(self, text, kind="inference", clue_id=CLUE):
        return f"vi_{StateStore.verify_item_key(clue_id, kind, text)}"

    def _run(self, params):
        row = enqueue_task(
            self.repo, case_id="c1", task_type=TASK_VERIFY,
            params=params, created_by=params.get("operator") or self.OP)
        return handle_verify(row, repo=self.repo, factory=self.factory)

    def _row(self, item_id):
        with StateStore("c1", self.state_path) as st:
            return st.get_verify_item(item_id)

    def _audit_events(self):
        """只返核查写事件（build_succeeded 等生命周期事件不计入断言；
        链完整性由 _chain_ok() 覆盖全链）。"""
        with StateStore("c1", self.state_path) as st:
            rows = st.conn.execute(
                "SELECT operator, ontology_version, before_state, after_state "
                "FROM audit_chain WHERE json_extract(after_state,'$.event') "
                "LIKE 'verify_item_%' ORDER BY seq").fetchall()
        return [(r["operator"], r["ontology_version"],
                 json.loads(r["before_state"]) if r["before_state"] else None,
                 json.loads(r["after_state"])) for r in rows]

    def _chain_ok(self):
        with StateStore("c1", self.state_path) as st:
            return AuditChain(st.conn, "c1", backend="sqlite",
                              ontology_version="v1").chain_verify()

    def _transition_params(self, item_id, nxt, **kw):
        return {"op": "transition", "clue_id": CLUE, "item_id": item_id,
                "next_status": nxt, "operator": self.OP, **kw}

    # ---- AC1：合法转移写 state + 审计链 1 条且链校验通过 ----
    def test_legal_transition_writes_state_and_chain(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        res = self._run(self._transition_params(iid, "核查中"))
        self.assertEqual(res["status"], "核查中")
        row = self._row(iid)
        self.assertEqual(row["status"], "核查中")
        self.assertEqual(row["operator"], self.OP)
        self.assertTrue(row["updated_at"])
        events = self._audit_events()
        self.assertEqual(len(events), 1)
        operator, ver, before, after = events[0]
        self.assertEqual(operator, self.OP)
        self.assertEqual(ver, "v1")
        self.assertEqual(before["status"], "待核查")
        self.assertEqual(after["status"], "核查中")
        self.assertEqual(after["item_id"], iid)
        self.assertEqual(after["event"], "verify_item_transition")
        self.assertTrue(self._chain_ok())

    # ---- AC2：agent: 身份拒绝 ----
    def test_agent_operator_forbidden(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        with self.assertRaises(TaskExecError) as cm:
            self._run(self._transition_params(iid, "核查中",
                                              operator="agent:rookie"))
        self.assertEqual(cm.exception.code, "VERIFY_FORBIDDEN")
        # 行未改、链未动
        self.assertEqual(self._row(iid)["status"], "待核查")
        self.assertEqual(self._audit_events(), [])

    def test_anonymous_operator_forbidden(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        with self.assertRaises(TaskExecError) as cm:
            self._run(self._transition_params(iid, "核查中", operator=""))
        self.assertEqual(cm.exception.code, "VERIFY_FORBIDDEN")

    # ---- AC3：非法迁移/不存在项/缺结论 ----
    def test_illegal_transition_rejected(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        with self.assertRaises(TaskExecError) as cm:
            self._run(self._transition_params(iid, "建议"))  # 待核查→建议非法
        self.assertEqual(cm.exception.code, "VERIFY_REJECTED")
        self.assertEqual(self._row(iid)["status"], "待核查")
        self.assertEqual(self._audit_events(), [])

    def test_missing_item_not_found(self):
        with self.assertRaises(TaskExecError) as cm:
            self._run(self._transition_params("vi_deadbeefdead", "核查中"))
        self.assertEqual(cm.exception.code, "ITEM_NOT_FOUND")

    def test_cross_clue_item_not_found(self):
        """item 属于别的线索：按不存在处理（防跨线索改写）。"""
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        p = self._transition_params(iid, "核查中")
        p["clue_id"] = "clue_other"
        with self.assertRaises(TaskExecError) as cm:
            self._run(p)
        self.assertEqual(cm.exception.code, "ITEM_NOT_FOUND")

    def test_conclusion_required_code_preserved(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        with self.assertRaises(TaskExecError) as cm:
            self._run(self._transition_params(iid, "已证实", conclusion=""))
        self.assertEqual(cm.exception.code, "CONCLUSION_REQUIRED")
        # 有结论即放行
        self._run(self._transition_params(iid, "已证实", conclusion="七行复核属实"))
        self.assertEqual(self._row(iid)["status"], "已证实")

    # ---- AC4：同 item 两个转移任务 FIFO 串行均成功 ----
    def test_two_transitions_fifo_both_succeed(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        r1 = self._run(self._transition_params(iid, "核查中"))
        r2 = self._run(self._transition_params(iid, "已证实",
                                               conclusion="证据闭合"))
        self.assertEqual(r1["status"], "核查中")
        self.assertEqual(r2["status"], "已证实")
        self.assertEqual(self._row(iid)["conclusion"], "证据闭合")
        events = self._audit_events()
        self.assertEqual([e[3]["status"] for e in events],
                         ["核查中", "已证实"])
        self.assertTrue(self._chain_ok())

    # ---- AC5：不产 DuckDB 版本文件 ----
    def test_no_version_file_produced(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        self._run({"op": "add_manual", "clue_id": CLUE,
                   "text": "人工补一条方向", "operator": self.OP})
        self._run(self._transition_params(iid, "核查中"))
        self.assertEqual(self.repo.current_version("c1"), 1)
        self.assertFalse(self.factory.version_path("c1", 2).exists())

    # ---- 建议项：采纳改写（before 留原文）/ 忽略 / 重新采纳 ----
    def test_suggested_adopt_with_rewrite(self):
        text = "建议复跑 time_window_collision 核验 {subject} 资金窗"
        self._seed([{"kind": "suggested", "text": text, "origin": "suggested",
                     "status": "建议", "channel": "function",
                     "ref_function": "time_window_collision"}])
        iid = self._item_id(text, kind="suggested")
        new_text = "复跑时间窗函数核验张卫国 7 笔整数资金（采纳时改写）"
        res = self._run(self._transition_params(iid, "待核查", text=new_text))
        self.assertTrue(res["rewritten"])
        row = self._row(iid)
        self.assertEqual(row["status"], "待核查")
        self.assertEqual(row["text"], new_text)  # item_id 不变、文本已覆写
        events = self._audit_events()
        self.assertEqual(events[0][2]["text"], text)       # before 建议原文
        self.assertEqual(events[0][3]["text"], new_text)   # after 改写文本

    def test_suggested_ignore_and_readopt(self):
        text = "建议外部调取工商内档"
        self._seed([{"kind": "suggested", "text": text, "origin": "suggested",
                     "status": "建议", "channel": "external"}])
        iid = self._item_id(text, kind="suggested")
        self._run(self._transition_params(iid, "已忽略"))
        self.assertEqual(self._row(iid)["status"], "已忽略")
        self._run(self._transition_params(iid, "待核查"))
        self.assertEqual(self._row(iid)["status"], "待核查")
        self.assertTrue(self._chain_ok())

    def test_text_ignored_on_non_adoption_transition(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        self._run(self._transition_params(iid, "核查中", text="想顺手改文本"))
        row = self._row(iid)
        self.assertEqual(row["status"], "核查中")
        self.assertEqual(row["text"], INFERENCE_TEXT)  # 非采纳迁移不覆写

    # ---- 人工添加 ----
    def test_add_manual_writes_and_audits_idempotent(self):
        text = "补充核实华清越与宏业建设的股权关系"
        r1 = self._run({"op": "add_manual", "clue_id": CLUE, "text": text,
                        "operator": self.OP})
        self.assertEqual(r1["added"], 1)
        self.assertEqual(r1["status"], "待核查")
        row = self._row(r1["item_id"])
        self.assertEqual(row["kind"], "manual")
        self.assertEqual(row["origin"], "manual")
        # 同文本重复：不建行、item_id 稳定
        r2 = self._run({"op": "add_manual", "clue_id": CLUE, "text": text,
                        "operator": self.OP})
        self.assertEqual(r2["added"], 0)
        self.assertEqual(r2["item_id"], r1["item_id"])
        events = self._audit_events()
        self.assertTrue(all(e[3]["event"] == "verify_item_add_manual"
                            for e in events))

    def test_add_manual_blank_text_rejected(self):
        with self.assertRaises(TaskExecError) as cm:
            self._run({"op": "add_manual", "clue_id": CLUE, "text": "   ",
                       "operator": self.OP})
        self.assertEqual(cm.exception.code, "TEXT_REQUIRED")

    # ---- 前置/参数类 ----
    def test_unknown_op_and_no_version(self):
        with self.assertRaises(TaskExecError) as cm:
            self._run({"op": "delete", "clue_id": CLUE,
                       "operator": self.OP})
        self.assertEqual(cm.exception.code, "UNKNOWN_OP")
        # 未 BUILD 案件
        self.repo.create_case(CaseRecord(id="c2", tenant_id="t1", name="新案"))
        row = enqueue_task(self.repo, case_id="c2", task_type=TASK_VERIFY,
                           params={"op": "add_manual", "clue_id": CLUE,
                                   "text": "x", "operator": self.OP})
        with self.assertRaises(TaskExecError) as cm:
            handle_verify(row, repo=self.repo, factory=self.factory)
        self.assertEqual(cm.exception.code, "NO_VERSION")

    # ---- 终态翻案重开合法且留痕 ----
    def test_reopen_terminal_is_legal(self):
        self._seed([{"kind": "inference", "text": INFERENCE_TEXT}])
        iid = self._item_id(INFERENCE_TEXT)
        self._run(self._transition_params(iid, "已查否", conclusion="通话记录排除"))
        self._run(self._transition_params(iid, "核查中"))
        self.assertEqual(self._row(iid)["status"], "核查中")
        self.assertTrue(self._chain_ok())


# ----------------------------------------------------------------------
# REQ-V-002：核查项惰性供给（读面组装）
# ----------------------------------------------------------------------
DEMOF = ROOT / "cases" / "demoF"
DEMOF_ONTO_BASE = DEMOF / "ontology"
_DEGRADE_REASON = ("只有一个通话对端（其他对端未入库），中位数判据不可用 → "
                   "降级到绝对频次阈值")


def _write_artifact(case_dir: Path, version: int,
                    clues: list[dict]) -> Path:
    art_dir = case_dir / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    p = art_dir / f"clues_v{version}.json"
    p.write_text(json.dumps({"version": version, "clues": clues},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _synthetic_r1_raw(clue_id: str = "clue_t1") -> dict:
    """带 rule_id 的普通（非合并）线索：backfill 应原样返回。"""
    return {
        "clue_id": clue_id,
        "skill_id": "xu_shi",
        "title": "张卫国 · 季度末整数现金存入",
        "detail": {
            "依据": "与工资性非整数收支规律不符；季末时点整数现金存入",
            "rule_id": "R1",
            "rule_text": "在季度末边界前后 15 天内出现整数现金存入。",
            "级别": "待核实",
        },
        "source_rows": [{"from_raw": "张卫国", "amount": 50000,
                         "summary": "现金存入"}],
        "assumption_chain": ["H1"],
        "jian_types": ["生间"],
        "needs_human_review": True,
        "status": "待查",
        "audit_log": [],
        "note": "",
    }


class VerifyProvisionTest(unittest.TestCase):
    """REQ-V-002：三栏 → auto 核查项映射 + 合并线索规则字段回填（方案 b）。"""

    # ---- AC4/AC6：id 前缀映射，文本逐字；fact/r 卡不成项 ----
    def test_evidence_prefix_mapping_and_r_card_skipped(self):
        evidence = [
            {"id": "fX-0", "kind": "fact", "text": "事实卡不成项"},
            {"id": "iX", "kind": "inference", "text": "依据文本"},
            {"id": "dX", "kind": "pending", "text": "判据已降级：r"},
            {"id": "hX", "kind": "pending", "text": "待验证假设：H1（x）"},
            {"id": "rX", "kind": "pending", "text": "规则判据：留痕不成项"},
        ]
        items = provision_from_evidence(evidence)
        self.assertEqual(
            [(i["kind"], i["text"]) for i in items],
            [("inference", "依据文本"),
             ("pending_degrade", "判据已降级：r"),
             ("pending_hypothesis", "待验证假设：H1（x）")])

    # ---- AC6：降级卡仅 is_degraded=True 时生成 ----
    def test_degrade_card_only_when_degraded(self):
        raw = {"clue_id": "clue_dg",
               "detail": {"degrade_reason": _DEGRADE_REASON},
               "source_rows": []}
        yes = provision_for_clue(dict(raw, is_degraded=True))
        no = provision_for_clue(dict(raw, is_degraded=False))
        d_yes = [i for i in yes if i["kind"] == "pending_degrade"]
        self.assertEqual(len(d_yes), 1)
        self.assertEqual(d_yes[0]["text"], f"判据已降级：{_DEGRADE_REASON}")
        self.assertFalse(any(i["kind"] == "pending_degrade" for i in no))

    # ---- 普通线索 detail 已带 rule_id：backfill 原对象返回、不复制 ----
    def test_backfill_identity_when_rule_present(self):
        raw = _synthetic_r1_raw()
        self.assertIs(
            backfill_rule_fields(raw, pack_id="default", base_dir=None), raw)

    # ---- 零命中 / 主体列不符：fail-safe 不回填 ----
    def test_backfill_no_match_or_subject_mismatch(self):
        merged = {
            "clue_id": "clue_mx", "title": "奇正分工方案 · 某人 | 完全不相干标题",
            "detail": {"merged_from": ["clue_mx", "clue_other"]},
            "source_rows": [{"foo": 1}],
        }
        self.assertIs(backfill_rule_fields(
            merged, pack_id="default", base_dir=DEMOF_ONTO_BASE), merged)
        # 标题含 R6 但行集无 R6 subject_column（资金主体）→ 排除
        mismatch = {
            "clue_id": "clue_my",
            "title": "奇正分工方案 · 某人 | 某人 · 中标-资金时间窗碰撞",
            "detail": {"merged_from": ["clue_my", "clue_other"]},
            "source_rows": [{"foo": 1}],
        }
        self.assertIs(backfill_rule_fields(
            mismatch, pack_id="default", base_dir=DEMOF_ONTO_BASE), mismatch)

    # ---- AC5：demoF v8 合并线索经规则目录反查回填 R6，供出 2 个 auto 项 ----
    def test_backfill_demof_v8_merged_r6(self):
        art = json.loads(
            (DEMOF / "artifacts" / "clues_v8.json").read_text(encoding="utf-8"))
        raw = next(c for c in art["clues"] if c["clue_id"] == CLUE)
        self.assertNotIn("rule_id", raw["detail"])  # 前提：现状产物丢字段

        filled = backfill_rule_fields(
            raw, pack_id="default", base_dir=DEMOF_ONTO_BASE)
        self.assertIsNot(filled, raw)
        # artifact 原对象不被改写（ADR-V-5）
        self.assertNotIn("rule_id", raw["detail"])
        self.assertEqual(filled["detail"]["rule_id"], "R6")
        self.assertEqual(filled["detail"]["依据"], INFERENCE_TEXT)
        self.assertIn("lnk_time_window", filled["detail"]["rule_text"])

        items = provision_for_clue(
            raw, pack_id="default", base_dir=DEMOF_ONTO_BASE)
        self.assertEqual(
            [(i["kind"], i["text"]) for i in items],
            [("inference", INFERENCE_TEXT),
             ("pending_hypothesis", HYPOTHESIS_TEXT)])
        ids = sorted(f"vi_{StateStore.verify_item_key(CLUE, i['kind'], i['text'])}"
                     for i in items)
        self.assertEqual(ids, ["vi_6a7a9feea147f413",
                               "vi_d0577e14036a1c2e"])


class VerifyProvisionViewTest(unittest.TestCase):
    """REQ-V-002：assemble_detail 读面供给（state 在才写、只补缺）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.case_dir = self.tmp / "c1"
        self.access = AccessContext(
            operator="王峰", role="正兵", clearance=3, case_id="c1",
            purpose="REQ-V-002 测试", network="web")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _detail(self, **kw):
        return assemble_detail(
            case_dir=self.case_dir, version=kw.pop("version", 1),
            clue_id=kw.pop("clue_id", "clue_t1"), state_map={},
            access=self.access, **kw)

    # ---- AC1：首次详情 → state 出现 auto 行 + verify 进度 ----
    def test_detail_provisions_auto_items(self):
        raw = _synthetic_r1_raw()
        _write_artifact(self.case_dir, 1, [raw])
        with StateStore("c1", self.case_dir / "state.sqlite") as st:
            data = self._detail(state_store=st)
        self.assertIn("verify", data)
        items = data["verify"]["items"]
        self.assertEqual({i["kind"] for i in items},
                         {"inference", "pending_hypothesis"})
        self.assertTrue(all(i["origin"] == "auto" for i in items))
        self.assertEqual(data["verify"]["progress"]["pending"], 2)
        # 三栏证据同源回显：推断文本逐字
        inf = [e for e in data["evidence"] if e["kind"] == "inference"]
        self.assertEqual([e["text"] for e in inf],
                         [raw["detail"]["依据"]])

    # ---- AC2：人裁结论后再次供给：不新增行、不覆盖状态/结论 ----
    def test_reprovision_keeps_human_conclusion(self):
        _write_artifact(self.case_dir, 1, [_synthetic_r1_raw()])
        with StateStore("c1", self.case_dir / "state.sqlite") as st:
            first = self._detail(state_store=st)
            inf = next(i for i in first["verify"]["items"]
                       if i["kind"] == "inference")
            st.transition_verify_item(
                inf["item_id"], status="已证实", conclusion="复核无误",
                operator="王峰", updated_at="2026-09-11 10:00:00")
            second = self._detail(state_store=st)
        rows = {i["kind"]: i for i in second["verify"]["items"]}
        self.assertEqual(len(second["verify"]["items"]), 2)
        self.assertEqual(rows["inference"]["status"], "已证实")
        self.assertEqual(rows["inference"]["conclusion"], "复核无误")
        self.assertEqual(rows["inference"]["operator"], "王峰")
        self.assertEqual(second["verify"]["progress"]["concluded"], 1)
        self.assertEqual(second["verify"]["progress"]["pending"], 1)

    # ---- AC3：无 state.sqlite → 供给跳过、无 verify 键、不建文件 ----
    def test_no_state_skips_provision(self):
        _write_artifact(self.case_dir, 1, [_synthetic_r1_raw()])
        data = self._detail(state_store=None)
        self.assertNotIn("verify", data)
        self.assertIn("evidence", data)  # 三栏证据不受影响
        self.assertFalse((self.case_dir / "state.sqlite").exists())

    # ---- AC5：demoF v8 合并线索读面回归（2 个 auto 项 + item_id 锁定）----
    def test_detail_demof_v8_merged_regression(self):
        art_dir = self.case_dir / "artifacts"
        art_dir.mkdir(parents=True)
        shutil.copy(DEMOF / "artifacts" / "clues_v8.json",
                    art_dir / "clues_v8.json")
        with StateStore("c1", self.case_dir / "state.sqlite") as st:
            data = assemble_detail(
                case_dir=self.case_dir, version=8, clue_id=CLUE,
                state_map={}, access=self.access, pack_id="default",
                base_dir=DEMOF_ONTO_BASE, state_store=st)
        items = data["verify"]["items"]
        self.assertEqual(
            [(i["kind"], i["item_id"]) for i in items],
            [("inference", "vi_d0577e14036a1c2e"),
             ("pending_hypothesis", "vi_6a7a9feea147f413")])
        self.assertEqual(data["verify"]["progress"]["pending"], 2)
        # 三栏回显：回填后推断/待核实出现；r 卡只留痕、不成项
        ekinds = [e["kind"] for e in data["evidence"]]
        self.assertIn("inference", ekinds)
        pending_texts = [e["text"] for e in data["evidence"]
                         if e["kind"] == "pending"]
        self.assertTrue(any(t.startswith("规则判据：") for t in pending_texts))
        self.assertFalse(
            any(i["kind"] == "pending_rule" for i in items))


if __name__ == "__main__":
    unittest.main()
