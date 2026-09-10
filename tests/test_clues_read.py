"""
tests/test_clues_read.py
M3 阶段 C：W-019 线索研判读面（列表/详情/suppressed）+ dashboard 待办切 state。

AC 对应：
  AC-1 列表按级别/维度/间类/主体/状态筛选 + 优先级排序 + 分页；
  AC-2 详情给五间/溯源 source_rows/审计留痕；
  AC-3 处置后列表状态取自 state 真值（覆盖产物旧状态）；
  AC-4 被抑制结果不删除，suppressed 端点可查；
  AC-5 合并线索详情展示 merged_from；
  AC-6 读面只读已产出结果，不触发任何任务/重扫；
  秩级：正兵及以下不见内间线索（REQ-011 延续，详情 fail-closed 404）。
另：dashboard 待办计数切 state.sqlite（source=state）。
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

from core.registry import ClueStatus, LineageClue

from server.app.cases import CaseService
from server.app.clues_artifact import save_case_clues
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool


def _clue(cid, title, *, jian=("生间",), level="高", dim="资金",
          score=0, rank=9, source_rows=None, detail_extra=None,
          merged_from=None):
    det = {"级别": level, "维度": dim, "priority_score": score,
           "priority_rank": rank}
    if merged_from:
        det["merged_from"] = merged_from
    if detail_extra:
        det.update(detail_extra)
    return LineageClue(
        clue_id=cid, skill_id="xu_shi", title=title, jian_types=list(jian),
        detail=det,
        source_rows=source_rows or [{"file": "b.parquet", "row_id": 1}])


def _seed_clues():
    return [
        _clue("clue-a", "大额取现 100 万", level="高", dim="资金",
              score=90, rank=1),
        _clue("clue-b", "内间通话高频", jian=("内间",), level="高",
              dim="通讯", score=95, rank=0),
        _clue("clue-c", "轨迹伴行", jian=("生间", "因间"), level="中",
              dim="轨迹", score=50, rank=3,
              merged_from=["clue-c-1", "clue-c-2"],
              detail_extra={"suppressed_log": [
                  {"rule_id": "R2-alt", "clue_id": "clue-c-alt",
                   "reason": "exclusive_group 主规则命中，抑制备选",
                   "suppressed_by_group": "G2",
                   "suppressed_by_rule": "R2"}]}),
        _clue("clue-d", "低额零散", level="低", dim="资金", score=10, rank=5),
    ]


class CluesReadTest(unittest.TestCase):
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
        salt3, h3 = hash_password("pw-li")
        self.repo.create_user(User(operator="李检", password_hash=h3,
                                   salt=salt3, role="偏将", clearance=2,
                                   tenant_id="t2"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        self.auth_l = self._login("李检", "pw-li")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def make_case(self, case_id="c1", clues=None, version=1):
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": case_id, "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version(case_id, version, "test")
        save_case_clues(self.factory.case_dir(case_id), version,
                        clues if clues is not None else _seed_clues())

    def _dispose(self, clue_id, action, headers=None, **extra):
        body = {"action": action, **extra}
        r = self.client.post(f"/api/v1/cases/c1/clues/{clue_id}/actions",
                             headers=headers or self.auth_h, json=body)
        self.assertEqual(r.status_code, 202, r.text)
        self.pool.run_until_drained(max_idle_rounds=5)

    # ------------------------------------------------------------------
    # AC-1：列表 + 排序 + 筛选 + 分页
    # ------------------------------------------------------------------
    def test_list_sorted_and_paginated(self):
        self.make_case("c1")
        r = self.client.get("/api/v1/cases/c1/clues", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(data["available"])
        self.assertEqual(data["total"], 4)
        ids = [i["clue_id"] for i in data["items"]]
        # priority_score 降序：b(95) > a(90) > c(50) > d(10)
        self.assertEqual(ids, ["clue-b", "clue-a", "clue-c", "clue-d"])
        # 分页
        r = self.client.get("/api/v1/cases/c1/clues?page=2&page_size=2",
                            headers=self.auth_h)
        data = r.json()["data"]
        self.assertEqual([i["clue_id"] for i in data["items"]],
                         ["clue-c", "clue-d"])
        self.assertEqual(data["total"], 4)

    def test_list_filters(self):
        self.make_case("c1")
        # 级别
        r = self.client.get("/api/v1/cases/c1/clues?level=高",
                            headers=self.auth_h)
        self.assertEqual({i["clue_id"] for i in r.json()["data"]["items"]},
                         {"clue-a", "clue-b"})
        # 维度
        r = self.client.get("/api/v1/cases/c1/clues?dimension=资金",
                            headers=self.auth_h)
        self.assertEqual({i["clue_id"] for i in r.json()["data"]["items"]},
                         {"clue-a", "clue-d"})
        # 间类
        r = self.client.get("/api/v1/cases/c1/clues?jian=因间",
                            headers=self.auth_h)
        self.assertEqual({i["clue_id"] for i in r.json()["data"]["items"]},
                         {"clue-c"})
        # 主体（标题关键词）
        r = self.client.get("/api/v1/cases/c1/clues?subject=取现",
                            headers=self.auth_h)
        self.assertEqual({i["clue_id"] for i in r.json()["data"]["items"]},
                         {"clue-a"})

    def test_status_filter_after_dispose(self):
        self.make_case("c1")
        self._dispose("clue-a", "verify")
        # status 筛选走 state 真值
        r = self.client.get(
            f"/api/v1/cases/c1/clues?status={ClueStatus.VERIFYING}",
            headers=self.auth_h)
        items = r.json()["data"]["items"]
        self.assertEqual([i["clue_id"] for i in items], ["clue-a"])
        self.assertEqual(items[0]["status_source"], "state")

    # ------------------------------------------------------------------
    # AC-2/5：详情 + 溯源 + 合并来源
    # ------------------------------------------------------------------
    def test_detail_source_rows_and_merge(self):
        self.make_case("c1")
        r = self.client.get("/api/v1/cases/c1/clues/clue-c",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["merged_from"], ["clue-c-1", "clue-c-2"])  # AC-5
        self.assertTrue(d["source_rows"])  # AC-2 溯源
        self.assertEqual(d["jian_types"], ["生间", "因间"])
        # 无此线索 → 404
        r = self.client.get("/api/v1/cases/c1/clues/clue-ghost",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # AC-3：处置后状态覆盖 + 决策副作用入详情
    # ------------------------------------------------------------------
    def test_state_status_overlay_and_decision(self):
        self.make_case("c1")
        # 处置前：状态全来自产物（state 表尚不存在）
        r = self.client.get("/api/v1/cases/c1/clues/clue-a",
                            headers=self.auth_h)
        d = r.json()["data"]
        self.assertEqual(d["status"], ClueStatus.PENDING)
        self.assertEqual(d["status_source"], "artifact")
        # 处置 clue-d 后：state 状态镜像生效（save_statuses 全量镜像本版线索）
        self._dispose("clue-d", "verify")
        r = self.client.get("/api/v1/cases/c1/clues/clue-d",
                            headers=self.auth_h)
        d = r.json()["data"]
        self.assertEqual(d["status"], ClueStatus.VERIFYING)
        self.assertEqual(d["status_source"], "state")
        self.assertEqual(d["operator"], "王检察官")
        # 同版其他线索：镜像行存在但为默认待查（operator 空）
        r = self.client.get("/api/v1/cases/c1/clues/clue-a",
                            headers=self.auth_h)
        d = r.json()["data"]
        self.assertEqual(d["status"], ClueStatus.PENDING)
        self.assertEqual(d["status_source"], "state")
        self.assertEqual(d["operator"], "")

    # ------------------------------------------------------------------
    # AC-4：suppressed 端点
    # ------------------------------------------------------------------
    def test_suppressed_listing(self):
        self.make_case("c1")
        r = self.client.get("/api/v1/cases/c1/clues/suppressed",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["rule_id"], "R2-alt")
        self.assertEqual(data["items"][0]["host_clue_id"], "clue-c")

    # ------------------------------------------------------------------
    # 秩级：内间线索过滤（REQ-011）
    # ------------------------------------------------------------------
    def test_neijian_hidden_from_soldier(self):
        self.make_case("c1")
        # 正兵：列表不见内间 clue-b
        r = self.client.get("/api/v1/cases/c1/clues", headers=self.auth_s)
        ids = {i["clue_id"] for i in r.json()["data"]["items"]}
        self.assertNotIn("clue-b", ids)
        self.assertIn("access_note", r.json()["data"])
        # 详情 fail-closed 404（不可枚举）
        r = self.client.get("/api/v1/cases/c1/clues/clue-b",
                            headers=self.auth_s)
        self.assertEqual(r.status_code, 404)
        # human 全见
        r = self.client.get("/api/v1/cases/c1/clues", headers=self.auth_h)
        ids = {i["clue_id"] for i in r.json()["data"]["items"]}
        self.assertIn("clue-b", ids)

    # ------------------------------------------------------------------
    # AC-6：读面不触发任务
    # ------------------------------------------------------------------
    def test_reads_do_not_create_tasks(self):
        self.make_case("c1")
        self.client.get("/api/v1/cases/c1/clues", headers=self.auth_h)
        self.client.get("/api/v1/cases/c1/clues/clue-a", headers=self.auth_h)
        self.client.get("/api/v1/cases/c1/clues/suppressed",
                        headers=self.auth_h)
        self.client.get("/api/v1/cases/c1/clues?level=高&status=待查",
                        headers=self.auth_h)
        self.assertEqual(self.repo.list_tasks(case_id="c1"), [])

    # ------------------------------------------------------------------
    # 无产物 / 跨租户 / 未登录
    # ------------------------------------------------------------------
    def test_no_artifact_degrades(self):
        # 有版本指针但无线索产物（BUILD 未产出/未运行）：降级空列表不报错
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c2", "name": "空案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version("c2", 1, "test")
        r = self.client.get("/api/v1/cases/c2/clues", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertFalse(data["available"])
        self.assertEqual(data["items"], [])

    def test_cross_tenant_404(self):
        self.make_case("c1")
        r = self.client.get("/api/v1/cases/c1/clues", headers=self.auth_l)
        self.assertEqual(r.status_code, 404)

    def test_no_token_401(self):
        self.make_case("c1")
        r = self.client.get("/api/v1/cases/c1/clues")
        self.assertEqual(r.status_code, 401)

    # ------------------------------------------------------------------
    # dashboard 待办计数切 state
    # ------------------------------------------------------------------
    def test_dashboard_disposal_counts_from_state(self):
        self.make_case("c1")
        self._dispose("clue-a", "verify")
        # D1：confirm 声明 only_from=["查证中"]，待查态不可直接固证
        self._dispose("clue-b", "verify")
        self._dispose("clue-b", "confirm")
        r = self.client.get("/api/v1/cases/c1/dashboard", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        disposal = r.json()["data"]["todo"]["disposal"]
        self.assertEqual(disposal["source"], "state")
        self.assertTrue(disposal["available"])
        # state 状态镜像覆盖本版全部线索（save_statuses 全量 upsert）
        self.assertEqual(disposal["total"], 4)
        self.assertEqual(disposal["by_status"][ClueStatus.VERIFYING], 1)
        self.assertEqual(disposal["by_status"][ClueStatus.CONFIRMED], 1)
        self.assertEqual(disposal["by_status"][ClueStatus.PENDING], 2)


if __name__ == "__main__":
    unittest.main()
