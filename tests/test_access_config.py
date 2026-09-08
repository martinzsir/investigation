"""
tests/test_access_config.py
M4 阶段 A：W-015 权限与字段遮蔽配置。

AC：
  AC-1 可配置对象级权限（角色 + min_clearance）；
  AC-2 字段级遮蔽（default/allow_roles/mask）过 loader 校验；
  AC-3 未声明权限对象一律拒绝（fail-closed）；
  AC-4 不同角色同对象敏感字段返回不同（遮蔽生效）；
  AC-5 mask 类型仅可选已实现类型；
  AC-6 保存即生效，无需重建语义层。
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


class AccessConfigTest(unittest.TestCase):
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
        self.policies_path = self.snap_dir / "policies.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _read_policies(self):
        return json.loads(self.policies_path.read_text(encoding="utf-8"))

    def test_get_policies(self):
        r = self.client.get("/api/v1/cases/c1/policies", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertIn("object_policies", d)
        self.assertIn("property_policies", d)

    def test_put_policies_valid(self):
        """AC-1/2/6：合法策略保存过 loader，立即生效（不重建语义层）。"""
        pol = self._read_policies()
        r = self.client.put("/api/v1/cases/c1/policies", headers=self.auth_pj,
                            json=pol)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["saved"])

    def test_put_policies_bad_mask(self):
        """AC-5：mask 非法 → 400，不落盘。"""
        before = self._read_policies()
        pol = json.loads(json.dumps(before))
        pol["property_policies"].append(
            {"object": "person", "property": "phone",
             "default": "deny", "allow_roles": ["human"],
             "mask": "reversible"})  # 非法 mask
        r = self.client.put("/api/v1/cases/c1/policies", headers=self.auth_pj,
                            json=pol)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._read_policies(), before)

    def test_fail_closed_undeclared_object(self):
        """AC-3：未声明策略的对象运行时一律拒绝（fail-closed）。"""
        from core.access import AccessContext
        from core.policy import PolicyEngine, PolicyDeniedError
        pol = self._read_policies()
        # 移除 person 的对象策略
        pol["object_policies"] = [
            op for op in pol["object_policies"] if op["object"] != "person"]
        r = self.client.put("/api/v1/cases/c1/policies", headers=self.auth_pj,
                            json=pol)
        self.assertEqual(r.status_code, 200, r.text)
        # 运行时：person 未声明策略 → fail-closed 拒绝
        pe = PolicyEngine("default", path=self.policies_path)
        ctx = AccessContext(operator="t", role="正兵", clearance=1)
        with self.assertRaises(PolicyDeniedError):
            pe.check_object(ctx, "person")

    def test_put_policies_forbidden_for_analyst(self):
        """正兵无写权限 → 403。"""
        # 用 human (clearance 4) 可读可写；这里测未授权角色
        salt3, h3 = hash_password("pw-sol")
        self.repo.create_user(User(operator="李侦查员", password_hash=h3,
                                   salt=salt3, role="正兵", clearance=1,
                                   tenant_id="t1"))
        auth_s = self._login("李侦查员", "pw-sol")
        pol = self._read_policies()
        r = self.client.put("/api/v1/cases/c1/policies", headers=auth_s,
                            json=pol)
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
