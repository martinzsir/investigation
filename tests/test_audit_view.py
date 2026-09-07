"""
tests/test_audit_view.py
M2 阶段 C+D：W-023 审计链查看/自检 + W-024 操作者身份绑定与平台审计。

W-023：
  AC-1 时间线字段齐全（操作人/状态迁移/法定依据/本体版本）可筛选；
  AC-2 篮选与分页确定性；
  AC-3 篡改/断链检出 chain_ok=false；
  AC-4 空链红线：actual_count==0 一律 chain_ok=false + empty_chain=true
      （决策 D-M2-3：core 的 REQ-G-025「纯查询运行空链=完整」仅 CLI/MCP 语义，
       平台案件建案必经 BUILD 留痕，无合法空链场景——红线在 API 包装层强制，
       core 零改动，auditinteg 组保持回归锚）；
  AC-5 处置事件数与库内处置留痕交叉比对；
W-024：
  平台审计事件（登录/登出/鉴权失败/authz_failure）落 platform_audit；
  /audit/events 仅管理员（is_admin=1 或 system）可访问；
  operator 一律取会话主体——请求体 operator 字段无效；
  审计链 operator 与登录记录同值一一对应。

grep 门禁（最小影响红线扩展）：
  server/ 内无 core Store 实例化；server/app 除 store/ 外无 duckdb.connect；
  sqlite3.connect 仅出现在 meta/repo_sqlite.py 与 store/state_store.py。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
from fastapi.testclient import TestClient

from core.audit import AuditChain
from core.lineage import save_statuses
from core.registry import LineageClue

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool


def _ev(operator: str, after: dict, before: dict | None = None,
        source: list[str] | None = None) -> dict:
    return {"operator": operator, "before": before, "after": after,
            "source_row_ids": source or [], "ontology_version": "ont-1.0"}


class AuditViewTest(unittest.TestCase):
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
        salt, h = hash_password("pw-wang")
        self.repo.create_user(User(operator="王检", password_hash=h,
                                   salt=salt, role="主办", clearance=3,
                                   tenant_id="t1"))
        salt2, h2 = hash_password("pw-li")
        self.repo.create_user(User(operator="李检", password_hash=h2,
                                   salt=salt2, role="正兵", clearance=1,
                                   tenant_id="t1"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.05, backoff_base=0.05)
        self.pool.start()
        self.token_w = self.login("王检", "pw-wang")
        self.token_l = self.login("李检", "pw-li")
        self.auth_w = {"Authorization": f"Bearer {self.token_w}"}
        self.auth_l = {"Authorization": f"Bearer {self.token_l}"}

    def tearDown(self):
        self.pool.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, operator: str, password: str) -> str:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]["token"]

    def make_case(self, case_id: str = "c1") -> None:
        r = self.client.post("/api/v1/cases", headers=self.auth_w,
                             json={"case_id": case_id, "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def write_chain(self, case_id: str, version: int, events: list[dict],
                    statuses: list[LineageClue] | None = None) -> None:
        """写版本文件：audit_chain 事件 + 可选 clue_disposal_status 处置留痕。"""
        store = self.factory.for_case(case_id, mode="write", version=version)
        conn = store.write_conn
        try:
            chain = AuditChain(conn, case_id)
            for e in events:
                chain.append(**e)
            if statuses:
                save_statuses(conn, statuses)
        finally:
            store.close()
        self.repo.set_version(case_id, version, "test")

    # ------------------------------------------------------------------
    # W-023 AC-1/2：时间线字段与筛选分页
    # ------------------------------------------------------------------
    def test_timeline_fields_filter_pagination(self):
        self.make_case("c1")
        self.write_chain("c1", 1, [
            _ev("王检", {"status": "查证中", "note": "调流水"},
                before={"status": "待查"}, source=["row-1"]),
            _ev("李检", {"proposal_id": "pp-1", "title": "提案"}),
            _ev("王检", {"status": "已排除", "note": "正常往来"},
                before={"status": "查证中"}),
        ])
        r = self.client.get("/api/v1/cases/c1/audit", headers=self.auth_w)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        # 信封：{items,total,page,page_size}
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 50)
        # AC-1 四要素字段齐全
        first = data["items"][0]
        for key in ("operator", "status_from", "status_to", "legal_basis",
                    "ontology_version", "occurred_at", "event_id", "action"):
            self.assertIn(key, first)
        self.assertEqual(first["status_from"], "待查")
        self.assertEqual(first["status_to"], "查证中")
        self.assertEqual(first["ontology_version"], "ont-1.0")
        self.assertEqual(first["action"], "disposal")

        # AC-2 筛选：operator
        r = self.client.get("/api/v1/cases/c1/audit?operator=李检",
                            headers=self.auth_w)
        self.assertEqual(r.json()["data"]["total"], 1)
        self.assertEqual(r.json()["data"]["items"][0]["action"], "proposal")
        # AC-2 筛选：action 派生标签
        r = self.client.get("/api/v1/cases/c1/audit?action=disposal",
                            headers=self.auth_w)
        self.assertEqual(r.json()["data"]["total"], 2)
        # AC-2 筛选：clue_id（before/after/source JSON 包含匹配）
        r = self.client.get("/api/v1/cases/c1/audit?clue_id=row-1",
                            headers=self.auth_w)
        self.assertEqual(r.json()["data"]["total"], 1)
        # AC-2 分页：page_size=2 → 两页
        r = self.client.get("/api/v1/cases/c1/audit?page=1&page_size=2",
                            headers=self.auth_w)
        d1 = r.json()["data"]
        self.assertEqual(len(d1["items"]), 2)
        self.assertEqual(d1["total"], 3)
        r = self.client.get("/api/v1/cases/c1/audit?page=2&page_size=2",
                            headers=self.auth_w)
        self.assertEqual(len(r.json()["data"]["items"]), 1)
        # 时间范围筛选（occurred_at 为 ISO 秒级字符串，字典序即时间序）
        evs = d1["items"]
        mid = evs[0]["occurred_at"]
        r = self.client.get(f"/api/v1/cases/c1/audit?from_ts={mid}",
                            headers=self.auth_w)
        self.assertEqual(r.json()["data"]["total"], 3)  # 全部同一秒内
        # 参数校验
        r = self.client.get("/api/v1/cases/c1/audit?page=0", headers=self.auth_w)
        self.assertEqual(r.status_code, 422)
        r = self.client.get("/api/v1/cases/c1/audit?page_size=500",
                            headers=self.auth_w)
        self.assertEqual(r.status_code, 422)

    def test_timeline_cross_tenant_404_and_unbuilt_case_empty(self):
        self.make_case("c1")
        # 跨租户 404（李检属 t1，这里用另一租户用户验证）
        salt3, h3 = hash_password("pw-zhao")
        self.repo.create_user(User(operator="赵检", password_hash=h3,
                                   salt=salt3, role="正兵", clearance=1,
                                   tenant_id="t2"))
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "赵检", "password": "pw-zhao"})
        auth_z = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        r = self.client.get("/api/v1/cases/c1/audit", headers=auth_z)
        self.assertEqual(r.status_code, 404)
        # 已建案未 BUILD（无版本文件）→ 空时间线不 500
        r = self.client.get("/api/v1/cases/c1/audit", headers=self.auth_w)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], {"items": [], "total": 0,
                                            "page": 1, "page_size": 50})

    # ------------------------------------------------------------------
    # W-023 AC-3：篡改检出
    # ------------------------------------------------------------------
    def test_tamper_detected_by_verify(self):
        self.make_case("c1")
        self.write_chain("c1", 1, [
            _ev("王检", {"status": "查证中"}, before={"status": "待查"}),
            _ev("王检", {"status": "已固证"}, before={"status": "查证中"}),
        ])
        # 直接改库模拟篡改（绕过 append，破坏签名链）
        path = self.factory.version_path("c1", 1)
        conn = duckdb.connect(str(path))
        try:
            conn.execute("UPDATE audit_chain SET after_state=? WHERE seq=1",
                         ['{"status": "已立案"}'])
        finally:
            conn.close()
        r = self.client.post("/api/v1/cases/c1/audit/verify",
                             headers=self.auth_w)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertFalse(data["chain_ok"])
        self.assertTrue(data["broken_links"])
        self.assertFalse(data["empty_chain"])
        self.assertEqual(data["actual_count"], 2)

    # ------------------------------------------------------------------
    # W-023 AC-4：空链红线（决策 D-M2-3：API 包装层强制）
    # ------------------------------------------------------------------
    def test_empty_chain_red_line(self):
        self.make_case("c1")
        # 场景 1：有版本文件但链上 0 条
        self.write_chain("c1", 1, [])
        r = self.client.post("/api/v1/cases/c1/audit/verify",
                             headers=self.auth_w)
        data = r.json()["data"]
        self.assertFalse(data["chain_ok"], "空链不得返回 chain_ok=true（W-023 AC-4）")
        self.assertTrue(data["empty_chain"])
        self.assertEqual(data["actual_count"], 0)
        # 场景 2：建案后从未 BUILD（无版本文件）
        self.make_case("c2")
        r = self.client.post("/api/v1/cases/c2/audit/verify",
                             headers=self.auth_w)
        data = r.json()["data"]
        self.assertFalse(data["chain_ok"])
        self.assertTrue(data["empty_chain"])

    # ------------------------------------------------------------------
    # W-023 AC-5：处置事件 × 库内处置留痕 交叉比对
    # ------------------------------------------------------------------
    def test_disposal_cross_check(self):
        self.make_case("c1")
        # 一致：链上有处置事件 + 库内有非待查处置留痕
        clue = LineageClue(clue_id="clue_x1", title="整数存入",
                           status="查证中")
        self.write_chain("c1", 1, [
            _ev("王检", {"clue_id": "clue_x1", "status": "查证中"},
                before={"status": "待查"}),
        ], statuses=[clue])
        r = self.client.post("/api/v1/cases/c1/audit/verify",
                             headers=self.auth_w)
        data = r.json()["data"]
        self.assertTrue(data["chain_ok"])
        self.assertTrue(data["cross_check"]["consistent"])
        self.assertEqual(data["cross_check"]["disposal_events"], 1)
        self.assertEqual(data["cross_check"]["persisted_non_pending"], 1)

        # 接线缺口：库内有处置留痕、链上零处置事件（REQ-G-025 unwired）
        self.make_case("c3")
        clue2 = LineageClue(clue_id="clue_x2", title="过桥资金",
                            status="已排除")
        self.write_chain("c3", 1, [
            _ev("王检", {"note": "与处置无关的记录"}),
        ], statuses=[clue2])
        r = self.client.post("/api/v1/cases/c3/audit/verify",
                             headers=self.auth_w)
        data = r.json()["data"]
        self.assertFalse(data["chain_ok"])
        self.assertFalse(data["cross_check"]["consistent"])
        self.assertEqual(data["cross_check"]["disposal_events"], 0)
        self.assertEqual(data["cross_check"]["persisted_non_pending"], 1)

    # ------------------------------------------------------------------
    # W-024：平台审计事件 + 管理员门槛 + operator 红线
    # ------------------------------------------------------------------
    def test_platform_events_recorded(self):
        # 登录成功/失败/登出/鉴权失败 均落 platform_audit
        self.client.post("/api/v1/auth/login",
                         json={"operator": "王检", "password": "wrong"})
        self.client.post("/api/v1/auth/login",
                         json={"operator": "ghost", "password": "x"})
        self.client.get("/api/v1/cases")  # 无 token → auth_failure
        self.client.post("/api/v1/auth/logout", headers=self.auth_l)
        events = {e["event"] for e in self.repo.list_platform_events(limit=100)}
        self.assertIn("login", events)
        self.assertIn("login_failure", events)
        self.assertIn("logout", events)
        self.assertIn("auth_failure", events)
        # 登录主体与事件 operator 同值
        logins = [e for e in self.repo.list_platform_events(limit=100)
                  if e["event"] == "login"]
        self.assertIn("王检", {e["operator"] for e in logins})

    def test_platform_events_admin_gate(self):
        self.make_case("c1")
        # 非管理员（李检 正兵）403，且落 authz_failure
        r = self.client.get("/api/v1/audit/events", headers=self.auth_l)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"]["code"], "FORBIDDEN")
        evs = self.repo.list_platform_events(event="authz_failure", limit=10)
        self.assertTrue(evs)
        self.assertEqual(evs[0]["operator"], "李检")
        # 提权为管理员后可查
        self.repo.set_user_admin("王检", True)
        r = self.client.get("/api/v1/audit/events", headers=self.auth_w)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        # system 角色免 is_admin
        salt, h = hash_password("pw-sys")
        self.repo.create_user(User(operator="system", password_hash=h,
                                   salt=salt, role="system", clearance=9,
                                   tenant_id="t0"))
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "system", "password": "pw-sys"})
        auth_s = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        r = self.client.get("/api/v1/audit/events", headers=auth_s)
        self.assertEqual(r.status_code, 200)

    def test_operator_from_session_not_body(self):
        """W-024：operator 一律取会话主体——请求体 operator 字段无效。"""
        self.make_case("c1")
        self.write_chain("c1", 1, [_ev("王检", {"status": "查证中"},
                                       before={"status": "待查"})])
        # verify 无请求体入参：塞 operator 字段不影响结果
        r1 = self.client.post("/api/v1/cases/c1/audit/verify",
                              headers=self.auth_w)
        r2 = self.client.post("/api/v1/cases/c1/audit/verify",
                              headers=self.auth_w,
                              json={"operator": "forged", "role": "system"})
        self.assertEqual(r1.json()["data"], r2.json()["data"])
        # timeline 的 operator 查询参数只是筛选条件，不改变身份
        r = self.client.get("/api/v1/cases/c1/audit?operator=forged",
                            headers=self.auth_w)
        self.assertEqual(r.json()["data"]["total"], 0)  # 仅筛选为空

    def test_chain_operator_matches_login(self):
        """W-024：审计链 operator 与平台审计登录记录同值一一对应。"""
        self.make_case("c1")
        self.write_chain("c1", 1, [
            _ev("王检", {"status": "查证中"}, before={"status": "待查"}),
            _ev("李检", {"status": "已排除"}, before={"status": "查证中"}),
        ])
        r = self.client.get("/api/v1/cases/c1/audit", headers=self.auth_w)
        chain_ops = {it["operator"] for it in r.json()["data"]["items"]}
        login_ops = {e["operator"] for e in self.repo.list_platform_events(
            event="login", limit=100)}
        self.assertTrue(chain_ops, "时间线应有操作人")
        self.assertTrue(chain_ops <= login_ops,
                        f"链上操作人 {chain_ops} 应都有对应登录记录 {login_ops}")

    # ------------------------------------------------------------------
    # grep 门禁（最小影响红线扩展）
    # ------------------------------------------------------------------
    def test_grep_gates(self):
        server = ROOT / "server"
        # 1) server/ 内无 core Store 实例化（StoreFactory/CrossCaseStore 不算）
        pat_store = re.compile(r"(?<![A-Za-z_])Store\(")
        hits = [str(f) for f in server.rglob("*.py")
                if pat_store.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [], f"server/ 内不得实例化 core Store：{hits}")
        # 2) server/app 除 store/ 外无 duckdb.connect
        pat_conn = re.compile(r"duckdb\.connect\(")
        hits = [str(f) for f in (server / "app").rglob("*.py")
                if f.parent.name != "store"
                and pat_conn.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [], f"store/ 外不得直连 duckdb：{hits}")
        # 3) sqlite3.connect 仅在 meta/repo_sqlite.py 与 store/state_store.py
        pat_sq = re.compile(r"sqlite3\.connect\(")
        allowed = {"repo_sqlite.py", "state_store.py"}
        hits = [f.name for f in server.rglob("*.py")
                if pat_sq.search(f.read_text(encoding="utf-8"))]
        unexpected = [h for h in hits if h not in allowed]
        self.assertEqual(unexpected, [],
                         f"sqlite3.connect 越界：{unexpected}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
