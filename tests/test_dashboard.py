"""
tests/test_dashboard.py
M2 阶段 E：W-018 治理仪表盘（首屏健康度/诊断类别/双覆盖/待办计数/ops 摘要/下钻）。

AC 对应：
  AC-1 首屏健康度 healthy/degraded/critical + 计数；
  AC-2 诊断类别全覆盖（零命中/跳过/覆盖缺口/版本锚定/脏值）；
  AC-3 双覆盖并列（声明覆盖 miaosuan:dimension + 实证覆盖 :empirical）；
  AC-4 缺口文案含具体维度名（reason + detail.missing 透传）；
  AC-5 待办计数（处置留痕聚合；实体裁决候选 M3 接线前降级 available=False）；
  AC-6 诊断可下钻（kind/severity 筛选 + seq 详情）。
另含：ops_events 摘要（孤儿/回收/归档，按案件过滤）、跨租户 404、
未 BUILD 案件逐节降级不 500。
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

from core.lineage import save_statuses
from core.registry import LineageClue
from core.run_health import RunHealth

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool


def _record(conn, run_id: str) -> RunHealth:
    """在写连接上落一组诊断（测试注入用；生产路径来自 BUILD/管线）。"""
    rh = RunHealth(conn, run_id=run_id)
    rh.record("rule_zero_hit", "warning", source="rules:R1",
              reason="R1 零命中（clean_scan）", rule_id="R1",
              zero_type="clean_scan")
    rh.record("entity_table_skipped", "warning", source="entity:person",
              reason="obj_person 缺列跳过")
    rh.record("coverage_gap", "warning", source="miaosuan:dimension",
              reason="资金、时间 维度无线索支撑", missing=["资金", "时间"])
    rh.record("coverage_gap", "warning", source="miaosuan:dimension:empirical",
              reason="通讯 维度实证缺失", missing=["通讯"])
    rh.record("version_anchor_missing", "warning", source="audit",
              reason="版本锚点缺失")
    rh.record("source_value_cast_failed", "warning", source="build",
              reason="TRY_CAST 降级 3 行")
    return rh


class DashboardTest(unittest.TestCase):
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
                                   tenant_id="t2"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.05, backoff_base=0.05)
        self.pool.start()
        self.auth_w = self._login("王检", "pw-wang")
        self.auth_l = self._login("李检", "pw-li")

    def tearDown(self):
        self.pool.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def make_case(self, case_id: str = "c1") -> None:
        r = self.client.post("/api/v1/cases", headers=self.auth_w,
                             json={"case_id": case_id, "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def build_version(self, case_id: str, version: int = 1,
                      with_diagnostics: bool = True,
                      statuses: list[LineageClue] | None = None) -> None:
        """构造版本文件：诊断行 + 可选处置留痕。"""
        store = self.factory.for_case(case_id, mode="write", version=version)
        conn = store.write_conn
        try:
            if with_diagnostics:
                _record(conn, f"run-v{version}")
            if statuses:
                save_statuses(conn, statuses)
        finally:
            store.close()
        self.repo.set_version(case_id, version, "test")

    # ------------------------------------------------------------------
    # AC-1 健康度横幅
    # ------------------------------------------------------------------
    def test_health_banner_degraded_and_counts(self):
        self.make_case("c1")
        self.build_version("c1")
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_w)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        # 6 warning 0 critical → degraded
        self.assertEqual(data["health"]["status"], "degraded")
        self.assertTrue(data["health"]["available"])
        self.assertEqual(data["health"]["计数"]["warning"], 6)
        self.assertEqual(data["health"]["计数"]["critical"], 0)
        self.assertEqual(data["diagnostics"]["total"], 6)

    def test_health_banner_healthy_when_no_diagnostics(self):
        self.make_case("c1")
        self.build_version("c1", with_diagnostics=False)
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_w)
        data = r.json()["data"]
        # 表存在但零诊断 → healthy（非降级假象：available=True 但计数为 0）
        self.assertEqual(data["health"]["status"], "healthy")
        self.assertEqual(data["health"]["诊断总数"], 0)

    def test_health_banner_critical(self):
        self.make_case("c1")
        store = self.factory.for_case("c1", mode="write", version=1)
        try:
            rh = RunHealth(store.write_conn, run_id="run-x")
            rh.record("audit_integrity_gap", "critical", source="audit:chain",
                      reason="断链 1 条")
        finally:
            store.close()
        self.repo.set_version("c1", 1, "test")
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_w)
        self.assertEqual(r.json()["data"]["health"]["status"], "critical")

    # ------------------------------------------------------------------
    # AC-2 诊断类别全覆盖 + AC-3/4 双覆盖
    # ------------------------------------------------------------------
    def test_diagnostic_kinds_and_dual_coverage(self):
        self.make_case("c1")
        self.build_version("c1")
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_w)
        data = r.json()["data"]
        by_kind = data["diagnostics"]["by_kind"]
        # AC-2 五类诊断全覆盖（coverage_gap 为声明+实证两行）
        for k, n in (("rule_zero_hit", 1), ("entity_table_skipped", 1),
                     ("coverage_gap", 2), ("version_anchor_missing", 1),
                     ("source_value_cast_failed", 1)):
            self.assertEqual(by_kind.get(k, 0), n, f"诊断类别 {k} 计数不符")
        # AC-3 双覆盖并列
        cov = data["coverage"]
        self.assertEqual(len(cov["declared"]), 1)
        self.assertEqual(len(cov["empirical"]), 1)
        # AC-4 缺口文案含具体维度名
        self.assertEqual(cov["declared"][0]["missing"], ["资金", "时间"])
        self.assertIn("资金", cov["declared"][0]["reason"])
        self.assertEqual(cov["empirical"][0]["missing"], ["通讯"])

    # ------------------------------------------------------------------
    # AC-5 待办计数 + ops 摘要
    # ------------------------------------------------------------------
    def test_todo_counts_and_ops_summary(self):
        self.make_case("c1")
        clues = [LineageClue(clue_id="clue_a", title="整数存入", status="查证中"),
                 LineageClue(clue_id="clue_b", title="过桥资金", status="已排除")]
        self.build_version("c1", statuses=clues)
        self.repo.record_ops("orphan_scan", "c1", {"orphans": 2})
        self.repo.record_ops("version_reclaimed", "c1", {"version": 3})
        self.repo.record_ops("orphan_scan", "c2", {"orphans": 9})  # 他案不入
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_w)
        data = r.json()["data"]
        todo = data["todo"]["disposal"]
        self.assertTrue(todo["available"])
        self.assertEqual(todo["total"], 2)
        self.assertEqual(todo["by_status"]["查证中"], 1)
        self.assertEqual(todo["by_status"]["已排除"], 1)
        self.assertEqual(todo["by_status"]["待查"], 0)
        # 实体裁决候选无持久化读面 → 显式降级
        self.assertFalse(data["todo"]["review"]["available"])
        # ops 摘要：本案件事件在内，他案被过滤
        self.assertIn("orphan_scan", data["ops"]["by_kind"])
        self.assertIn("version_reclaimed", data["ops"]["by_kind"])
        for e in data["ops"]["recent"]:
            self.assertIn(e["case_id"], ("", "c1"))

    def test_todo_degraded_when_no_disposal_table(self):
        self.make_case("c1")
        self.build_version("c1")  # 无 clue_disposal_status
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_w)
        todo = r.json()["data"]["todo"]["disposal"]
        self.assertFalse(todo["available"])
        self.assertEqual(todo["total"], 0)

    # ------------------------------------------------------------------
    # AC-6 诊断下钻
    # ------------------------------------------------------------------
    def test_diagnostics_filter_and_detail(self):
        self.make_case("c1")
        self.build_version("c1")
        # kind 筛选
        r = self.client.get("/api/v1/cases/c1/diagnostics?kind=coverage_gap",
                            headers=self.auth_w)
        items = r.json()["data"]["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["kind"] == "coverage_gap" for i in items))
        # severity 筛选
        r = self.client.get("/api/v1/cases/c1/diagnostics?severity=critical",
                            headers=self.auth_w)
        self.assertEqual(r.json()["data"]["items"], [])
        # 下钻：seq=1 详情（detail JSON 已解析为 dict）
        r = self.client.get("/api/v1/cases/c1/diagnostics/1",
                            headers=self.auth_w)
        self.assertEqual(r.status_code, 200, r.text)
        row = r.json()["data"]
        self.assertEqual(row["seq"], 1)
        self.assertEqual(row["kind"], "rule_zero_hit")
        self.assertEqual(row["detail"]["rule_id"], "R1")
        # 下钻 404
        r = self.client.get("/api/v1/cases/c1/diagnostics/999",
                            headers=self.auth_w)
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # 边界：跨租户 404 / 未 BUILD 降级
    # ------------------------------------------------------------------
    def test_cross_tenant_404_and_unbuilt_degrade(self):
        self.make_case("c1")
        # 跨租户 404（李检属 t2）
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_l)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/v1/cases/c1/diagnostics", headers=self.auth_l)
        self.assertEqual(r.status_code, 404)
        # 已建案未 BUILD：逐节降级不 500
        self.make_case("c9")
        r = self.client.get("/api/v1/cases/c9/dashboard", headers=self.auth_w)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertFalse(data["health"]["available"])
        self.assertEqual(data["diagnostics"]["total"], 0)
        self.assertEqual(data["coverage"], {"declared": [], "empirical": []})
        self.assertFalse(data["todo"]["disposal"]["available"])
        # 未 BUILD 下钻 → 空列表 / 404
        r = self.client.get("/api/v1/cases/c9/diagnostics", headers=self.auth_w)
        self.assertEqual(r.json()["data"]["items"], [])
        r = self.client.get("/api/v1/cases/c9/diagnostics/1", headers=self.auth_w)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
