"""
tests/test_views_api.py
M4 阶段 A：W-017 角色视图配置。

AC：
  AC-1 可定义视图生成合法 views.json；
  AC-2 视图物化为 v_<name>，不复制数据；
  AC-3 读视图唯一入口 OntologyReadGateway.view(name)（不绕过网关）；
  AC-4 视图不绕过 PolicyEngine——无权限角色通过视图访问仍被拒；
  AC-5 视图引用列必须已声明，未声明列被拒绝。
权限：GET 登录即可；PUT 需偏将及以上。
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


class ViewsApiTest(unittest.TestCase):
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
        salt2, h2 = hash_password("pw-pj")
        self.repo.create_user(User(operator="张偏将", password_hash=h2,
                                   salt=salt2, role="偏将", clearance=2,
                                   tenant_id="t1"))
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_pj = self._login("张偏将", "pw-pj")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.snap_dir = self.svc.snapshot_dir("c1", "default")
        self.views_path = self.snap_dir / "views.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def test_get_views(self):
        r = self.client.get("/api/v1/cases/c1/views", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("views", r.json()["data"])

    def test_put_views_valid(self):
        """AC-1：合法视图保存过 loader。"""
        data = json.loads(self.views_path.read_text(encoding="utf-8"))
        r = self.client.put("/api/v1/cases/c1/views", headers=self.auth_pj,
                            json={"views": data["views"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_put_views_undeclared_column(self):
        """AC-5：引用列未声明 → 400，不落盘。"""
        before = self.views_path.read_text(encoding="utf-8")
        bad_views = [{
            "name": "bad_view",
            "base_object": "person",
            "properties": ["person_id", "nonexistent_col"],
            "roles": ["human"],
        }]
        r = self.client.put("/api/v1/cases/c1/views", headers=self.auth_pj,
                            json={"views": bad_views})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.views_path.read_text(encoding="utf-8"), before)

    def test_put_views_undeclared_base_object(self):
        """AC-5：base_object 未声明 → 400。"""
        bad_views = [{
            "name": "bad_view",
            "base_object": "ghost",
            "properties": [],
            "roles": ["human"],
        }]
        r = self.client.put("/api/v1/cases/c1/views", headers=self.auth_pj,
                            json={"views": bad_views})
        self.assertEqual(r.status_code, 400, r.text)

    def test_put_views_forbidden_for_soldier(self):
        salt3, h3 = hash_password("pw-sol")
        self.repo.create_user(User(operator="李侦查员", password_hash=h3,
                                   salt=salt3, role="正兵", clearance=1,
                                   tenant_id="t1"))
        auth_s = self._login("李侦查员", "pw-sol")
        data = json.loads(self.views_path.read_text(encoding="utf-8"))
        r = self.client.put("/api/v1/cases/c1/views", headers=auth_s,
                            json={"views": data["views"]})
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
