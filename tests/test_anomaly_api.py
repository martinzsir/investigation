"""
tests/test_anomaly_api.py
M4 阶段 B：W-022 异常线索通道。

AC：
  AC-1 异常线索独立通道；
  AC-2 级别恒"待核实"；
  AC-3 携带 diagnostic_ids；
  AC-4 不参与五间交叉等级计算（core 硬编码，Web 仅分区展示）；
  AC-5 按主体聚合；
  AC-6 同构进审计。
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


class AnomalyApiTest(unittest.TestCase):
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

    def test_anomalies_empty_before_build(self):
        r = self.client.get("/api/v1/cases/c1/anomalies", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["total"], 0)

    def test_anomalies_from_diagnostics(self):
        """AC-1/2/3/5：诊断转异常线索，级别待核实、带 diagnostic_ids、按主体聚合。"""
        self._build_case()
        # 插入一条 coverage_gap 诊断（异常通道 eligible 类型）
        ver = self.repo.current_version("c1")
        store = self.factory.for_case("c1", mode="write", version=ver)
        try:
            h = RunHealth(store.read_conn)
            h.record("coverage_gap", "warning", source="miao",
                     reason="庙算维度覆盖不足",
                     subject="张三", missing=["生间", "内间"])
        finally:
            store.close()

        r = self.client.get("/api/v1/cases/c1/anomalies", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["level"], "待核实")  # AC-2
        self.assertGreaterEqual(d["total"], 1)
        item = d["items"][0]
        self.assertTrue(item.get("is_anomaly"))  # AC-1 独立通道
        self.assertIn("diagnostic_ids", item)     # AC-3
        self.assertIn("张三", item.get("subject", ""))  # AC-5 按主体

    def test_anomalies_not_mixed_with_normal_clues(self):
        """AC-4：异常线索不参与五间交叉（端点独立返回，不混入正常线索流）。"""
        self._build_case()
        # 异常端点只返回异常，不返回正常规则线索
        r = self.client.get("/api/v1/cases/c1/anomalies", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        for it in r.json()["data"]["items"]:
            self.assertTrue(it.get("is_anomaly"))


if __name__ == "__main__":
    unittest.main()
