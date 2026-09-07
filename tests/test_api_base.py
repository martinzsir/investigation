"""
tests/test_api_base.py
M1 阶段 F：FastAPI 骨架端到端（TestClient）。

红线断言（S1~S6）：
  S1 无 token 一律 401（含 SSE 端点）；无 Cookie/query-token 旁路；
     operator 取自会话（建案 created_by 以会话为准，忽略请求体注入）；
  S2 CORS 默认关闭：跨域 preflight 不放行；白名单显式配置才放行；
  S3 业务路由统一 /api/v1 前缀；
  S4 错误信封 {"ok":false,"error":{"code","message"}}；
  S5 /api/v1/health 免认证；
  S6 成功信封 {"ok":true,"data","data_version"}；幂等键冲突返回同一任务。
另含：跨租户案件/任务访问一律 404；登录失败统一文案；任务 SSE 终态可达。
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

from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool


class ApiBaseTest(unittest.TestCase):
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
        # 两个租户的用户
        salt1, h1 = hash_password("pw-wang")
        self.repo.create_user(User(operator="王检", password_hash=h1,
                                   salt=salt1, role="主办", clearance=3,
                                   tenant_id="t1"))
        salt2, h2 = hash_password("pw-li")
        self.repo.create_user(User(operator="李检", password_hash=h2,
                                   salt=salt2, role="正兵", clearance=1,
                                   tenant_id="t2"))
        # 单 Worker 随测运行
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.05, backoff_base=0.05)
        self.pool.start()
        self.token_w = self.login("王检", "pw-wang")
        self.token_l = self.login("李检", "pw-li")

    def tearDown(self):
        self.pool.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, operator: str, password: str) -> str:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]["token"]

    def auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # ---- S5 健康探针 ----
    def test_health_no_auth(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["meta"], "ok")
        self.assertEqual(body["data"]["version"], "M1")

    # ---- S1 认证红线 ----
    def test_no_token_unauthorized_everywhere(self):
        for method, path in [("get", "/api/v1/cases"),
                             ("get", "/api/v1/tasks"),
                             ("get", "/api/v1/auth/me"),
                             ("get", "/api/v1/tasks/t_x/events")]:
            r = getattr(self.client, method)(path)
            self.assertEqual(r.status_code, 401, f"{path} 应 401")
            body = r.json()
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_login_failure_uniform_message(self):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "王检", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("用户名或密码错误", r.json()["error"]["message"])
        r2 = self.client.post("/api/v1/auth/login",
                              json={"operator": "ghost", "password": "x"})
        self.assertEqual(r2.status_code, 401)
        self.assertEqual(r2.json()["error"]["message"],
                         r.json()["error"]["message"])  # 不泄露用户存在性

    def test_me_and_logout(self):
        r = self.client.get("/api/v1/auth/me", headers=self.auth(self.token_w))
        self.assertEqual(r.json()["data"]["operator"], "王检")
        self.assertEqual(r.json()["data"]["tenant_id"], "t1")
        r = self.client.post("/api/v1/auth/logout",
                             headers=self.auth(self.token_w))
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/v1/auth/me",
                            headers=self.auth(self.token_w))
        self.assertEqual(r.status_code, 401)

    # ---- S6 信封 + 案件流程 ----
    def test_case_lifecycle_envelope(self):
        r = self.client.post("/api/v1/cases",
                             headers=self.auth(self.token_w),
                             json={"case_id": "c1", "name": "海州专案"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data_version"], 0)
        self.assertEqual(body["data"]["status"], "待建案")
        self.assertEqual(body["data"]["tenant_id"], "t1")
        # created_by 取自会话（请求体无法注入 operator）
        self.assertEqual(self.repo.get_case("c1").created_by, "王检")
        # 重复建案 409
        r = self.client.post("/api/v1/cases",
                             headers=self.auth(self.token_w),
                             json={"case_id": "c1", "name": "重复"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "CONFLICT")
        # 详情带版本
        r = self.client.get("/api/v1/cases/c1",
                            headers=self.auth(self.token_w))
        self.assertEqual(r.status_code, 200)
        self.assertIn("data_version", r.json())
        # 列表
        r = self.client.get("/api/v1/cases", headers=self.auth(self.token_w))
        self.assertEqual([c["id"] for c in r.json()["data"]], ["c1"])

    def test_cross_tenant_case_404(self):
        self.client.post("/api/v1/cases", headers=self.auth(self.token_w),
                         json={"case_id": "c1", "name": "甲案"})
        r = self.client.get("/api/v1/cases/c1",
                            headers=self.auth(self.token_l))
        self.assertEqual(r.status_code, 404)  # 不泄露存在性
        r = self.client.get("/api/v1/cases", headers=self.auth(self.token_l))
        self.assertEqual(r.json()["data"], [])

    # ---- 任务 + SSE ----
    def _wait_terminal(self, tid: str, timeout_s: float = 15.0) -> dict:
        import time
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = self.client.get(f"/api/v1/tasks/{tid}",
                                headers=self.auth(self.token_w))
            data = r.json()["data"]
            if data["status"] in ("SUCCEEDED", "FAILED"):
                return data
            time.sleep(0.1)
        self.fail(f"任务 {tid} 未到终态")

    def test_task_enqueue_idempotent_and_sse(self):
        self.client.post("/api/v1/cases", headers=self.auth(self.token_w),
                         json={"case_id": "c1", "name": "甲案"})
        payload = {"task_type": "PING", "idem_key": "k1"}
        r1 = self.client.post("/api/v1/cases/c1/tasks",
                              headers=self.auth(self.token_w), json=payload)
        self.assertEqual(r1.status_code, 200, r1.text)
        tid = r1.json()["data"]["id"]
        # S6 幂等：同键返回同一任务
        r2 = self.client.post("/api/v1/cases/c1/tasks",
                              headers=self.auth(self.token_w), json=payload)
        self.assertEqual(r2.json()["data"]["id"], tid)
        # 未知任务类型 400
        r = self.client.post("/api/v1/cases/c1/tasks",
                             headers=self.auth(self.token_w),
                             json={"task_type": "NUKE", "idem_key": "k2"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        # 终态
        done = self._wait_terminal(tid)
        self.assertEqual(done["status"], "SUCCEEDED")

        # SSE：带 token 可收到终态帧
        frames: list[dict] = []
        with self.client.stream("GET", f"/api/v1/tasks/{tid}/events",
                                headers=self.auth(self.token_w)) as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.headers["content-type"]
                            .startswith("text/event-stream"))
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    frames.append(json.loads(line[5:].strip()))
                    if frames[-1]["status"] in ("SUCCEEDED", "FAILED"):
                        break
                if len(frames) > 20:
                    break
        self.assertTrue(frames)
        self.assertEqual(frames[-1]["status"], "SUCCEEDED")

    def test_sse_requires_token(self):
        # S1：SSE 端点同样 401，不开 query-token 旁路
        r = self.client.get("/api/v1/tasks/t_x/events")
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/api/v1/tasks/t_x/events?token=abc")
        self.assertEqual(r.status_code, 401)

    def test_cross_tenant_task_404(self):
        self.client.post("/api/v1/cases", headers=self.auth(self.token_w),
                         json={"case_id": "c1", "name": "甲案"})
        r = self.client.post("/api/v1/cases/c1/tasks",
                             headers=self.auth(self.token_w),
                             json={"task_type": "PING", "idem_key": "k9"})
        tid = r.json()["data"]["id"]
        r = self.client.get(f"/api/v1/tasks/{tid}",
                            headers=self.auth(self.token_l))
        self.assertEqual(r.status_code, 404)
        r = self.client.get(f"/api/v1/tasks/{tid}/events",
                            headers=self.auth(self.token_l))
        self.assertEqual(r.status_code, 404)

    # ---- S2 CORS 默认关闭 ----
    def test_cors_default_closed_and_whitelist(self):
        # 默认：跨域 preflight 不放行
        r = self.client.options(
            "/api/v1/cases",
            headers={"Origin": "http://evil.example",
                     "Access-Control-Request-Method": "GET"})
        self.assertNotIn("access-control-allow-origin", r.headers)

        # 显式白名单：放行
        app2 = create_app(self.ctx, cors_origins=["http://app.local"])
        client2 = TestClient(app2)
        r = client2.options(
            "/api/v1/health",
            headers={"Origin": "http://app.local",
                     "Access-Control-Request-Method": "GET"})
        self.assertEqual(r.headers.get("access-control-allow-origin"),
                         "http://app.local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
