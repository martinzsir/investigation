"""
tests/test_escape_hatch_api.py
M5 阶段 C：W-031 代码逃生舱。

AC：
  AC-1 生成合法代码桩文件；
  AC-2 含输入输出契约说明；
  AC-3 含可运行测试骨架（初始失败状态）；
  AC-4 含注册位置说明；
  AC-5 触发统计。
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

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.escape_hatch import EXT_TYPES, generate_stub


class EscapeHatchApiTest(unittest.TestCase):
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
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator="u1", password_hash=h, salt=salt,
                                   role="human", clearance=4, tenant_id="t1"))
        self.h = self._login("u1", "pw")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def test_all_ext_types_supported(self):
        self.assertEqual(set(EXT_TYPES),
                         {"function", "value_type", "clean_rule", "side_effect"})

    def test_generate_function_stub(self):
        """AC-1/2/3/4：function 代码桩含契约+测试骨架+注册点。"""
        r = self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "function", "name": "my_detector",
                  "description": "检测异常交易"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        paths = [f["path"] for f in data["files"]]
        self.assertIn("core/functions/my_detector.py", paths)
        # AC-2：含输入输出契约
        impl = next(f["content"] for f in data["files"]
                    if f["path"] == "core/functions/my_detector.py")
        self.assertIn("输入契约", impl)
        self.assertIn("输出契约", impl)
        # AC-3：测试骨架初始失败
        test_content = next(f["content"] for f in data["files"]
                            if f["path"].endswith(".py") and "test" in f["path"])
        self.assertIn("self.fail", test_content)
        # AC-4：注册点说明
        self.assertTrue(data["registration_points"])

    def test_generate_value_type_stub(self):
        r = self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "value_type", "name": "phone",
                  "description": "手机号类型"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(data["files"])
        self.assertTrue(data["registration_points"])

    def test_generate_clean_rule_stub(self):
        r = self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "clean_rule", "name": "strip_space",
                  "description": "去空格"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(any("clean" in f["path"] for f in data["files"]))

    def test_generate_side_effect_stub(self):
        r = self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "side_effect", "name": "notify",
                  "description": "通知副作用"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(any("sideeffect" in f["path"] for f in data["files"]))

    def test_invalid_ext_type_rejected(self):
        r = self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "invalid", "name": "x"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_stats(self):
        """AC-5：触发统计。"""
        self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "function", "name": "f1"})
        self.client.post(
            "/api/v1/escape-hatch/generate", headers=self.h,
            json={"ext_type": "function", "name": "f2"})
        r = self.client.get("/api/v1/escape-hatch/stats", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["items"]
        func_stat = next((i for i in items if i["ext_type"] == "function"), None)
        self.assertIsNotNone(func_stat)
        self.assertEqual(func_stat["count"], 2)

    def test_stub_not_written_to_core(self):
        """逃生舱不写文件、不注册 core（仅返回文本）。"""
        from core.functions import FUNCTION_IMPLS
        before = set(FUNCTION_IMPLS.keys())
        generate_stub("function", "ghost_func", "test")
        after = set(FUNCTION_IMPLS.keys())
        self.assertEqual(before, after, "逃生舱不得注册 core Function")


if __name__ == "__main__":
    unittest.main()
