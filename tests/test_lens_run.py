"""
tests/test_lens_run.py
画布定向镜头带参调度（TASK_LENS_RUN）：启停文件 requires_params 预留的定向入口。

覆盖：
  ① POST run 202 入队 → Worker 对当前版本跑定向镜头 → lens_runs 补充产物
     落盘 + ops 留痕（空库下镜头隔离降级 degraded，任务仍成功）；
  ② 线索读面并线：lens_runs 产物线索进 GET /clues 列表与详情（主产物
     D-M3-2 不可变，定向线索挂产生它的版本）；
  ③ 权限与边界：正兵 403；未知镜头 404；内置技能/包级停用/草案镜头/
     案件停用 400；未声明参数/必填缺失 400（路由预检不入队）；
  ④ 幂等：同版本同参数重提返回同任务；
  ⑤ 失败路径：无生效版本任务 FAILED（NO_VERSION）。
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

from server.app.clues_artifact import (
    lens_run_dir,
    load_lens_runs,
    save_case_clues,
    save_lens_run,
)
from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.snapshot_config import lens_overrides_path
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool
from core.registry import LineageClue


class LensRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.app = create_app(self.ctx)  # 启动即 discover_packs()（真实 packs）
        self.client = TestClient(self.app)
        for operator, pw, role, clr in (
                ("王检察官", "pw-pro", "human", 4),
                ("张偏将", "pw-pj", "偏将", 2),
                ("李侦查员", "pw-sol", "正兵", 1)):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role, clearance=clr,
                                       tenant_id="t1"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_pj = self._login("张偏将", "pw-pj")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "定向镜头案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.case_dir = self.factory.case_dir("c1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _make_version1(self, *, with_clues_artifact: bool = False):
        """造 v1 版本文件 + 版本指针（不跑真实 BUILD）；可选落主线索产物。"""
        store = self.factory.for_case("c1", mode="write", version=1)
        store.close()  # 打开即自动创建空库文件
        self.repo.set_version("c1", 1, by="test")
        if with_clues_artifact:
            save_case_clues(self.case_dir, 1, [])

    def _run(self, skill_id="relation_neighborhood", headers=None, **kw):
        body = {"params": {"target_subject": "张三"}, **kw}
        return self.client.post(
            f"/api/v1/cases/c1/lenses/{skill_id}/run",
            headers=headers or self.auth_pj, json=body)

    # ---- ① 202 入队 → 补充产物 + ops 留痕 -------------------------------
    def test_run_produces_lens_run_artifact(self):
        self._make_version1()
        r = self._run()
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()["data"]
        self.assertEqual(body["skill_id"], "relation_neighborhood")
        self.assertEqual(body["task"]["task_type"], "LENS_RUN")
        task_id = body["task"]["id"]

        self.pool.run_until_drained(max_idle_rounds=40)
        row = next(t for t in self.repo.list_tasks(case_id="c1")
                   if t.id == task_id)
        self.assertEqual(row.status, TASK_SUCCEEDED, row.error_message)

        runs = load_lens_runs(self.case_dir, 1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["skill_id"], "relation_neighborhood")
        self.assertEqual(runs[0]["params"], {"target_subject": "张三"})
        self.assertEqual(runs[0]["operator"], "张偏将")
        self.assertEqual(runs[0]["version"], 1)
        self.assertIn("clues", runs[0])
        # ops 留痕（含触发人）
        ops = self.repo.list_ops(kind="lens_run")
        self.assertGreaterEqual(len(ops), 1)
        self.assertEqual(ops[0]["case_id"], "c1")

    # ---- ② 读面并线 ------------------------------------------------------
    def test_read_surface_merges_lens_run_clues(self):
        self._make_version1(with_clues_artifact=True)
        clue = LineageClue(skill_id="relation_neighborhood",
                           title="定向：张三 关系圈层", detail={"依据": "邻域"})
        save_lens_run(self.case_dir, 1, run_id="lensrun_t1",
                      skill_id="relation_neighborhood",
                      params={"target_subject": "张三"},
                      operator="张偏将", clues=[clue])
        # 列表并线
        r = self.client.get("/api/v1/cases/c1/clues", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["items"]
        hit = [x for x in items if x["clue_id"] == clue.clue_id]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["skill_id"], "relation_neighborhood")
        self.assertEqual(hit[0]["status"], "待查")
        # 详情可见
        r = self.client.get(
            f"/api/v1/cases/c1/clues/{clue.clue_id}", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["title"], "定向：张三 关系圈层")

    # ---- ③ 权限与边界 ---------------------------------------------------
    def test_run_requires_analyst(self):
        self._make_version1()
        r = self._run(headers=self.auth_s)
        self.assertEqual(r.status_code, 403, r.text)

    def test_run_unknown_lens_404(self):
        self._make_version1()
        r = self._run(skill_id="ghost_lens")
        self.assertEqual(r.status_code, 404, r.text)

    def test_run_builtin_rejected(self):
        self._make_version1()
        r = self.client.post("/api/v1/cases/c1/lenses/xu_shi/run",
                             headers=self.auth_pj, json={"params": {}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("内置", r.json()["error"]["message"])

    def test_run_draft_lens_rejected(self):
        self._make_version1()
        r = self._run(skill_id="vlm_inspect", params={})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("草案", r.json()["error"]["message"])

    def test_run_case_disabled_rejected(self):
        self._make_version1()
        r = self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                            headers=self.auth_pj,
                            json={"enabled": False, "reason": "本案不跑关系线"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self._run()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("停用", r.json()["error"]["message"])

    def test_run_missing_required_param_400(self):
        self._make_version1()
        r = self.client.post("/api/v1/cases/c1/lenses/relation_neighborhood/run",
                             headers=self.auth_pj, json={"params": {}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("target_subject", r.json()["error"]["message"])
        # 必败任务未入队
        self.assertEqual(self.repo.list_tasks(case_id="c1"), [])

    def test_run_unknown_param_400(self):
        self._make_version1()
        r = self.client.post("/api/v1/cases/c1/lenses/relation_neighborhood/run",
                             headers=self.auth_pj,
                             json={"params": {"target_subject": "张三",
                                              "ghost": 1}})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("ghost", r.json()["error"]["message"])

    # ---- ④ 幂等 ---------------------------------------------------------
    def test_same_version_params_idempotent(self):
        self._make_version1()
        r1 = self._run()
        r2 = self._run()
        self.assertEqual(r1.status_code, 202, r1.text)
        self.assertEqual(r2.status_code, 202, r2.text)
        self.assertEqual(r1.json()["data"]["task"]["id"],
                         r2.json()["data"]["task"]["id"])

    # ---- ⑤ 失败路径 -----------------------------------------------------
    def test_run_without_version_fails_task(self):
        r = self._run()  # 版本指针 0：预检全过，Worker 侧 NO_VERSION 失败
        self.assertEqual(r.status_code, 202, r.text)
        task_id = r.json()["data"]["task"]["id"]
        self.pool.run_until_drained(max_idle_rounds=40)
        row = next(t for t in self.repo.list_tasks(case_id="c1")
                   if t.id == task_id)
        self.assertEqual(row.status, TASK_FAILED, row.error_message)
        self.assertEqual(row.error_code, "NO_VERSION")
        # 失败不落补充产物
        self.assertFalse(lens_run_dir(self.case_dir, 1).exists())


if __name__ == "__main__":
    unittest.main()
