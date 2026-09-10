"""
tests/test_diagnose_worker.py
手动运行诊断（DIAGNOSE 任务）：
  - 不随 BUILD 自动留痕；用户手动发起才写 run_diagnostic；
  - 对当前生效版本 write 补诊断，不产新版本、不动版本指针；
  - BUILD 五类原料从 artifacts/build_stats_vN.json 补落（缺失=旧版本，跳过）；
  - 每次发起落 diagnostic_run 印记（零问题也有留痕）；
  - 重复发起 = 新 run_id 追加。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.run_health import RunHealth

from server.app.build_stats_artifact import (
    build_stats_path,
    load_build_stats,
    save_build_stats,
)
from server.app.meta.models import CASE_ACTIVE, CASE_DRAFT, CaseRecord
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.store import StoreFactory
from server.app.worker.diagnose import handle_diagnose
from server.app.worker.tasks import (
    TASK_BUILD,
    TASK_DIAGNOSE,
    TaskExecError,
    enqueue_task,
    handle_build,
)


def _builder(conn, *, pack: str, base_dir: Path, progress) -> dict:
    """极简构建器：建一张表即成功（不依赖完整语义层）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    progress(90.0, "compile", "完成", "stub")
    return {"objects": {}, "links": {}, "skipped": []}


def _builder_with_stats(conn, *, pack: str, base_dir: Path, progress) -> dict:
    """带 BUILD 诊断原料的构建器（模拟 default_builder 的 build_stats 键）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    progress(90.0, "compile", "完成", "stub")
    return {"objects": {}, "links": {}, "skipped": [],
            "build_stats": {"dirty": ["obj_person.raw_name<-姓名: 1 列脏值"]}}


class DiagnoseWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="案"))
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        self.repo.create_case(CaseRecord(id="c2", tenant_id="t1", name="未构建案"))
        self.repo.transition_case("c2", CASE_ACTIVE, by="u")
        # 快照指向真实 ontology/（网关构造即 load_pack）
        self.snap = lambda cid: ROOT / "ontology"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, case_id="c1", builder=_builder):
        t = enqueue_task(self.repo, case_id=case_id, task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=self.snap, builder=builder)
        return t

    def _diagnose(self, case_id="c1", created_by="王检察官"):
        t = enqueue_task(self.repo, case_id=case_id, task_type=TASK_DIAGNOSE,
                         created_by=created_by)
        return handle_diagnose(t, repo=self.repo, factory=self.factory,
                               snapshot_base_for=self.snap)

    def _diag_rows(self, case_id="c1", version=1):
        """全表诊断行（跨 run；表未创建时返回 []）。"""
        store = self.factory.for_case(case_id, mode="read", version=version)
        try:
            cur = store.read_conn.execute(
                "SELECT run_id, kind, severity, source FROM run_diagnostic "
                "ORDER BY run_id, seq")
            return [dict(zip(["run_id", "kind", "severity", "source"], r))
                    for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            store.close()

    # ---- 门槛 ----
    def test_no_version_rejected(self):
        t = enqueue_task(self.repo, case_id="c2", task_type=TASK_DIAGNOSE)
        with self.assertRaises(TaskExecError) as cm:
            handle_diagnose(t, repo=self.repo, factory=self.factory,
                            snapshot_base_for=self.snap)
        self.assertEqual(cm.exception.code, "NO_VERSION")
        self.assertEqual(self.repo.current_version("c2"), 0)

    # ---- 核心：手动发起才有留痕 ----
    def test_diagnose_writes_run_diagnostic_without_new_version(self):
        self._build()
        # BUILD 后无诊断（stub builder 未产 build_stats，且 BUILD 不写 run_diagnostic）
        self.assertFalse(build_stats_path(self.factory.case_dir("c1"), 1).exists())

        res = self._diagnose()
        self.assertEqual(res["version"], 1)
        self.assertFalse(self.factory.version_path("c1", 2).exists(),
                         "诊断写当前版本，不产 v2")
        self.assertEqual(self.repo.current_version("c1"), 1)
        self.assertFalse(res["build_stats_present"])

        rows = self._diag_rows()
        kinds = [r["kind"] for r in rows]
        self.assertIn("diagnostic_run", kinds, "零问题也必须有运行印记")
        marker = [r for r in rows if r["kind"] == "diagnostic_run"][0]
        self.assertEqual(marker["source"], "manual_diagnose")
        self.assertEqual(marker["severity"], "info")
        self.assertGreaterEqual(res["health"]["诊断总数"], 1)

    def test_repeat_diagnose_appends_new_run(self):
        self._build()
        r1 = self._diagnose()
        r2 = self._diagnose()
        self.assertNotEqual(r1["run_id"], r2["run_id"])
        # 两组 run 各有一条印记
        rows = self._diag_rows()
        markers = [r for r in rows if r["kind"] == "diagnostic_run"]
        self.assertEqual(len(markers), 2)
        # readonly 默认取最新 run，看板展示最新一次
        store = self.factory.for_case("c1", mode="read", version=1)
        try:
            latest = RunHealth.readonly(store.read_conn)
            self.assertEqual(latest.run_id, r2["run_id"])
        finally:
            store.close()

    # ---- BUILD 五类原料补落 ----
    def test_build_stats_replayed_from_artifact(self):
        self._build()
        # 模拟 BUILD 已留存的原料（真实链路由 default_builder + handle_build 落）
        p = save_build_stats(self.factory.case_dir("c1"), 1,
                             {"dirty": ["obj_person.raw_name<-姓名: 脏值"],
                              "degraded": [], "quarantine": [],
                              "clean_stats": [], "dedup_conflicts": []})
        self.assertIsNotNone(p)
        self.assertEqual(load_build_stats(self.factory.case_dir("c1"), 1)["dirty"],
                         ["obj_person.raw_name<-姓名: 脏值"])

        res = self._diagnose()
        self.assertTrue(res["build_stats_present"])
        self.assertEqual(res["build_counts"]["dirty"], 1)
        kinds = [r["kind"] for r in self._diag_rows()]
        self.assertIn("source_value_cast_failed", kinds)
        self.assertIn("diagnostic_run", kinds)

    def test_handle_build_persists_build_stats_artifact(self):
        """端到端：builder 返回 build_stats → BUILD 成功即落原料（仍不写诊断表）。"""
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=self.snap,
                     builder=_builder_with_stats)
        p = build_stats_path(self.factory.case_dir("c1"), 1)
        self.assertTrue(p.exists())
        self.assertEqual(len(load_build_stats(self.factory.case_dir("c1"), 1)["dirty"]), 1)
        # 原料落盘 ≠ 诊断留痕：BUILD 后 run_diagnostic 仍为空（表都不存在）
        self.assertEqual(self._diag_rows(), [])

    def test_build_activates_draft_case(self):
        """构建成功后「待建案」自动迁移为「侦查中」。"""
        # c2 在 setUp 中已被 activate，新建一个 draft 案件
        self.repo.create_case(CaseRecord(id="c3", tenant_id="t1", name="待建案"))
        self.assertEqual(self.repo.get_case("c3").status, CASE_DRAFT)
        t = enqueue_task(self.repo, case_id="c3", task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=self.snap, builder=_builder)
        self.assertEqual(self.repo.get_case("c3").status, CASE_ACTIVE)

    def test_build_does_not_reactivate_active_case(self):
        """已处于「侦查中」的案件不重复迁移。"""
        # c1 在 setUp 中已是 ACTIVE
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=self.snap, builder=_builder)
        self.assertEqual(self.repo.get_case("c1").status, CASE_ACTIVE)


if __name__ == "__main__":
    unittest.main()
