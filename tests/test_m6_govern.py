"""
tests/test_m6_govern.py
M6 阶段 E/F（W-P-011~015、W-P-017）：
  W-P-011 数据元配置（GET 快照 / PUT 校验落盘 / 越权 403 / 非法 400 不落盘）；
  W-P-012 ETL 管道（派生视图 / 回写 / validate 冲突与两路出路，无 force 字段）；
  W-P-013 系统设置（queue/resources/health，admin 门禁 + reason 审计）；
  W-P-014 任务取消（仅 PENDING；创建人/admin；409/403/404）；
  W-P-015 门户汇总 + 案件归档（summary 结构 / 状态机 / ARCHIVE 入队）；
  W-P-017 配置中心（snapshots / features 白名单 / 平台阈值，llm 红线键 400）。
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

from core.run_health import RunHealth

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import CASE_ACTIVE, TASK_RUNNING, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory

API = "/api/v1"


class M6GovernTest(unittest.TestCase):
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

        def mk(operator, pw, role, clearance, tenant, is_admin=0):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role, clearance=clearance,
                                       tenant_id=tenant, is_admin=is_admin))

        mk("赵管理", "pw-adm", "human", 4, "t1", is_admin=1)
        mk("王检察官", "pw-pro", "human", 4, "t1")
        mk("李侦查员", "pw-sol", "正兵", 1, "t1")
        mk("李检", "pw-li", "正兵", 1, "t2")
        self.auth_a = self._login("赵管理", "pw-adm")
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        self.auth_l = self._login("李检", "pw-li")
        r = self.client.post(f"{API}/cases", headers=self.auth_a,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def _login(self, operator, password):
        r = self.client.post(f"{API}/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _snap_file(self, filename) -> Path:
        return self.svc.snapshot_dir("c1", "default") / filename

    def _seed_built_v1(self):
        self.repo.set_version("c1", 1, "test")
        store = self.factory.for_case("c1", mode="write", version=1)
        try:
            conn = store.write_conn
            conn.execute("CREATE TABLE obj_person (person_id VARCHAR, "
                         "raw_name VARCHAR)")
            conn.execute("INSERT INTO obj_person VALUES ('p1','张三')")
            conn.execute("CREATE TABLE meta_ontology_state (pack VARCHAR, "
                         "build_id VARCHAR PRIMARY KEY, built_at VARCHAR, "
                         "schema_version INTEGER, ontology_version VARCHAR, "
                         "source_watermark VARCHAR, ontology_watermark VARCHAR, "
                         "input_hashes VARCHAR, params_hash VARCHAR, "
                         "is_current BOOLEAN)")
            conn.execute("INSERT INTO meta_ontology_state VALUES "
                         "('default','b1','2026-06-01T00:00:00',2,'v1',"
                         "NULL,NULL,NULL,NULL,TRUE)")
        finally:
            store.close()

    # ---- W-P-011 数据元 ----
    def test_011_data_elements(self):
        r = self.client.get(f"{API}/cases/c1/data-elements",
                            headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        original = r.json()["data"]
        self.assertIsInstance(original, dict)

        # 合法全量写（原样回写）→ 落盘
        r = self.client.put(f"{API}/cases/c1/data-elements",
                            headers=self.auth_a, json=original)
        self.assertEqual(r.status_code, 200, r.text)
        on_disk = json.loads(
            self._snap_file("data_elements.json").read_text("utf-8"))
        self.assertEqual(on_disk, original)

        # 非法内容 → 400 且不落盘
        bad = dict(original)
        bad["schema_version"] = 999
        r = self.client.put(f"{API}/cases/c1/data-elements",
                            headers=self.auth_a, json=bad)
        self.assertEqual(r.status_code, 400, r.text)
        on_disk2 = json.loads(
            self._snap_file("data_elements.json").read_text("utf-8"))
        self.assertEqual(on_disk2, original)

        # 低 clearance → 403
        r = self.client.put(f"{API}/cases/c1/data-elements",
                            headers=self.auth_s, json=original)
        self.assertEqual(r.status_code, 403, r.text)

        # 跨租户读 → 404
        r = self.client.get(f"{API}/cases/c1/data-elements",
                            headers=self.auth_l)
        self.assertEqual(r.status_code, 404, r.text)

    # ---- W-P-012 ETL 管道 ----
    def test_012_etl_pipeline_views_and_writeback(self):
        r = self.client.get(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        sources = r.json()["data"]["sources"]
        objs = {s["object"] for s in sources}
        self.assertIn("person", objs)
        person = next(s for s in sources if s["object"] == "person")
        self.assertEqual(person["dedup_on_conflict"], "keep_latest")
        self.assertEqual(person["dedup_key"], [])

        # 回写去重策略
        r = self.client.put(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_a,
                            json={"sources": [{
                                "object": "person",
                                "dedup_key": ["raw_name"],
                                "dedup_on_conflict": "keep_first",
                                "clean": ["strip"]}]})
        self.assertEqual(r.status_code, 200, r.text)
        person2 = next(s for s in r.json()["data"]["sources"]
                       if s["object"] == "person")
        self.assertEqual(person2["dedup_key"], ["raw_name"])
        self.assertEqual(person2["dedup_on_conflict"], "keep_first")

        # 落盘文件仍可被 loader 装载（GET 二次确认）
        r = self.client.get(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)

        # body 形态错误 → 400
        r = self.client.put(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_a, json={"sources": "x"})
        self.assertEqual(r.status_code, 400, r.text)

        # 低 clearance → 403
        r = self.client.put(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_s, json={"sources": []})
        self.assertEqual(r.status_code, 403, r.text)

    def test_012_etl_validate_conflicts(self):
        url = f"{API}/cases/c1/etl-pipeline/validate"
        # 1:1 冲突：同一源列映射两个属性
        r = self.client.post(url, headers=self.auth_a,
                             json={"target_table": "银行流水",
                                   "mapping": {"from_raw": "金额",
                                               "to_raw": "金额"}})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertFalse(data["valid"])
        kinds = {c["type"] for c in data["conflicts"]}
        self.assertIn("one_to_one", kinds)
        # 红线四：绝无 force/ignore/continue
        blob = json.dumps(data, ensure_ascii=False)
        for banned in ("force", "ignore", "continue"):
            self.assertNotIn(banned, blob)
        path_keys = {p["key"] for p in data["paths"]}
        self.assertEqual(path_keys,
                         {"A_split_source_sql", "B_degrade_column"})

        # 缺列冲突
        r = self.client.post(url, headers=self.auth_a,
                             json={"target_table": "银行流水",
                                   "mapping": {"from_raw": "不存在列"}})
        kinds = {c["type"] for c in r.json()["data"]["conflicts"]}
        self.assertIn("missing_column", kinds)

        # 未知属性
        r = self.client.post(url, headers=self.auth_a,
                             json={"target_table": "银行流水",
                                   "mapping": {"ghost_prop": "金额"}})
        kinds = {c["type"] for c in r.json()["data"]["conflicts"]}
        self.assertIn("unknown_prop", kinds)

        # 合法映射
        r = self.client.post(url, headers=self.auth_a,
                             json={"target_table": "银行流水",
                                   "mapping": {"from_raw": "主体"}})
        self.assertTrue(r.json()["data"]["valid"], r.text)

        # 未声明源表 → 400
        r = self.client.post(url, headers=self.auth_a,
                             json={"target_table": "火星表",
                                   "mapping": {"from_raw": "主体"}})
        self.assertEqual(r.status_code, 400, r.text)

    # ---- W-P-013 系统设置 ----
    def test_013_settings_queue(self):
        r = self.client.get(f"{API}/settings/queue", headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"],
                         {"max_workers": 2, "poll_interval_ms": 100})

        r = self.client.put(f"{API}/settings/queue", headers=self.auth_a,
                            json={"values": {"max_workers": 4},
                                  "reason": "任务高峰扩容"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"{API}/settings/queue", headers=self.auth_a)
        self.assertEqual(r.json()["data"]["max_workers"], 4)

        # 非 admin 读/写均 403
        self.assertEqual(self.client.get(f"{API}/settings/queue",
                                         headers=self.auth_s).status_code, 403)
        r = self.client.put(f"{API}/settings/queue", headers=self.auth_s,
                            json={"values": {"max_workers": 8}, "reason": "x"})
        self.assertEqual(r.status_code, 403, r.text)

        # reason 缺失 → 400
        r = self.client.put(f"{API}/settings/queue", headers=self.auth_a,
                            json={"values": {"max_workers": 4}})
        self.assertEqual(r.status_code, 400, r.text)

        # 白名单外键 → 400
        r = self.client.put(f"{API}/settings/queue", headers=self.auth_a,
                            json={"values": {"foo": 1}, "reason": "x"})
        self.assertEqual(r.status_code, 400, r.text)

        # 越界值 → 400
        r = self.client.put(f"{API}/settings/queue", headers=self.auth_a,
                            json={"values": {"max_workers": 99},
                                  "reason": "x"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_013_settings_resources_and_health(self):
        r = self.client.get(f"{API}/settings/resources", headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("storage_root", r.json()["data"])

        r = self.client.put(f"{API}/settings/resources", headers=self.auth_a,
                            json={"values": {"max_rows_default": 2000},
                                  "reason": "加大导出上限"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"{API}/settings/resources", headers=self.auth_a)
        self.assertEqual(r.json()["data"]["max_rows_default"], 2000)

        # storage_root 不可写（不在白名单）
        r = self.client.put(f"{API}/settings/resources", headers=self.auth_a,
                            json={"values": {"storage_root": "/etc"},
                                  "reason": "x"})
        self.assertEqual(r.status_code, 400, r.text)

        # health：登录即可读
        r = self.client.get(f"{API}/settings/health", headers=self.auth_s)
        self.assertEqual(r.status_code, 200, r.text)
        h = r.json()["data"]
        self.assertTrue(h["meta_ok"])
        self.assertIn("pending", h["queue"])
        self.assertEqual(h["versions"]["backend"], "M6")
        self.assertEqual(h["worker"]["max_workers"], 2)

    # ---- W-P-017 配置中心 ----
    def test_017_snapshots_features_thresholds(self):
        r = self.client.get(f"{API}/settings/snapshots", headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["items"]
        self.assertTrue(any(s["case_id"] == "c1" for s in items))
        # 非 admin → 403
        self.assertEqual(self.client.get(f"{API}/settings/snapshots",
                                         headers=self.auth_s).status_code, 403)

        # features 白名单
        r = self.client.put(f"{API}/settings/features", headers=self.auth_a,
                            json={"values": {"ui_density": "compact"},
                                  "reason": "密度偏好"})
        self.assertEqual(r.status_code, 200, r.text)
        # 红线键 llm_enabled → 400
        r = self.client.put(f"{API}/settings/features", headers=self.auth_a,
                            json={"values": {"llm_enabled": True},
                                  "reason": "开启模型"})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.client.put(f"{API}/settings/features", headers=self.auth_a,
                            json={"values": {"ui_density": "weird"},
                                  "reason": "x"})
        self.assertEqual(r.status_code, 400, r.text)

        # 平台阈值
        r = self.client.get(f"{API}/settings/policies-thresholds",
                            headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("note", r.json()["data"])
        r = self.client.put(f"{API}/settings/policies-thresholds",
                            headers=self.auth_a,
                            json={"values": {"stale_days": 30},
                                  "reason": "陈旧期调整"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["policies_thresholds"]["stale_days"],
                         30)
        r = self.client.put(f"{API}/settings/policies-thresholds",
                            headers=self.auth_a,
                            json={"values": {"stale_days": 0}, "reason": "x"})
        self.assertEqual(r.status_code, 400, r.text)
        # 非 admin
        r = self.client.get(f"{API}/settings/policies-thresholds",
                            headers=self.auth_s)
        self.assertEqual(r.status_code, 403, r.text)

    # ---- W-P-014 任务取消 ----
    def _enqueue(self, headers, case="c1", idem=None):
        r = self.client.post(f"{API}/cases/{case}/tasks", headers=headers,
                             json={"task_type": "BUILD",
                                   "idem_key": idem or ""})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]["id"]

    def test_014_cancel_pending_by_creator(self):
        tid = self._enqueue(self.auth_h)
        r = self.client.post(f"{API}/tasks/{tid}/cancel", headers=self.auth_h,
                             json={"reason": "误提交"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["status"], "CANCELLED")
        # 重复取消（已终态）→ 409
        r = self.client.post(f"{API}/tasks/{tid}/cancel", headers=self.auth_h,
                             json={"reason": "再取消"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_014_cancel_running_conflict(self):
        tid = self._enqueue(self.auth_a)
        self.assertTrue(self.repo.claim_task(tid))
        r = self.client.post(f"{API}/tasks/{tid}/cancel", headers=self.auth_a,
                             json={"reason": "x"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_014_cancel_permissions(self):
        # 非创建人、非 admin → 403
        tid = self._enqueue(self.auth_s)
        r = self.client.post(f"{API}/tasks/{tid}/cancel", headers=self.auth_h,
                             json={"reason": "x"})
        self.assertEqual(r.status_code, 403, r.text)
        # admin 可取消他人任务
        r = self.client.post(f"{API}/tasks/{tid}/cancel", headers=self.auth_a,
                             json={"reason": "管理员回收"})
        self.assertEqual(r.status_code, 200, r.text)
        # 跨租户 → 404
        tid2 = self._enqueue(self.auth_s)
        r = self.client.post(f"{API}/tasks/{tid2}/cancel",
                             headers=self.auth_l, json={"reason": "x"})
        self.assertEqual(r.status_code, 404, r.text)

    # ---- 失败/已取消任务重试（POST /tasks/{id}/retry）----
    def test_014b_retry_failed_build_creates_new_task(self):
        tid = self._enqueue(self.auth_h)
        # PENDING（非终态）不可重试 → 409
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_h)
        self.assertEqual(r.status_code, 409, r.text)
        self.repo.fail_task(tid, error_code="BUILD_FAIL", error_message="boom")

        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertNotEqual(d["id"], tid)            # 新任务 id
        self.assertEqual(d["task_type"], "BUILD")
        self.assertEqual(d["status"], "PENDING")
        self.assertEqual(d["idem_key"], "")          # 不沿用旧幂等键
        self.assertEqual(d["created_by"], "王检察官")
        # 旧任务保留 FAILED 留痕；新任务可被 Worker 认领
        self.assertEqual(self.repo.get_task(tid).status, "FAILED")
        self.assertTrue(self.repo.claim_task(d["id"]))
        # 同一条旧任务可再次重试（每次都是独立新任务）
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)

    def test_014b_retry_permissions_and_tenant(self):
        tid = self._enqueue(self.auth_s)
        self.repo.fail_task(tid, error_code="X", error_message="boom")
        # 非创建人、非 admin → 403
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_h)
        self.assertEqual(r.status_code, 403, r.text)
        # 跨租户 → 404（先于权限判断）
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_l)
        self.assertEqual(r.status_code, 404, r.text)
        # admin 可重试他人任务，新任务发起人记 admin
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["created_by"], "赵管理")

    def test_014b_retry_cancelled_build(self):
        tid = self._enqueue(self.auth_h)
        r = self.client.post(f"{API}/tasks/{tid}/cancel", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["status"], "PENDING")
        self.assertEqual(d["task_type"], "BUILD")
        self.assertEqual(self.repo.get_task(tid).status, "CANCELLED")

    def test_014b_retry_import_missing_upload(self):
        # IMPORT 重试：params 指向不存在的上传件登记 → 404
        r = self.client.post(f"{API}/cases/c1/tasks", headers=self.auth_h,
                             json={"task_type": "IMPORT",
                                   "params": {"upload_id": "up_nope",
                                              "target_table": "通话记录",
                                              "column_map": {}}})
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["data"]["id"]
        self.repo.fail_task(tid, error_code="X", error_message="boom")
        r = self.client.post(f"{API}/tasks/{tid}/retry", headers=self.auth_h)
        self.assertEqual(r.status_code, 404, r.text)

    # ---- W-P-015 门户汇总 + 归档 ----
    def test_015_summary_unbuilt(self):
        r = self.client.get(f"{API}/cases/c1/summary", headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["case"]["id"], "c1")
        self.assertEqual(d["todos"],
                         {"clues_pending": 0, "review_pending": 0,
                          "anomalies_pending": 0})
        self.assertTrue(d["health"]["chain_ok"])
        self.assertIsInstance(d["recent_tasks"], list)

    def test_015_summary_with_diagnostics(self):
        self._seed_built_v1()
        wstore = self.factory.for_case("c1", mode="write", version=1)
        try:
            RunHealth(wstore.write_conn).record(
                "clean_drop_rate", "warning", source="build_ontology",
                reason="测试告警")
        finally:
            wstore.close()
        r = self.client.get(f"{API}/cases/c1/summary", headers=self.auth_a)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertGreaterEqual(d["todos"]["anomalies_pending"], 1)
        self.assertEqual(d["health"]["diagnostics_warn"],
                         d["todos"]["anomalies_pending"])

    def test_015_archive(self):
        # 低 clearance → 403
        r = self.client.post(f"{API}/cases/c1/archive", headers=self.auth_s,
                             json={"reason": "结案归档"})
        self.assertEqual(r.status_code, 403, r.text)

        # 待建案 → 已封存 非法迁移 → 409
        r = self.client.post(f"{API}/cases/c1/archive", headers=self.auth_a,
                             json={"reason": "结案归档"})
        self.assertEqual(r.status_code, 409, r.text)

        # 转入侦查中后归档 → 202 并入队 ARCHIVE
        self.repo.transition_case("c1", CASE_ACTIVE, by="赵管理")
        r = self.client.post(f"{API}/cases/c1/archive", headers=self.auth_a,
                             json={"reason": "结案归档"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["data"]["task_type"], "ARCHIVE")
        r = self.client.get(f"{API}/cases/c1", headers=self.auth_a)
        self.assertEqual(r.json()["data"]["status"], "已封存")

        # 重复归档 → 409（终态）
        r = self.client.post(f"{API}/cases/c1/archive", headers=self.auth_a,
                             json={"reason": "再次归档"})
        self.assertEqual(r.status_code, 409, r.text)


if __name__ == "__main__":
    unittest.main()
