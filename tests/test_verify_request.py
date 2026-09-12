"""
tests/test_verify_request.py
核查工作区 REQ-V-012/013：调取清单台账（verify_request CRUD/状态机/超期读面
派生 + 台账 API 202 闭环）—— 测试组 verifyreq。

REQ-V-012 断言（VerifyRequestTest，state 层；实施方案验收 1~3）：
  1. 状态非法迁移被拒（core.verify_machine.validate_request_transition）；
     材料已回可关闭：待发起→已发起→材料已回→关闭 全链合法；
  2. due_date 过期的「已发起」请求在 list 响应中 overdue:true，
     存储 status 不变（超期是读面派生，永不落库、免定时任务）；
  3. CRUD 幂等（表幂等建、ID 唯一、未知 request_id 更新返回 None、
     线索隔离）；审计链由 Worker op 落，本组数据面断言。

REQ-V-013 断言（RequestApiTest，TestClient 造案 + WorkerPool 确定性驱动）：
  AC-1 创建→发起→回执→关闭全链路 202 任务流转，GET 反映终态；
       audit_chain 落 verify_request_create/verify_request_transition；
  AC-2 overdue 徽标经 API 透出（存储 status 不变）；
  AC-3 跨租户 404；未登录 401；agent:* 403（台账是人的催办行为，
       且不产生任务行）；
  另含：非法迁移 Worker 兜底 FAILED REQUEST_REJECTED、API 形状 400
  （target/material/next_status 缺失、due_date/状态非法值）、
  GET 过滤（clue_id/status）与无 state 时 available:false。

请求状态机（verify_machine）：待发起→已发起→材料已回|关闭；材料已回→关闭。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from core.registry import LineageClue
from core.verify_machine import (
    ERR_INVALID_TRANSITION,
    ERR_UNKNOWN_STATUS,
    R_CLOSED,
    R_DRAFT,
    R_RETURNED,
    R_SENT,
    REQUEST_STATUSES,
    VerifyTransitionError,
    can_request_transition,
    validate_request_transition,
)
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


def _ts(days_ago: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)) \
        .isoformat(timespec="seconds")


def _date(days_ago: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)) \
        .strftime("%Y-%m-%d")


class VerifyRequestTest(unittest.TestCase):
    """REQ-V-012 台账数据面（state 层，无 API/Worker 参与）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "state.sqlite"
        self.st = StateStore("c1", self.db)

    def tearDown(self):
        self.st.close()

    def _insert(self, **kw) -> dict:
        params = dict(case_id="c1", clue_id="clue-1",
                      target="海州银行营业部",
                      material="2025 年 1-6 月账户流水",
                      created_by="王检察官", created_at=_ts())
        params.update(kw)
        return self.st.insert_verify_request(**params)

    # ------------------------------------------------------------------
    # 验收 1：状态机（合法全链 + 非法拒绝）
    # ------------------------------------------------------------------
    def test_machine_legal_path(self):
        self.assertEqual(
            frozenset(REQUEST_STATUSES),
            frozenset({"待发起", "已发起", "材料已回", "关闭"}))
        self.assertTrue(can_request_transition(R_DRAFT, R_SENT))
        self.assertTrue(can_request_transition(R_SENT, R_RETURNED))
        self.assertTrue(can_request_transition(R_SENT, R_CLOSED))
        self.assertTrue(can_request_transition(R_RETURNED, R_CLOSED))
        self.assertFalse(can_request_transition(R_CLOSED, R_SENT))
        self.assertFalse(can_request_transition(R_DRAFT, R_RETURNED))

    def test_machine_illegal_rejected(self):
        for cur, nxt in ((R_DRAFT, R_RETURNED), (R_DRAFT, R_CLOSED),
                         (R_RETURNED, R_SENT), (R_CLOSED, R_SENT),
                         (R_SENT, R_SENT)):
            with self.assertRaises(VerifyTransitionError) as cm:
                validate_request_transition(cur, nxt)
            self.assertEqual(cm.exception.code, ERR_INVALID_TRANSITION)
        with self.assertRaises(VerifyTransitionError) as cm:
            validate_request_transition("不存在", R_SENT)
        self.assertEqual(cm.exception.code, ERR_UNKNOWN_STATUS)
        with self.assertRaises(VerifyTransitionError) as cm:
            validate_request_transition(R_SENT, "不存在")
        self.assertEqual(cm.exception.code, ERR_UNKNOWN_STATUS)

    def test_full_lifecycle_via_store(self):
        row = self._insert(item_id="vi_1",
                           legal_instrument="调取函（2026）12 号",
                           handler="李侦查员", due_date="2099-01-01")
        self.assertTrue(row["request_id"].startswith("vr_"))
        self.assertEqual(row["status"], R_DRAFT)
        # 非法迁移在落库前被状态机单点拒绝（Worker 纪律的同构断言）
        with self.assertRaises(VerifyTransitionError):
            validate_request_transition(row["status"], R_RETURNED)
        for nxt in (R_SENT, R_RETURNED, R_CLOSED):
            validate_request_transition(row["status"], nxt)  # 不抛
            row = self.st.update_verify_request_status(
                row["request_id"], nxt, updated_at=_ts())
            self.assertEqual(row["status"], nxt)
        self.assertTrue(row["updated_at"])
        # 终态：关闭后再迁移一律非法
        with self.assertRaises(VerifyTransitionError):
            validate_request_transition(R_CLOSED, R_SENT)

    # ------------------------------------------------------------------
    # 验收 2：overdue 读面派生（存储 status 不变）
    # ------------------------------------------------------------------
    def test_overdue_derived_not_stored(self):
        overdue = self._insert(due_date=_date(1))
        self.st.update_verify_request_status(overdue["request_id"], R_SENT)
        future = self._insert(due_date="2099-01-01")
        self.st.update_verify_request_status(future["request_id"], R_SENT)
        no_due = self._insert(due_date="")
        self.st.update_verify_request_status(no_due["request_id"], R_SENT)
        returned = self._insert(due_date=_date(30))
        self.st.update_verify_request_status(
            returned["request_id"], R_SENT, updated_at=_ts(31))
        self.st.update_verify_request_status(
            returned["request_id"], R_RETURNED)

        rows = {r["request_id"]: r
                for r in self.st.list_verify_requests("clue-1")}
        self.assertTrue(rows[overdue["request_id"]]["overdue"])
        self.assertFalse(rows[future["request_id"]]["overdue"])
        self.assertFalse(rows[no_due["request_id"]]["overdue"])
        self.assertFalse(rows[returned["request_id"]]["overdue"])
        # 存储状态永不因超期改写：直读行验证仍为 已发起
        self.assertEqual(
            self.st.get_verify_request(overdue["request_id"])["status"],
            R_SENT)

    # ------------------------------------------------------------------
    # 验收 3：CRUD 幂等 / 隔离
    # ------------------------------------------------------------------
    def test_list_order_and_status_filter(self):
        a = self._insert(created_at=_ts(2))
        b = self._insert(created_at=_ts(1))
        rows = self.st.list_verify_requests("clue-1")
        self.assertEqual([r["request_id"] for r in rows],
                         [b["request_id"], a["request_id"]])  # 新在前
        self.assertEqual(
            self.st.list_verify_requests("clue-1", status=R_SENT), [])
        self.st.update_verify_request_status(a["request_id"], R_SENT)
        rows = self.st.list_verify_requests("clue-1", status=R_SENT)
        self.assertEqual([r["request_id"] for r in rows],
                         [a["request_id"]])
        # 线索隔离：其他线索不可见
        self.assertEqual(self.st.list_verify_requests("clue-x"), [])

    def test_update_missing_returns_none(self):
        self._insert()
        self.assertIsNone(
            self.st.update_verify_request_status("vr_missing0000", R_SENT))

    def test_legacy_db_without_request_table(self):
        """旧库（无 verify_request 表）打开即建，台账可用（幂等建表）。"""
        self.st.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db) + suffix)
            if p.exists():
                p.unlink()
        legacy = sqlite3.connect(str(self.db))
        legacy.execute("CREATE TABLE audit_chain (seq INTEGER)")
        legacy.commit()
        legacy.close()
        st = StateStore("c1", self.db)
        try:
            row = st.insert_verify_request(
                case_id="c1", clue_id="clue-1", target="市监局",
                material="工商档案", created_by="王检察官")
            self.assertEqual(row["status"], R_DRAFT)
            # list 行含派生键 overdue；核心字段与 get 原行一致
            listed = st.list_verify_requests("clue-1")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["request_id"], row["request_id"])
            self.assertEqual(listed[0]["overdue"], False)
        finally:
            st.close()


class RequestApiTest(unittest.TestCase):
    """REQ-V-013 台账 API（TestClient 造案；Worker 不自动 start，
    run_until_drained 确定性驱动——同 test_verify_api 纪律）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.client = TestClient(create_app(self.ctx))
        for operator, role in (("王检察官", "human"),
                               ("李侦查员", "正兵"),
                               ("agent:sunzi", "正兵")):
            salt, h = hash_password("pw")
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role, clearance=1,
                                       tenant_id="t1"))
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator="李检", password_hash=h,
                                   salt=salt, role="正兵", clearance=1,
                                   tenant_id="t2"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官")
        self.auth_agent = self._login("agent:sunzi")
        self.auth_t2 = self._login("李检")
        # 建案 + 版本指针 + 线索产物（add_request Worker 前置 ver>=1）
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version("c1", 1, "test")
        save_case_clues(self.factory.case_dir("c1"), 1,
                        [LineageClue(clue_id="clue-1", skill_id="xu_shi",
                                     title="测试线索", jian_types=["生间"],
                                     source_rows=[{"file": "b.parquet",
                                                   "row_id": 1}])])

    def tearDown(self):
        self.pool.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _login(self, operator: str) -> dict:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": "pw"})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _drain(self) -> None:
        self.pool.run_until_drained(max_idle_rounds=5)

    def _create(self, body: dict, case_id: str = "c1",
                clue_id: str = "clue-1", headers: dict | None = None):
        return self.client.post(
            f"/api/v1/cases/{case_id}/clues/{clue_id}/verify-requests",
            headers=headers or self.auth_h, json=body)

    def _trans(self, request_id: str, next_status: str,
               case_id: str = "c1", headers: dict | None = None):
        return self.client.post(
            f"/api/v1/cases/{case_id}/verify-requests/{request_id}"
            f"/transitions",
            headers=headers or self.auth_h, json={"next_status": next_status})

    def _get(self, case_id: str = "c1", headers: dict | None = None,
             query: str = ""):
        return self.client.get(
            f"/api/v1/cases/{case_id}/verify-requests{query}",
            headers=headers or self.auth_h)

    def _task_of(self, resp):
        t = self.repo.get_task(resp.json()["data"]["id"])
        self.assertIsNotNone(t)
        return t

    def _reqs(self, headers: dict | None = None, query: str = "",
              case_id: str = "c1"):
        r = self._get(case_id=case_id, headers=headers, query=query)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def _only_request_id(self) -> str:
        data = self._reqs()
        self.assertEqual(len(data["items"]), 1)
        return data["items"][0]["request_id"]

    # ------------------------------------------------------------------
    # AC-1：全链路 202 流转 + GET 终态 + 审计链
    # ------------------------------------------------------------------
    def test_full_lifecycle_202_and_get(self):
        r = self._create({
            "target": "海州银行营业部",
            "material": "2025 年 1-6 月账户流水",
            "legal_instrument": "调取函（2026）12 号",
            "handler": "李侦查员",
            "due_date": "2099-01-01",
            "note": "加急",
        })
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._task_of(r)
        self.assertEqual(t.status, TASK_SUCCEEDED, t.error_message)

        data = self._reqs()
        self.assertTrue(data["available"])
        self.assertEqual(len(data["items"]), 1)
        row = data["items"][0]
        rid = row["request_id"]
        self.assertTrue(rid.startswith("vr_"))
        self.assertEqual(row["status"], R_DRAFT)
        self.assertEqual(row["target"], "海州银行营业部")
        self.assertEqual(row["created_by"], "王检察官")

        # 发起 → 回执登记 → 关闭：每步 202，GET 反映终态
        for nxt in (R_SENT, R_RETURNED, R_CLOSED):
            r = self._trans(rid, nxt)
            self.assertEqual(r.status_code, 202, r.text)
            self._drain()
            self.assertEqual(self._task_of(r).status, TASK_SUCCEEDED)
            rows = self._reqs()["items"]
            self.assertEqual(rows[0]["request_id"], rid)
            self.assertEqual(rows[0]["status"], nxt)

        # 审计链：create + 3 次 transition，签名完整
        st = StateStore("c1", self.factory.case_dir("c1") / "state.sqlite")
        try:
            self.assertTrue(st.chain_verify())
            rows = st._conn.execute(
                "SELECT after_state FROM audit_chain "
                "ORDER BY seq").fetchall()
            events = [json.loads(r[0]).get("event", "") for r in rows]
            self.assertIn("verify_request_create", events)
            self.assertEqual(
                events.count("verify_request_transition"), 3)
        finally:
            st.close()

    # ------------------------------------------------------------------
    # AC-2：overdue 徽标经 API 透出（存储 status 不变）
    # ------------------------------------------------------------------
    def test_overdue_badge_via_api(self):
        due = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._create({"target": "市监局", "material": "工商档案",
                      "due_date": due})
        self._drain()
        rid = self._only_request_id()
        data = self._reqs()
        self.assertFalse(data["items"][0]["overdue"])  # 待发起不派生超期
        self._trans(rid, R_SENT)
        self._drain()
        data = self._reqs()
        self.assertEqual(data["items"][0]["status"], R_SENT)
        self.assertTrue(data["items"][0]["overdue"])  # 过期已发起 → true

    # ------------------------------------------------------------------
    # AC-3：跨租户 404 / 未登录 401 / agent 403
    # ------------------------------------------------------------------
    def test_cross_tenant_and_agent(self):
        # 未登录 401（不带头直呼，同 test_verify_api.test_no_token_401 纪律）
        self.assertEqual(self.client.get(
            "/api/v1/cases/c1/verify-requests").status_code, 401)
        # 跨租户 404（读 + 写）
        self.assertEqual(self._get(headers=self.auth_t2).status_code, 404)
        self.assertEqual(self._create(
            {"target": "x", "material": "y"},
            headers=self.auth_t2).status_code, 404)
        # agent 红线：403 且不产生任务行
        before = len(self.repo.list_tasks(case_id="c1"))
        self.assertEqual(self._create(
            {"target": "x", "material": "y"},
            headers=self.auth_agent).status_code, 403)
        self.assertEqual(self._get(headers=self.auth_t2).status_code, 404)
        r = self._create({"target": "x", "material": "y"})
        self._drain()
        rid = self._only_request_id()
        self.assertEqual(self._trans(
            rid, R_SENT, headers=self.auth_agent).status_code, 403)
        self.assertEqual(
            len(self.repo.list_tasks(case_id="c1")), before + 1)  # 仅 human 1 条

    # ------------------------------------------------------------------
    # 非法迁移：Worker 兜底 FAILED REQUEST_REJECTED，存储不变
    # ------------------------------------------------------------------
    def test_invalid_transition_task_failed(self):
        self._create({"target": "x", "material": "y"})
        self._drain()
        rid = self._only_request_id()
        r = self._trans(rid, R_RETURNED)  # 待发起 → 材料已回 非法
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        t = self._task_of(r)
        self.assertEqual(t.status, TASK_FAILED)
        self.assertEqual(t.error_code, "REQUEST_REJECTED")
        data = self._reqs()
        self.assertEqual(data["items"][0]["status"], R_DRAFT)  # 不落库
        # 非法目标值在 API 侧即 400（不产生任务行）
        n = len(self.repo.list_tasks(case_id="c1"))
        self.assertEqual(self._trans(rid, "不存在").status_code, 400)
        self.assertEqual(len(self.repo.list_tasks(case_id="c1")), n)

    # ------------------------------------------------------------------
    # 形状 400：target/material/next_status 缺失、due_date 非法
    # ------------------------------------------------------------------
    def test_validation_400s(self):
        self.assertEqual(self._create({"material": "y"}).status_code, 400)
        self.assertEqual(self._create({"target": "x"}).status_code, 400)
        self.assertEqual(self._create(
            {"target": "x", "material": "y",
             "due_date": "2099/01/01"}).status_code, 400)
        # next_status 缺失/非法 → 400（无任务行）
        self.assertEqual(self.client.post(
            "/api/v1/cases/c1/verify-requests/vr_x/transitions",
            headers=self.auth_h,
            json={}).status_code, 400)
        self.assertEqual(self._trans("vr_x", "不存在").status_code, 400)
        self.assertEqual(self.repo.list_tasks(case_id="c1"), [])

    # ------------------------------------------------------------------
    # GET 过滤与 available:false
    # ------------------------------------------------------------------
    def test_get_filters_and_unavailable(self):
        self._create({"target": "x", "material": "y"},
                     clue_id="clue-1")
        self._create({"target": "y", "material": "z"}, clue_id="clue-2")
        self._drain()
        data = self._reqs(query="?clue_id=clue-1")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["clue_id"], "clue-1")
        data = self._reqs(query="?status=待发起")
        self.assertEqual(len(data["items"]), 2)
        data = self._reqs(query="?status=已发起")
        self.assertEqual(data["items"], [])
        # 全案件视角（不带 clue_id）：两行都在
        self.assertEqual(len(self._reqs()["items"]), 2)
        # 无 state.sqlite 的案件 → available:false 空清单。
        # 建案路由含 B5 生命周期审计（on_case_created 开 StateStore 落链，
        # 副作用即建 state.sqlite），故直接走 CaseService 造案模拟文件缺失
        # （同 test_verify_api.py available:false 用例的前提纪律）
        self.svc.create_case(case_id="c2", name="空案",
                             tenant_id="t1", created_by="王检察官")
        state_path = self.factory.case_dir("c2") / "state.sqlite"
        self.assertFalse(state_path.exists())  # 前置：文件确实不存在
        data = self._reqs(case_id="c2")
        self.assertFalse(data["available"])
        self.assertEqual(data["items"], [])


if __name__ == "__main__":
    unittest.main()
