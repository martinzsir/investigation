"""
tests/test_s0_admin.py
S0-2/F2 本体管理员能力位：meta_users.is_ontology_admin 五件套 + 双条件门禁。

验收点：
  - repo 能力位 setter（默认 0，置位/撤销可往返）；
  - 既有库追加式迁移（R6：meta.db 只 ALTER 不重建，账号数据不动）；
  - require_ontology_admin 双条件：能力位 且 rank ≥ 偏将，system 旁路
    （R4：能力位不改 rank，两把尺子不互比）；
  - 授权端点：平台管理员可授权/撤销；非平台管理员 403；目标用户 404；
  - 登录响应携带 is_ontology_admin 布尔（前端渲染不靠 403 探测）。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from server.app.deps import WebContext
from server.app.cases import CaseService
from server.app.envelope import APIError
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.snapshot_config import require_ontology_admin
from server.app.security import Principal, hash_password
from server.app.store import StoreFactory


def _principal(role: str, is_ontology_admin: int = 0,
               is_admin: int = 0) -> Principal:
    return Principal(operator="测试员", role=role, clearance=0,
                     tenant_id="t1", token="tok",
                     is_admin=is_admin,
                     is_ontology_admin=is_ontology_admin)


class TestOntologyAdminGate(unittest.TestCase):
    """双条件门禁（UC-S0-4/5/6）。"""

    def test_system_bypass(self):
        require_ontology_admin(_principal("system"))  # 不抛

    def test_admin_flag_with_rank_passes(self):
        require_ontology_admin(_principal("偏将", is_ontology_admin=1))  # 不抛
        require_ontology_admin(_principal("主办", is_ontology_admin=1))  # 不抛

    def test_flag_without_rank_rejected(self):
        """光有能力位没有办案资格（职级低于偏将）→ 403。"""
        with self.assertRaises(APIError) as cm:
            require_ontology_admin(_principal("正兵", is_ontology_admin=1))
        self.assertEqual(cm.exception.status, 403)

    def test_rank_without_flag_rejected(self):
        """光有职级没有能力位（改标准 ≠ 办案）→ 403。"""
        with self.assertRaises(APIError) as cm:
            require_ontology_admin(_principal("偏将"))
        self.assertEqual(cm.exception.status, 403)


class TestOntologyAdminRepo(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_user(self, operator: str) -> None:
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator=operator, password_hash=h,
                                   salt=salt, role="偏将", clearance=2,
                                   tenant_id="t1"))

    def test_default_flag_zero_and_set_unset(self):
        self._mk_user("张偏将")
        self.assertEqual(self.repo.get_user("张偏将").is_ontology_admin, 0)
        self.repo.set_user_ontology_admin("张偏将", True)
        self.assertEqual(self.repo.get_user("张偏将").is_ontology_admin, 1)
        self.repo.set_user_ontology_admin("张偏将", False)
        self.assertEqual(self.repo.get_user("张偏将").is_ontology_admin, 0)

    def test_flag_does_not_touch_role(self):
        """R4：能力位与 rank 正交——置位不改角色/职级。"""
        self._mk_user("张偏将")
        before = self.repo.get_user("张偏将")
        self.repo.set_user_ontology_admin("张偏将", True)
        after = self.repo.get_user("张偏将")
        self.assertEqual((before.role, before.clearance),
                         (after.role, after.clearance))

    def test_legacy_db_migrates_additively(self):
        """R6：老库（无 is_ontology_admin 列）追加迁移，既有账号不丢。"""
        legacy = self.tmp / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.executescript("""
            CREATE TABLE meta_users (
                operator      VARCHAR PRIMARY KEY,
                password_hash VARCHAR NOT NULL,
                salt          VARCHAR NOT NULL,
                role          VARCHAR NOT NULL,
                clearance     INTEGER NOT NULL DEFAULT 1,
                tenant_id     VARCHAR NOT NULL,
                status        VARCHAR NOT NULL DEFAULT 'active',
                created_at    VARCHAR NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO meta_users (operator, password_hash, salt, role,
                                    clearance, tenant_id, created_at)
            VALUES ('老用户', 'h', 's', '偏将', 2, 't1', '2026-01-01T00:00:00');
        """)
        conn.commit()
        conn.close()
        repo = SqliteMetaRepo(legacy)  # 触发 _migrate
        u = repo.get_user("老用户")
        self.assertIsNotNone(u)
        self.assertEqual(u.is_ontology_admin, 0)
        repo.set_user_ontology_admin("老用户", True)
        self.assertEqual(repo.get_user("老用户").is_ontology_admin, 1)


class TestOntologyAdminEndpoint(unittest.TestCase):
    """授权端点（平台管理员专属）+ 登录响应携带能力位。"""

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
        for op, pw, is_admin in (("平台管理员", "pw-admin", 1),
                                 ("张偏将", "pw-pj", 0)):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=op, password_hash=h,
                                       salt=salt, role="human", clearance=4,
                                       tenant_id="t1", is_admin=is_admin))
        self.auth_admin = self._login("平台管理员", "pw-admin")
        self.auth_user = self._login("张偏将", "pw-pj")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator: str, password: str) -> dict[str, str]:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator,
                                   "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def test_platform_admin_grants_and_revokes(self):
        r = self.client.post("/api/v1/users/张偏将/ontology-admin",
                             headers=self.auth_admin, json={"enabled": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["is_ontology_admin"])
        self.assertEqual(
            self.repo.get_user("张偏将").is_ontology_admin, 1)
        # 撤销
        r = self.client.post("/api/v1/users/张偏将/ontology-admin",
                             headers=self.auth_admin, json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.repo.get_user("张偏将").is_ontology_admin, 0)

    def test_non_platform_admin_forbidden(self):
        r = self.client.post("/api/v1/users/平台管理员/ontology-admin",
                             headers=self.auth_user, json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text)

    def test_unknown_user_404(self):
        r = self.client.post("/api/v1/users/不存在用户/ontology-admin",
                             headers=self.auth_admin, json={"enabled": True})
        self.assertEqual(r.status_code, 404, r.text)

    def test_login_response_carries_flag(self):
        """登录/个人信息响应带 is_ontology_admin 布尔（能力位可视化）。"""
        self.repo.set_user_ontology_admin("张偏将", True)
        r = self.client.get("/api/v1/auth/me", headers=self.auth_user)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["data"]["is_ontology_admin"], True)


if __name__ == "__main__":
    unittest.main()
