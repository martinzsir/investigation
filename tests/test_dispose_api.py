"""
tests/test_dispose_api.py
M3 阶段 B：W-020 线索处置与状态机（DISPOSE 快速通道端到端）。

AC 对应：
  AC-1 五动作界面触发，产物与代码调用等价（经 ActionExecutor 落 state）；
  AC-2 正兵触发 file 被拒（API 403 前置 + Worker ACTION_FORBIDDEN 兜底）；
  AC-3 file 缺 legal_basis 拒绝（API 400 + Worker ACTION_REJECTED 兜底）；
  AC-4 file 跳过中间态（待查→已立案）拒绝（状态机在 core）；
  AC-5 占位 operator 拒绝（statesink 组已覆盖 core 层；API operator 取会话
       无法注入，本模块不重复）；
  AC-6 每个处置动作落持久审计链（state.audit_chain，非内存日志）；
  AC-7 处置后线索状态与 state 真值一致（clue_disposal_status）。
另含：幂等键重复提交同一任务；exclude 缺 reason 拒绝；跨租户 404；
无 token 401；未 BUILD 案件处置失败 NO_VERSION。
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
from server.app.meta.models import (
    TASK_FAILED,
    TASK_SUCCEEDED,
    User,
)
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore
from server.app.worker.pool import WorkerPool


def _clue(cid: str, title: str = "测试线索") -> LineageClue:
    return LineageClue(clue_id=cid, skill_id="xu_shi", title=title,
                       jian_types=["生间"],
                       source_rows=[{"file": "b.parquet", "row_id": 1}])


class DisposeApiTest(unittest.TestCase):
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
        # human（检察官）与正兵（侦查员）两个会话
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
        # Worker 不自动 start：用 run_until_drained 确定性驱动
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

    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def make_case_with_clues(self, case_id: str = "c1",
                             clues: list[LineageClue] | None = None,
                             version: int = 1) -> None:
        """建案 + 版本指针 + 线索产物（不产 DuckDB 版本文件——处置只读 state）。"""
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": case_id, "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version(case_id, version, "test")
        save_case_clues(self.factory.case_dir(case_id), version,
                        clues or [_clue("clue-1", "大额取现")])

    def _post_action(self, case_id: str, clue_id: str, body: dict,
                     headers: dict | None = None):
        return self.client.post(
            f"/api/v1/cases/{case_id}/clues/{clue_id}/actions",
            headers=headers or self.auth_h, json=body)

    def _drain(self) -> None:
        self.pool.run_until_drained(max_idle_rounds=5)

    def _state(self, case_id: str = "c1") -> StateStore:
        return StateStore(case_id,
                          self.factory.case_dir(case_id) / "state.sqlite")

    def _latest_task(self, case_id: str = "c1"):
        tasks = self.repo.list_tasks(case_id=case_id)
        self.assertTrue(tasks, "应有任务")
        return tasks[-1]

    # ------------------------------------------------------------------
    # AC-1/6/7：全链路 verify → confirm → file（human）
    # ------------------------------------------------------------------
    def test_full_chain_verify_confirm_file(self):
        self.make_case_with_clues("c1")
        # verify
        r = self._post_action("c1", "clue-1", {"action": "verify",
                                               "note": "接手核查"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_task("c1")
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"verify 应成功：{t.error_code} {t.error_message}")
        state = self._state("c1")
        try:
            self.assertEqual(state.event_count(), 2)  # lifecycle + verify
            self.assertTrue(state.chain_verify())
            row = state.conn.execute(
                "SELECT status FROM clue_disposal_status "
                "WHERE clue_id=?", ["clue-1"]).fetchone()
            self.assertEqual(row[0], ClueStatus.VERIFYING)  # AC-7
        finally:
            state.close()
        # confirm
        r = self._post_action("c1", "clue-1", {"action": "confirm",
                                               "note": "证据固定"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        # file（human + legal_basis）
        r = self._post_action("c1", "clue-1",
                              {"action": "file",
                               "legal_basis": "海州检刑立〔2026〕12号"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_task("c1")
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"file 应成功：{t.error_code} {t.error_message}")
        state = self._state("c1")
        try:
            self.assertEqual(state.event_count(), 4)  # AC-6: lifecycle + verify + confirm + file
            self.assertTrue(state.chain_verify())
            # AC-7：状态真值=已立案；决策副作用落 review_decision
            row = state.conn.execute(
                "SELECT status FROM clue_disposal_status "
                "WHERE clue_id=?", ["clue-1"]).fetchone()
            self.assertEqual(row[0], ClueStatus.FILED)
            dec = state.list_decisions(target_id="clue-1")
            self.assertEqual(len(dec), 1)
            self.assertEqual(dec[0]["verdict"], ClueStatus.FILED)
            self.assertEqual(dec[0]["decided_by"], "王检察官")
        finally:
            state.close()

    # ------------------------------------------------------------------
    # AC-2：正兵 file → API 403（Worker 兜底见 test_soldier_file_forbidden_worker）
    # ------------------------------------------------------------------
    def test_soldier_file_denied_at_api(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1",
                              {"action": "file", "legal_basis": "某文号"},
                              headers=self.auth_s)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "FORBIDDEN")
        # 未入队
        self.assertEqual(self.repo.list_tasks(case_id="c1"), [])

    def test_soldier_verify_ok(self):
        """正兵可做 verify/exclude/confirm（仅 file 是 human 专属）。"""
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1", {"action": "verify"},
                              headers=self.auth_s)
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        self.assertEqual(self._latest_task("c1").status, TASK_SUCCEEDED)

    # ------------------------------------------------------------------
    # AC-3：file 缺 legal_basis → 400
    # ------------------------------------------------------------------
    def test_file_without_legal_basis_denied(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1", {"action": "file"},
                              headers=self.auth_h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")

    # ------------------------------------------------------------------
    # AC-4：待查 → 已立案（跳态）→ Worker ACTION_REJECTED
    # ------------------------------------------------------------------
    def test_file_skip_state_rejected_by_worker(self):
        self.make_case_with_clues("c1")
        # human 直 file（API 校验 legal_basis/角色都过，状态机在 core 拦截）
        r = self._post_action("c1", "clue-1",
                              {"action": "file",
                               "legal_basis": "海州检刑立〔2026〕12号"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_task("c1")
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "ACTION_REJECTED")
        # 被拒动作不落链
        state = self._state("c1")
        try:
            self.assertEqual(state.event_count(), 1)  # lifecycle event only, rejected action不落链
        finally:
            state.close()

    # ------------------------------------------------------------------
    # exclude 必须 reason（core 参数校验；空 reason → ACTION_REJECTED）
    # ------------------------------------------------------------------
    def test_exclude_without_reason_rejected(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1", {"action": "exclude"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_task("c1")
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "ACTION_REJECTED")

    def test_exclude_with_reason_ok(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1",
                              {"action": "exclude", "reason": "经查不实"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._latest_task("c1")
        self.assertEqual(t.status, TASK_SUCCEEDED,
                         f"{t.error_code} {t.error_message}")

    # ------------------------------------------------------------------
    # 非法动作名 / 未知线索 / 未 BUILD
    # ------------------------------------------------------------------
    def test_unknown_action_400(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1", {"action": "promote"})
        self.assertEqual(r.status_code, 400)

    def test_unknown_clue_failed(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-ghost", {"action": "verify"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_task("c1")
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "CLUE_NOT_FOUND")

    def test_no_version_failed(self):
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c2", "name": "空案"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self._post_action("c2", "clue-1", {"action": "verify"})
        self.assertEqual(r.status_code, 202)
        self._drain()
        t = self._latest_task("c2")
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "NO_VERSION")

    # ------------------------------------------------------------------
    # 幂等：同 idem_key 重复提交 → 同一任务
    # ------------------------------------------------------------------
    def test_idempotency_same_task(self):
        self.make_case_with_clues("c1")
        body = {"action": "verify", "idem_key": "k-001"}
        r1 = self._post_action("c1", "clue-1", body)
        r2 = self._post_action("c1", "clue-1", body)
        self.assertEqual(r1.json()["data"]["id"], r2.json()["data"]["id"])
        self._drain()
        tasks = [t for t in self.repo.list_tasks(case_id="c1")
                 if t.task_type == "DISPOSE"]
        self.assertEqual(len(tasks), 1)

    # ------------------------------------------------------------------
    # 跨租户 404 / 无 token 401
    # ------------------------------------------------------------------
    def test_cross_tenant_404(self):
        self.make_case_with_clues("c1")
        r = self._post_action("c1", "clue-1", {"action": "verify"},
                              headers=self.auth_l)
        self.assertEqual(r.status_code, 404)

    def test_no_token_401(self):
        self.make_case_with_clues("c1")
        r = self.client.post(
            "/api/v1/cases/c1/clues/clue-1/actions",
            json={"action": "verify"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
