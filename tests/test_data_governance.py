"""
tests/test_data_governance.py
M4 阶段 B：W-011 可选属性缺列降级（数据治理页）。

AC：
  AC-1 缺列降级警告可检索；
  AC-2 必填缺列仍硬失败（不产生诊断，BUILD 直接失败）；
  AC-3 其余属性正常物化；
  AC-4 降级后可查询缺列为 NULL；
  AC-5 不崩溃。
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

from core.run_health import RunHealth

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool

from tests.test_ingest_api import FLOW_MAP, flow_df


class DataGovernanceTest(unittest.TestCase):
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
        self.auth_h = self._login("王检察官", "pw-pro")
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

    def _build_case(self):
        df = flow_df(3)
        up = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("流水.csv", df.to_csv(index=False).encode("utf-8"))})
        self.assertEqual(up.status_code, 200, up.text)
        uid = up.json()["data"]["upload_id"]
        imp = self.client.post(
            f"/api/v1/cases/c1/sources/{uid}/import", headers=self.auth_h,
            json={"target_table": "银行流水", "column_map": FLOW_MAP})
        self.assertEqual(imp.status_code, 200, imp.text)
        self.pool.run_until_drained(max_idle_rounds=30)

    def test_missing_columns_empty_before_build(self):
        r = self.client.get("/api/v1/cases/c1/governance/missing-columns",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["total_warnings"], 0)

    def test_missing_columns_aggregates_diagnostics(self):
        """AC-1：缺列/脏值降级警告可检索并按对象属性聚合。"""
        self._build_case()
        ver = self.repo.current_version("c1")
        store = self.factory.for_case("c1", mode="write", version=ver)
        try:
            h = RunHealth(store.read_conn)
            h.record("source_column_missing", "warning", source="build_ontology",
                     reason="可选列缺失", object="transaction",
                     property="amount", detail={})
            h.record("source_value_cast_failed", "warning",
                     source="build_ontology", reason="脏值降级",
                     object="transaction", property="date", detail={})
        finally:
            store.close()

        r = self.client.get("/api/v1/cases/c1/governance/missing-columns",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["total_warnings"], 2)
        objs = {it["object"] for it in d["items"]}
        self.assertIn("transaction", objs)

    def test_missing_columns_does_not_crash(self):
        """AC-5：缺列降级不崩溃，端点正常返回。"""
        self._build_case()
        r = self.client.get("/api/v1/cases/c1/governance/missing-columns",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
