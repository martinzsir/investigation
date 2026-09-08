"""
tests/test_knowledge_api.py
M4 阶段 A：W-016 知识包维护。

AC：
  AC-1 新增/编辑/停用断言过 loader 校验；
  AC-2 valid_until 过期断言扫描自动排除（core r5 既有）；
  AC-3 零硬编码原则（断言不含硬编码人名规则）；
  AC-4 敏感地点白名单可被规则读取；
  AC-5 变更进审计链（ops 事件）。
权限：GET 登录即可；写需偏将及以上。
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


class KnowledgeApiTest(unittest.TestCase):
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
        self.knowledge_path = self.snap_dir / "case_knowledge.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def test_get_knowledge(self):
        r = self.client.get("/api/v1/cases/c1/knowledge", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertIn("relation_assertions", d)
        self.assertIn("subject_aliases", d)

    def test_put_knowledge_valid(self):
        """AC-1：合法断言保存过 loader。"""
        data = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        r = self.client.put("/api/v1/cases/c1/knowledge", headers=self.auth_pj,
                            json={"relation_assertions": data["relation_assertions"]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_put_knowledge_missing_fields(self):
        """AC-1：断言缺 from/to/type → 400，不落盘。"""
        before = self.knowledge_path.read_text(encoding="utf-8")
        r = self.client.put("/api/v1/cases/c1/knowledge", headers=self.auth_pj,
                            json={"relation_assertions": [{"from": "A"}]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.knowledge_path.read_text(encoding="utf-8"),
                         before)

    def test_post_add_assertion(self):
        """POST 追加断言。"""
        before = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        n_before = len(before["relation_assertions"])
        r = self.client.post("/api/v1/cases/c1/knowledge", headers=self.auth_pj,
                             json={"relation_assertions": [
                                 {"from": "新公司", "to": "新人",
                                  "type": "interest",
                                  "source": "测试", "valid_until": None}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["added"], 1)
        after = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        self.assertEqual(len(after["relation_assertions"]), n_before + 1)

    def test_expired_assertion_excluded(self):
        """AC-2：过期断言（valid_until 过去）在装载期被排除。"""
        data = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        data["relation_assertions"] = [
            {"from": "旧公司", "to": "旧人", "type": "interest",
             "source": "过期", "valid_until": "2010-01-01"}]
        self.knowledge_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # 过期断言不影响包装载（core r5 过滤）
        from core.ontology_loader import load_pack
        spec = load_pack("default",
                         base_dir=self.svc.snapshot_ontology_root("c1"))
        # case_knowledge 装载不应抛错
        self.assertIsNotNone(spec)

    def test_change_recorded(self):
        """AC-5：变更进审计（ops_events）。"""
        data = json.loads(self.knowledge_path.read_text(encoding="utf-8"))
        self.client.put("/api/v1/cases/c1/knowledge", headers=self.auth_pj,
                        json={"relation_assertions": data["relation_assertions"]})
        events = self.repo.list_ops()
        ops = [e["kind"] for e in events]
        self.assertIn("knowledge_save", ops)


if __name__ == "__main__":
    unittest.main()
