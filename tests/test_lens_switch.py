"""
tests/test_lens_switch.py
案件级镜头启停（类比规则工坊）：lenses.json + detect 按快照过滤。

覆盖：
  ① GET 镜头清单：插件镜头全列、内置五技能不列、requires_params/mode
     标记正确、未覆盖时生效值回落包声明；
  ② PUT 启停（偏将）：落 cases/<cid>/lenses.json + 入队 RESCAN（幂等键
     随版本）+ GET 反映 case_override；
  ③ 权限：正兵（clearance<2）403；未知镜头 404；内置技能 400；
  ④ 重复同值 PUT 幂等（文件不重写、RESCAN 同任务）；
  ⑤ detect 集成：run_detection 读案件快照 lenses.json——被停镜头移出
     批量清单、落 lens_case_disabled 留痕（BUILD 产物可审计）。
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
from server.app.meta.models import TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.snapshot_config import (
    lens_overrides_path,
    load_lens_overrides,
)
from server.app.store import StoreFactory
from server.app.worker.detect import run_detection
from server.app.worker.pool import WorkerPool


class LensSwitchTest(unittest.TestCase):
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
                             json={"case_id": "c1", "name": "镜头启停案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.lenses_path = lens_overrides_path(self.factory.case_dir("c1"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _get(self, headers=None):
        return self.client.get("/api/v1/cases/c1/lenses",
                               headers=headers or self.auth_h)

    # ---- ① GET 清单 ---------------------------------------------------
    def test_list_lenses_plugin_only_with_flags(self):
        r = self._get()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        items = {x["skill_id"]: x for x in data["lenses"]}
        # 插件镜头在列（真实 packs：relation/timeline 定向 + vlm 草案）
        for sid in ("relation_neighborhood", "timeline_cross_collision",
                    "vlm_inspect"):
            self.assertIn(sid, items)
        # 内置五技能不受案件启停，不进清单
        for sid in ("xu_shi", "qi_zheng", "yong_jian"):
            self.assertNotIn(sid, items)
        rel = items["relation_neighborhood"]
        self.assertEqual(rel["pack_id"], "relation")
        self.assertEqual(rel["mode"], "deterministic")
        self.assertTrue(rel["requires_params"])   # 定向镜头
        self.assertTrue(rel["pack_enabled"])
        self.assertIsNone(rel["case_override"])   # 未覆盖
        self.assertTrue(rel["enabled"])           # 生效值回落包声明
        self.assertEqual(items["vlm_inspect"]["mode"], "draft")

    # ---- ② PUT 启停 + RESCAN ------------------------------------------
    def test_put_switch_writes_file_and_enqueues_rescan(self):
        r = self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                            headers=self.auth_pj,
                            json={"enabled": False, "reason": "本案不跑关系线"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()["data"]
        self.assertEqual(body["skill_id"], "relation_neighborhood")
        self.assertFalse(body["enabled"])
        # RESCAN 已入队（幂等键随版本）
        self.assertEqual(body["rescan_task"]["task_type"], "RESCAN")
        # lenses.json 落盘
        self.assertEqual(load_lens_overrides(self.factory.case_dir("c1")),
                         {"relation_neighborhood": False})
        on_disk = json.loads(
            self.lenses_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema_version"], 1)
        # GET 反映 case_override 与生效值
        items = {x["skill_id"]: x for x in self._get().json()["data"]["lenses"]}
        self.assertFalse(items["relation_neighborhood"]["case_override"])
        self.assertFalse(items["relation_neighborhood"]["enabled"])
        # 审计留痕（ops_events；case_id 独立列，payload 为 JSON 串）
        ops = self.repo.list_ops(kind="lens_switch")
        self.assertGreaterEqual(len(ops), 1)
        self.assertEqual(ops[0]["case_id"], "c1")
        self.assertEqual(json.loads(ops[0]["payload"])["skill_id"],
                         "relation_neighborhood")

    def test_rescan_task_completes_after_switch(self):
        """启停后 RESCAN 全链路：detect 按快照过滤，任务成功收口。"""
        r = self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                            headers=self.auth_pj, json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        task_id = r.json()["data"]["rescan_task"]["id"]
        self.pool.run_until_drained(max_idle_rounds=40)
        row = next(t for t in self.repo.list_tasks(case_id="c1")
                   if t.id == task_id)
        self.assertEqual(row.status, TASK_SUCCEEDED, row.error_message)

    # ---- ③ 权限与校验 ---------------------------------------------------
    def test_put_requires_analyst(self):
        r = self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                            headers=self.auth_s, json={"enabled": False})
        self.assertEqual(r.status_code, 403, r.text)

    def test_put_unknown_lens_404(self):
        r = self.client.put("/api/v1/cases/c1/lenses/ghost_lens",
                            headers=self.auth_pj, json={"enabled": False})
        self.assertEqual(r.status_code, 404, r.text)

    def test_put_builtin_lens_rejected(self):
        r = self.client.put("/api/v1/cases/c1/lenses/xu_shi",
                            headers=self.auth_pj, json={"enabled": False})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("内置", r.json()["error"]["message"])

    # ---- ④ 幂等 ---------------------------------------------------------
    def test_same_value_put_is_idempotent(self):
        r1 = self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                             headers=self.auth_pj, json={"enabled": False})
        r2 = self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                             headers=self.auth_pj, json={"enabled": False})
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r1.json()["data"]["rescan_task"]["id"],
                         r2.json()["data"]["rescan_task"]["id"])

    # ---- ⑤ detect 按快照过滤 --------------------------------------------
    def test_run_detection_filters_by_case_switch(self):
        # 空库 + 快照目录缺失（run_rules 降级空产物，detect 契约内路径）：
        # 焦点是镜头清单过滤，不依赖数据/规则命中
        self.client.put("/api/v1/cases/c1/lenses/relation_neighborhood",
                        headers=self.auth_pj, json={"enabled": False})
        det = run_detection(
            version_file=self.tmp / "v1.duckdb",
            case_dir=self.factory.case_dir("c1"), version=1,
            pack="default", snapshot_base=self.tmp / "no_ontology")
        self.assertEqual(det["lens_case_disabled"],
                         ["relation_neighborhood"])
        self.assertNotIn("relation_neighborhood", det["lens_batch_skipped"])

    def test_run_detection_without_switch_keeps_defaults(self):
        det = run_detection(
            version_file=self.tmp / "v1.duckdb",
            case_dir=self.factory.case_dir("c1"), version=1,
            pack="default", snapshot_base=self.tmp / "no_ontology")
        self.assertEqual(det["lens_case_disabled"], [])
        # 判据已由「有没有必填参数」改为「能不能自动确定靶心」：
        # 6 个 relation_*/timeline_* 都声明了 auto_from，靶心可推导时
        # **应被调度**，不再因「有必填参数」整体跳过（此前该断言写死 6）。
        # 故此处只断言：跳过留痕是「推导不出靶心」的集合，且数量 < 镜头总数。
        skipped = det["lens_batch_skipped"]
        self.assertIsInstance(skipped, list)
        self.assertLess(len(skipped), 6,
                        f"靶心可推导的镜头不应进跳过留痕，实际 {skipped}")

    def test_lens_batch_skipped_means_unresolvable_not_directional(self):
        """「跳过」语义变更：= 推导不出靶心，而非「有必填参数」。

        此前 Web 端 detect 按「有必填参数 = 定向」整体跳过六个镜头，
        导致 Web 建案与 CLI（run_all）线索集不一致。本用例守住新语义。
        """
        from core.pack_loader import case_batch_lens_tasks
        from core.registry import get_registry

        reg = get_registry()
        tasks, unresolved, _disabled = case_batch_lens_tasks(
            reg, None, store=None, ctx={"clues": []}, pack="default")
        directional = {"relation_neighborhood", "relation_common_neighbors",
                       "relation_paths", "timeline_sequence",
                       "timeline_rhythm", "timeline_cross_collision"}
        scheduled = {sid for sid, _ in tasks}
        # 已调度的镜头不该同时出现在跳过留痕里
        self.assertFalse(scheduled & set(unresolved),
                         f"同一镜头不可既调度又跳过：{scheduled & set(unresolved)}")
        # 定向不再是跳过理由：能推导靶心的定向镜头应出现在调度中
        self.assertTrue(
            scheduled & directional or not (unresolved or scheduled),
            "靶心可推导时，定向镜头应被批量调度")


if __name__ == "__main__":
    unittest.main()
