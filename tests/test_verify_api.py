"""
tests/test_verify_api.py
REQ-V-005 核查项 API 端到端（读同步、写 202 入队 TASK_VERIFY）。

AC 对应（实施方案 REQ-V-005 验收标准）：
  AC-1 GET 返回供给后的清单与 progress；无 state 时 available:false；
  AC-2 POST transitions 返回 202+task_id，Worker 消费后 GET 可见新状态；
  AC-3 缺 next_status / 非法值 → 400（ERR_VALIDATION）；
  AC-4 跨租户访问他人案件 → 404；未登录 → 401；
  AC-5 相同 idem_key 重复提交不产生重复任务行。
另含：人工添加/空文本 400、采纳建议项改写文本、非法迁移/缺结论/不存在
item 在 Worker 兜底失败（VERIFY_REJECTED/CONCLUSION_REQUIRED/ITEM_NOT_FOUND）、
响应字段投影（external 往返、内部列不外泄）、审计链签名。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from core.registry import ClueStatus, LineageClue

from server.app.cases import CaseService
from server.app.clues_artifact import save_case_clues
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore
from server.app.worker.pool import WorkerPool


def _clue(cid: str = "clue-1", title: str = "测试线索") -> LineageClue:
    return LineageClue(clue_id=cid, skill_id="xu_shi", title=title,
                       jian_types=["生间"],
                       source_rows=[{"file": "b.parquet", "row_id": 1}])


class VerifyApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.app = create_app(self.ctx)
        self.client = TestClient(self.app)
        salt, h = hash_password("pw-pro")
        self.repo.create_user(User(operator="王检察官", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        salt2, h2 = hash_password("pw-sol")
        self.repo.create_user(User(operator="李侦查员", password_hash=h2,
                                   salt=salt2, role="正兵", clearance=1,
                                   tenant_id="t1"))
        salt3, h3 = hash_password("pw-li")
        self.repo.create_user(User(operator="李检", password_hash=h3,
                                   salt=salt3, role="正兵", clearance=1,
                                   tenant_id="t2"))
        # Worker 不自动 start：run_until_drained 确定性驱动
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        self.auth_l = self._login("李检", "pw-li")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def make_case(self, case_id: str = "c1",
                  clues: list[LineageClue] | None = None,
                  version: int = 1) -> None:
        """建案 + 版本指针 + 线索产物（不产 DuckDB 版本文件，核查只读 state）。"""
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": case_id, "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version(case_id, version, "test")
        save_case_clues(self.factory.case_dir(case_id), version,
                        clues or [_clue()])

    def _drain(self) -> None:
        self.pool.run_until_drained(max_idle_rounds=5)

    def _state(self, case_id: str = "c1") -> StateStore:
        return StateStore(case_id,
                          self.factory.case_dir(case_id) / "state.sqlite")

    def _get_items(self, case_id: str = "c1", clue_id: str = "clue-1",
                   headers: dict | None = None):
        return self.client.get(
            f"/api/v1/cases/{case_id}/clues/{clue_id}/verify-items",
            headers=headers or self.auth_h)

    def _post_item(self, body: dict, case_id: str = "c1",
                   clue_id: str = "clue-1", headers: dict | None = None):
        return self.client.post(
            f"/api/v1/cases/{case_id}/clues/{clue_id}/verify-items",
            headers=headers or self.auth_h, json=body)

    def _post_transition(self, item_id: str, body: dict,
                         case_id: str = "c1", clue_id: str = "clue-1",
                         headers: dict | None = None):
        return self.client.post(
            f"/api/v1/cases/{case_id}/clues/{clue_id}"
            f"/verify-items/{item_id}/transitions",
            headers=headers or self.auth_h, json=body)

    def _seed_items(self, items: list[dict], case_id: str = "c1",
                    clue_id: str = "clue-1") -> None:
        """直接写 state 造核查项（绕过供给，用于裁决/建议项路由测试）。"""
        state = self._state(case_id)
        try:
            state.upsert_verify_items(case_id, clue_id, items)
        finally:
            state.close()

    def _seed_one(self, *, text: str = "待核实事项甲",
                  kind: str = "manual", origin: str = "manual",
                  status: str = "待核查", **extra) -> str:
        payload = {"kind": kind, "text": text, "origin": origin,
                   "status": status}
        payload.update(extra)
        self._seed_items([payload])
        state = self._state()
        try:
            rows = state.list_verify_items("clue-1")
        finally:
            state.close()
        return next(r["item_id"] for r in rows if r["text"] == text)

    def _verify_tasks(self, case_id: str = "c1"):
        return [t for t in self.repo.list_tasks(case_id=case_id)
                if t.task_type == "VERIFY"]

    def _latest_verify_task(self, resp, case_id: str = "c1"):
        # list_tasks 为 ORDER BY created_at DESC，而 created_at 仅秒级精度，
        # 多任务同秒并列时排序不确定——必须按入队响应的任务 ID 精确定位。
        t = self.repo.get_task(resp.json()["data"]["id"])
        self.assertIsNotNone(t, "应有 VERIFY 任务")
        self.assertEqual(t.task_type, "VERIFY")
        return t

    # ------------------------------------------------------------------
    # REQ-V-008 辅助：DISPOSE 提交/任务/线索状态
    # ------------------------------------------------------------------
    def _post_action(self, body: dict, case_id: str = "c1",
                     clue_id: str = "clue-1", headers: dict | None = None):
        return self.client.post(
            f"/api/v1/cases/{case_id}/clues/{clue_id}/actions",
            headers=headers or self.auth_h, json=body)

    def _latest_dispose_task(self, resp, case_id: str = "c1"):
        # 同 _latest_verify_task：秒级 created_at 下不能靠列表排序猜最新。
        t = self.repo.get_task(resp.json()["data"]["id"])
        self.assertIsNotNone(t, "应有 DISPOSE 任务")
        self.assertEqual(t.task_type, "DISPOSE")
        return t

    def _clue_disposal_status(self, case_id: str = "c1",
                              clue_id: str = "clue-1"):
        state = self._state(case_id)
        try:
            row = state.conn.execute(
                "SELECT status FROM clue_disposal_status "
                "WHERE clue_id=?", [clue_id]).fetchone()
            return row[0] if row is not None else None
        finally:
            state.close()

    def _start_verifying(self):
        """待查 → 查证中（confirm 的 only_from 前置）。"""
        r = self._post_action({"action": "verify", "note": "接手核查"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        self.assertEqual(self._latest_dispose_task(r).status, TASK_SUCCEEDED)

    # ------------------------------------------------------------------
    # AC-1：GET 清单/progress/available
    # ------------------------------------------------------------------
    def test_get_without_state_available_false(self):
        # 建案 API 会补录 case_created 生命周期事件（state.sqlite 随之创建）；
        # available:false 的真实场景是 state 文件缺失——直接走 CaseService
        # 造案（不经建案路由）+ 版本指针 + 线索产物
        case_id = "c0"
        self.svc.create_case(case_id=case_id, name="无 state 案",
                             tenant_id="t1", created_by="王检察官")
        self.repo.set_version(case_id, 1, "test")
        save_case_clues(self.factory.case_dir(case_id), 1, [_clue()])
        state_path = self.factory.case_dir(case_id) / "state.sqlite"
        self.assertFalse(state_path.exists())  # 前置：文件确实不存在

        r = self._get_items(case_id=case_id)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertFalse(data["available"])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["progress"]["total"], 0)
        self.assertEqual(data["progress"]["by_status"], {})
        # 读面不创建 state.sqlite
        self.assertFalse(state_path.exists())

    def test_get_empty_items_when_state_exists(self):
        # 常规建案（state.sqlite 随生命周期事件创建）但无核查项：
        # available:true、空清单与零 progress
        self.make_case()
        r = self._get_items()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(data["available"])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["progress"]["total"], 0)

    def test_get_returns_items_and_progress(self):
        self.make_case()
        self._seed_items([
            {"kind": "manual", "text": "事项甲", "origin": "manual"},
            {"kind": "manual", "text": "事项乙", "origin": "manual",
             "status": "已证实"},
            {"kind": "suggested", "text": "建议丙", "origin": "suggested",
             "status": "建议"},
        ])
        r = self._get_items()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(data["available"])
        self.assertEqual([it["text"] for it in data["items"]],
                         ["事项甲", "事项乙", "建议丙"])  # 状态展示序
        p = data["progress"]
        self.assertEqual(p["total"], 3)
        self.assertEqual(p["pending"], 1)
        self.assertEqual(p["concluded"], 1)
        self.assertEqual(p["suggested"], 1)
        self.assertEqual(p["by_status"]["已证实"], 1)

    def test_get_field_projection_and_external_roundtrip(self):
        self.make_case()
        self._seed_items([{
            "kind": "suggested", "text": "调取水单", "origin": "suggested",
            "status": "建议", "channel": "external",
            "external": {"target": "海州银行", "material": "账户流水"},
            "falsification": "有合法对价则证伪"}])
        data = self._get_items().json()["data"]
        it = data["items"][0]
        # 方案契约字段齐全
        self.assertEqual(set(it), {
            "item_id", "kind", "text", "origin", "status", "conclusion",
            "operator", "updated_at", "channel", "ref_function",
            "external", "falsification"})
        # 内部列不外泄
        self.assertNotIn("item_key", it)
        self.assertNotIn("case_id", it)
        self.assertNotIn("clue_id", it)
        # external_json 往返为对象
        self.assertEqual(it["external"],
                         {"target": "海州银行", "material": "账户流水"})
        self.assertEqual(it["falsification"], "有合法对价则证伪")

    # ------------------------------------------------------------------
    # AC-2：写 202 + Worker 消费后状态可见
    # ------------------------------------------------------------------
    def test_add_manual_then_visible(self):
        self.make_case()
        r = self._post_item({"text": "调取张某 3 月流水"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["data"]["task_type"], "VERIFY")
        self._drain()
        self.assertEqual(self._latest_verify_task(r).status, TASK_SUCCEEDED)
        data = self._get_items().json()["data"]
        self.assertEqual(len(data["items"]), 1)
        it = data["items"][0]
        self.assertEqual(it["text"], "调取张某 3 月流水")
        self.assertEqual(it["kind"], "manual")
        self.assertEqual(it["origin"], "manual")
        self.assertEqual(it["status"], "待核查")

    def test_transition_flow_and_audit_chain(self):
        self.make_case()
        item_id = self._seed_one(text="事项甲")
        # 待核查 → 核查中
        r = self._post_transition(item_id, {"next_status": "核查中"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        # 核查中 → 已证实（带结论）
        r = self._post_transition(
            item_id, {"next_status": "已证实",
                      "conclusion": "流水与中标公告时间耦合，予以证实"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        self.assertEqual(self._latest_verify_task(r).status, TASK_SUCCEEDED)
        data = self._get_items().json()["data"]
        it = data["items"][0]
        self.assertEqual(it["status"], "已证实")
        self.assertEqual(it["operator"], "王检察官")
        self.assertIn("时间耦合", it["conclusion"])
        self.assertEqual(data["progress"]["concluded"], 1)
        self.assertEqual(data["progress"]["pending"], 0)
        # 两条迁移事件落链且签名可验
        state = self._state()
        try:
            self.assertTrue(state.chain_verify())
            rows = state.conn.execute(
                "SELECT json_extract(after_state,'$.event') AS event "
                "FROM audit_chain "
                "WHERE json_extract(after_state,'$.event') "
                "LIKE 'verify_item_%' ORDER BY seq").fetchall()
            events = [r["event"] for r in rows]
            self.assertEqual(events, ["verify_item_transition",
                                      "verify_item_transition"])
        finally:
            state.close()

    def test_suggested_adopt_with_rewrite(self):
        """采纳建议项（建议→待核查）携带 text=改写文本。"""
        self.make_case()
        item_id = self._seed_one(
            text="调取{subject}账户流水", kind="suggested",
            origin="suggested", status="建议", channel="function",
            ref_function="time_window_collision")
        r = self._post_transition(
            item_id, {"next_status": "待核查",
                      "text": "调取张某账户在窗口期的完整流水"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        self.assertEqual(self._latest_verify_task(r).status, TASK_SUCCEEDED)
        it = self._get_items().json()["data"]["items"][0]
        self.assertEqual(it["status"], "待核查")
        self.assertEqual(it["text"], "调取张某账户在窗口期的完整流水")
        # item_id 稳定（ADR-V-4），路由字段不丢
        self.assertEqual(it["item_id"], item_id)
        self.assertEqual(it["ref_function"], "time_window_collision")

    def test_text_ignored_for_non_adopt_transition(self):
        """非采纳迁移携带 text：Worker 忽略，不覆写原文本。"""
        self.make_case()
        item_id = self._seed_one(text="事项甲")
        r = self._post_transition(
            item_id, {"next_status": "核查中", "text": "想改文本"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        it = self._get_items().json()["data"]["items"][0]
        self.assertEqual(it["status"], "核查中")
        self.assertEqual(it["text"], "事项甲")

    # ------------------------------------------------------------------
    # AC-3：参数形状 400（Worker 兜底另测）
    # ------------------------------------------------------------------
    def test_add_empty_text_400(self):
        self.make_case()
        r = self._post_item({"text": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        self.assertEqual(self._verify_tasks(), [])

    def test_transition_missing_next_status_400(self):
        self.make_case()
        item_id = self._seed_one()
        r = self._post_transition(item_id, {"conclusion": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        self.assertEqual(self._verify_tasks(), [])

    def test_transition_illegal_status_400(self):
        self.make_case()
        item_id = self._seed_one()
        r = self._post_transition(item_id, {"next_status": "已起飞"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        self.assertEqual(self._verify_tasks(), [])

    # ------------------------------------------------------------------
    # Worker 兜底：非法迁移 / 缺结论 / 不存在 item / agent 红线
    # ------------------------------------------------------------------
    def test_worker_rejects_invalid_transition(self):
        self.make_case()
        item_id = self._seed_one()  # 待核查
        r = self._post_transition(item_id, {"next_status": "已忽略"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_verify_task(r)
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "VERIFY_REJECTED")

    def test_worker_conclusion_required(self):
        self.make_case()
        item_id = self._seed_one()  # 待核查
        r = self._post_transition(item_id, {"next_status": "已证实"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_verify_task(r)
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "CONCLUSION_REQUIRED")

    def test_worker_item_not_found(self):
        self.make_case()
        r = self._post_transition("vi_ghost", {"next_status": "核查中"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_verify_task(r)
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "ITEM_NOT_FOUND")

    # ------------------------------------------------------------------
    # AC-5：幂等
    # ------------------------------------------------------------------
    def test_add_idempotency_same_task(self):
        self.make_case()
        body = {"text": "同一核查事项"}
        r1 = self._post_item(body)
        r2 = self._post_item(body)
        self.assertEqual(r1.json()["data"]["id"], r2.json()["data"]["id"])
        self._drain()
        self.assertEqual(len(self._verify_tasks()), 1)
        # 消费后仍只落一项
        self.assertEqual(
            len(self._get_items().json()["data"]["items"]), 1)

    def test_transition_idempotency_same_task(self):
        self.make_case()
        item_id = self._seed_one()
        body = {"next_status": "核查中"}
        r1 = self._post_transition(item_id, body)
        r2 = self._post_transition(item_id, body)
        self.assertEqual(r1.json()["data"]["id"], r2.json()["data"]["id"])
        self._drain()
        self.assertEqual(len(self._verify_tasks()), 1)

    # ------------------------------------------------------------------
    # AC-4：跨租户 404 / 无 token 401
    # ------------------------------------------------------------------
    def test_cross_tenant_get_404(self):
        self.make_case()
        r = self._get_items(headers=self.auth_l)
        self.assertEqual(r.status_code, 404)

    def test_cross_tenant_add_404(self):
        self.make_case()
        r = self._post_item({"text": "x"}, headers=self.auth_l)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._verify_tasks(), [])

    def test_cross_tenant_transition_404(self):
        self.make_case()
        r = self._post_transition("vi_x", {"next_status": "核查中"},
                                  headers=self.auth_l)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._verify_tasks(), [])

    def test_no_token_401(self):
        self.make_case()
        r = self.client.get(
            "/api/v1/cases/c1/clues/clue-1/verify-items")
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            "/api/v1/cases/c1/clues/clue-1/verify-items",
            json={"text": "x"})
        self.assertEqual(r.status_code, 401)

    # ------------------------------------------------------------------
    # 正兵可写（agent 红线无法经会话注入，Worker 层用例见 test_verify_item）
    # ------------------------------------------------------------------
    def test_soldier_add_and_transition_ok(self):
        self.make_case()
        r = self._post_item({"text": "正兵提的核查项"}, headers=self.auth_s)
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        item_id = self._get_items().json()["data"]["items"][0]["item_id"]
        r = self._post_transition(
            item_id, {"next_status": "核查中"}, headers=self.auth_s)
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        self.assertEqual(self._latest_verify_task(r).status, TASK_SUCCEEDED)
        self.assertEqual(
            self._get_items().json()["data"]["items"][0]["operator"],
            "李侦查员")

    # ------------------------------------------------------------------
    # REQ-V-008：固证/排除核查门禁（Worker VERIFY_PENDING 拦截）
    # ------------------------------------------------------------------
    def test_confirm_blocked_when_pending_items(self):
        """AC-1：pending>0 时 confirm → FAILED/VERIFY_PENDING，状态仍查证中，
        审计链无固证迁移事件。"""
        self.make_case()
        self._seed_items([
            {"kind": "manual", "text": "未结事项甲", "origin": "manual",
             "status": "待核查"},
            {"kind": "manual", "text": "未结事项乙", "origin": "manual",
             "status": "核查中"},
            {"kind": "manual", "text": "已结事项丙", "origin": "manual",
             "status": "已证实"},
        ])
        self._start_verifying()  # → 查证中

        r = self._post_action({"action": "confirm", "note": "申请固证"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_dispose_task(r)
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "VERIFY_PENDING")
        # 错误信息含未结数与前项文本（可操作提示）
        self.assertIn("2 项核查未结", t.error_message)
        self.assertIn("未结事项甲", t.error_message)
        self.assertIn("未结事项乙", t.error_message)
        # 状态未推进；审计链只有 lifecycle + verify 两事件且链完整
        self.assertEqual(self._clue_disposal_status(), ClueStatus.VERIFYING)
        state = self._state()
        try:
            self.assertEqual(state.event_count(), 2)
            self.assertTrue(state.chain_verify())
        finally:
            state.close()

    def test_exclude_blocked_when_pending_items(self):
        """AC-4：exclude 同门禁（待查态可直接排除，门禁先于动作执行）。"""
        self.make_case()
        self._seed_items([
            {"kind": "manual", "text": "待排除前须了结", "origin": "manual"},
        ])
        r = self._post_action(
            {"action": "exclude", "reason": "经查不实"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_dispose_task(r)
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "VERIFY_PENDING")
        # 被拒动作不产生处置状态行
        self.assertIsNone(self._clue_disposal_status())
        state = self._state()
        try:
            self.assertEqual(state.event_count(), 1)  # lifecycle only
        finally:
            state.close()

    def test_gate_passes_after_all_concluded(self):
        """AC-2：全部得出结论（含无法核实）后 confirm 成功。"""
        self.make_case()
        self._seed_items([
            {"kind": "manual", "text": "事项甲", "origin": "manual",
             "status": "已证实"},
            {"kind": "manual", "text": "事项乙", "origin": "manual",
             "status": "已查否"},
            {"kind": "manual", "text": "事项丙", "origin": "manual",
             "status": "无法核实"},
        ])
        self._start_verifying()
        r = self._post_action({"action": "confirm", "note": "证据固定"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_dispose_task(r)
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"{t.error_code} {t.error_message}")
        self.assertEqual(self._clue_disposal_status(), ClueStatus.CONFIRMED)

    def test_gate_zero_items_compat(self):
        """AC-3：total=0 的线索 confirm 行为与门禁上线前完全一致。"""
        self.make_case()
        self._start_verifying()
        r = self._post_action({"action": "confirm", "note": "无核查项固证"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_dispose_task(r)
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"{t.error_code} {t.error_message}")
        self.assertEqual(self._clue_disposal_status(), ClueStatus.CONFIRMED)

    def test_gate_ignores_suggested_and_ignored_items(self):
        """REQ-V-018 口径：建议/已忽略是未采纳旁路态，从不阻塞固证。"""
        self.make_case()
        self._seed_items([
            {"kind": "suggested", "text": "建议丁", "origin": "suggested",
             "status": "建议"},
            {"kind": "manual", "text": "建议戊", "origin": "manual",
             "status": "已忽略"},
        ])
        self._start_verifying()
        r = self._post_action({"action": "confirm", "note": "不理睬建议直接固证"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_dispose_task(r)
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"{t.error_code} {t.error_message}")
        self.assertEqual(self._clue_disposal_status(), ClueStatus.CONFIRMED)

    def test_verify_and_reset_unaffected_by_pending_items(self):
        """AC-4：file/verify/reset 不经门禁（pending 项下 verify/reset 正常）。"""
        self.make_case()
        self._seed_items([
            {"kind": "manual", "text": "未结事项甲", "origin": "manual"},
        ])
        # verify：待查 → 查证中
        self._start_verifying()
        # reset：查证中 → 待查
        r = self._post_action({"action": "reset", "note": "退回补查"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_dispose_task(r)
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"{t.error_code} {t.error_message}")
        self.assertEqual(self._clue_disposal_status(), ClueStatus.PENDING)


if __name__ == "__main__":
    unittest.main()
