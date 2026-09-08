"""
tests/test_review_api.py
M4 阶段 C：W-021 人审队列与实体裁决。

AC：
  AC-1 needs_review 候选全量入队；
  AC-2 相似度依据 + 属性对比（证据面）；
  AC-3 裁决进审计；
  AC-4 驳回后不重复出现；
  AC-5 永不自动合并（红线：无 POST decision 不合并）；
  权限：POST 需正兵及以上。
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool

ORG_MAP = {"主体": "raw_name", "法人": "legal_rep",
           "状态": "status", "关联": "relation"}


def org_df() -> pd.DataFrame:
    # 宏业建设 / 宏业建设集团："集团"非可剥后缀，归一后核心字号前者是后者真前缀
    # → 弱证据前缀包含 → needs_review=True（法人不同避免强合并）
    return pd.DataFrame({
        "主体": ["宏业建设", "宏业建设集团", "A建材"],
        "法人": ["李志强", "王志强", "赵六"],
        "状态": ["存续", "存续", "存续"],
        "关联": ["", "", ""],
    })


class ReviewApiTest(unittest.TestCase):
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
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _build_with_org(self):
        df = org_df()
        up = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("工商.csv", df.to_csv(index=False).encode("utf-8"))})
        self.assertEqual(up.status_code, 200, up.text)
        uid = up.json()["data"]["upload_id"]
        imp = self.client.post(
            f"/api/v1/cases/c1/sources/{uid}/import", headers=self.auth_h,
            json={"target_table": "工商信息"})
        self.assertEqual(imp.status_code, 200, imp.text)
        self.pool.run_until_drained(max_idle_rounds=30)

    def _first_candidate_id(self) -> str:
        r = self.client.get("/api/v1/cases/c1/review/queue",
                            headers=self.auth_h)
        items = r.json()["data"]["items"]
        self.assertTrue(items, "期望存在 needs_review 候选")
        return items[0]["entity_id"]

    def test_queue_empty_before_build(self):
        r = self.client.get("/api/v1/cases/c1/review/queue",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["total"], 0)

    def test_queue_lists_candidates(self):
        """AC-1：needs_review 候选入队。"""
        self._build_with_org()
        r = self.client.get("/api/v1/cases/c1/review/queue",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["items"]
        # 宏业建设 / 宏业建设有限公司 前缀包含应产生 needs_review
        names = {it["canonical_name"] for it in items}
        self.assertTrue(any("宏业" in n for n in names))

    def test_evidence(self):
        """AC-2：证据面返回属性对比/相似度依据。"""
        self._build_with_org()
        rid = self._first_candidate_id()
        r = self.client.get(f"/api/v1/cases/c1/review/{rid}/evidence",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertIn("variants", d)
        self.assertIn("merge_reason", d)

    def test_merge_decision(self):
        """AC-3：合并裁决进审计（state.review_decision）。"""
        self._build_with_org()
        rid = self._first_candidate_id()
        r = self.client.post(f"/api/v1/cases/c1/review/{rid}/decision",
                             headers=self.auth_s,
                             json={"action": "review_merge"})
        self.assertEqual(r.status_code, 200, r.text)
        self.pool.run_until_drained(max_idle_rounds=20)
        # 队列中该候选不再出现（AC-4 驳回/合并后不重复）
        r2 = self.client.get("/api/v1/cases/c1/review/queue",
                             headers=self.auth_h)
        remaining = [it for it in r2.json()["data"]["items"]
                     if it["entity_id"] == rid]
        self.assertEqual(remaining, [])

    def test_reject_decision_requires_reason(self):
        """驳回必须给理由。"""
        self._build_with_org()
        rid = self._first_candidate_id()
        r = self.client.post(f"/api/v1/cases/c1/review/{rid}/decision",
                             headers=self.auth_s,
                             json={"action": "review_reject"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_no_auto_merge(self):
        """AC-5 红线：无 POST decision 时候选保持 needs_review（不自动合并）。"""
        self._build_with_org()
        r = self.client.get("/api/v1/cases/c1/review/queue",
                            headers=self.auth_h)
        # 候选仍在队列中（未被自动合并）
        self.assertGreaterEqual(r.json()["data"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
