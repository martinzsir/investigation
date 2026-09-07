"""
tests/test_m3_chain.py
M3 阶段 F（计划步骤 24）：研判主流程全链路冒烟（TestClient 端到端）。

建案 → 上传 CSV → 映射确认 → IMPORT+BUILD → 线索产物/列表/详情 →
正兵 verify（落 state 链）→ file 三拒（正兵 403 / 缺 legal_basis 400 /
状态未到已固证 ACTION_REJECTED）→ human confirm → human+legal_basis file 成功 →
审计时间线双源可见处置事件 → 链自检 state 源 → 仪表盘计数切 state →
规则工坊调参 → RESCAN → v2 产物。

数据：整万元转账（10000/20000/30000）→ R2 integer_transfer_aggregates 必中。
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
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool

from tests.test_ingest_api import FLOW_MAP, flow_df


class M3ChainTest(unittest.TestCase):
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
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "全链路案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _drain(self):
        self.pool.run_until_drained(max_idle_rounds=40)

    def _action(self, clue_id, action, headers, **params):
        body = {"action": action, **params}
        return self.client.post(
            f"/api/v1/cases/c1/clues/{clue_id}/actions",
            headers=headers, json=body)

    def test_research_main_flow_chain(self):
        # ---- 1. 上传 → 导入 → IMPORT+BUILD -------------------------------
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("流水.csv",
                            flow_df(3).to_csv(index=False).encode("utf-8"))})
        self.assertEqual(r.status_code, 200, r.text)
        up = r.json()["data"]
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up['upload_id']}/import",
            headers=self.auth_h,
            json={"target_table": "银行流水", "column_map": FLOW_MAP,
                  "clean": ["strip"]})
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()

        tasks = self.repo.list_tasks(case_id="c1")
        status = {t.task_type: t.status for t in tasks}
        self.assertEqual(status.get("IMPORT"), TASK_SUCCEEDED,
                         [t.error_message for t in tasks
                          if t.task_type == "IMPORT"])
        self.assertEqual(status.get("BUILD"), TASK_SUCCEEDED,
                         [t.error_message for t in tasks
                          if t.task_type == "BUILD"])
        # D-M3-2：线索报告产物随版本落盘
        art_dir = self.factory.case_dir("c1") / "artifacts"
        arts = sorted(art_dir.glob("clues_v*.json"))
        self.assertEqual(len(arts), 1)

        # ---- 2. 线索列表/详情 ---------------------------------------------
        r = self.client.get("/api/v1/cases/c1/clues", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["items"]
        self.assertGreaterEqual(len(items), 1)
        clue_id = items[0]["clue_id"]
        r = self.client.get(f"/api/v1/cases/c1/clues/{clue_id}",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["clue_id"], clue_id)

        # ---- 3. 正兵 verify（落 state 链）---------------------------------
        r = self._action(clue_id, "verify", self.auth_s, note="调取流水核实")
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        r = self.client.get(f"/api/v1/cases/c1/clues/{clue_id}",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["status"], "查证中")

        # ---- 4. file 三拒 --------------------------------------------------
        # 4a 正兵/AI 无权立案 → 403（API 前置红线）
        r = self._action(clue_id, "file", self.auth_s,
                         legal_basis="杭检立〔2026〕1号")
        self.assertEqual(r.status_code, 403)
        # 4b human 缺 legal_basis → 400
        r = self._action(clue_id, "file", self.auth_h)
        self.assertEqual(r.status_code, 400)
        # 4c human 带 legal 但线索未到已固证 → 入队后 Worker 状态机拒绝
        r = self._action(clue_id, "file", self.auth_h,
                         legal_basis="杭检立〔2026〕1号")
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        failed = [t for t in self.repo.list_tasks(case_id="c1")
                  if t.task_type == "DISPOSE" and t.status == TASK_FAILED]
        self.assertTrue(failed)
        self.assertEqual(failed[-1].error_code, "ACTION_REJECTED")

        # ---- 5. human confirm → file 成功 ---------------------------------
        r = self._action(clue_id, "confirm", self.auth_h, note="过桥结构成立")
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        # 显式 idem_key：前序 file 因状态机失败占了缺省幂等键，重试需新键
        r = self._action(clue_id, "file", self.auth_h,
                         legal_basis="杭检立〔2026〕1号",
                         idem_key="file-after-confirm")
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        r = self.client.get(f"/api/v1/cases/c1/clues/{clue_id}",
                            headers=self.auth_h)
        self.assertEqual(r.json()["data"]["status"], "已立案")

        # ---- 6. 审计时间线（D-M3-5 双源拼接）+ 链自检 --------------------
        r = self.client.get("/api/v1/cases/c1/audit", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        events = r.json()["data"]["items"]
        disposal = [e for e in events if e["action"] == "disposal"]
        self.assertGreaterEqual(len(disposal), 3)
        self.assertTrue(any(e.get("chain_source") == "state"
                            for e in disposal))
        filed = [e for e in disposal if e["status_to"] == "已立案"]
        self.assertTrue(filed)
        self.assertEqual(filed[-1]["legal_basis"], "杭检立〔2026〕1号")

        r = self.client.post("/api/v1/cases/c1/audit/verify",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        verify = r.json()["data"]
        self.assertEqual(verify["chain_source"], "state")
        self.assertTrue(verify["chain_ok"], verify)

        # ---- 7. 仪表盘处置卡切 state 真值 ---------------------------------
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        card = r.json()["data"]["todo"]["disposal"]
        self.assertEqual(card["source"], "state")
        self.assertEqual(card.get("by_status", {}).get("已立案"), 1)

        # ---- 8. 规则工坊：正兵只读禁写；human 调参 → RESCAN -------------
        r = self.client.get("/api/v1/cases/c1/rules", headers=self.auth_s)
        self.assertEqual(r.status_code, 200)
        r = self.client.put("/api/v1/cases/c1/rules/R2",
                            headers=self.auth_s,
                            json={"params": {"round_unit": 5000}})
        self.assertEqual(r.status_code, 403)
        r = self.client.put("/api/v1/cases/c1/rules/R2",
                            headers=self.auth_h,
                            json={"params": {"round_unit": 5000}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["data"]["rescan_task"])
        self._drain()
        # RESCAN 复用 BUILD 编排 → v2 + 新线索产物
        self.assertEqual(self.repo.current_version("c1"), 2)
        arts = sorted(art_dir.glob("clues_v*.json"))
        self.assertEqual(len(arts), 2)


if __name__ == "__main__":
    unittest.main()
