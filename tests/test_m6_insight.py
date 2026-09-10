"""
tests/test_m6_insight.py
M6 阶段 C/D（W-P-005~010）：
  W-P-005 庙算派生视图（heatmap/coverage/candidates/restricted 内间过滤）；
  W-P-006 数据画像（快照感知 profiler，未 BUILD 空态）；
  W-P-007 数据元智能推荐（生成/幂等/采纳驳回只记 state 不改 bindings）；
  W-P-008 数据质量检查（四扫描汇总落 state；heuristic 封顶 suggest）；
  W-P-009 隔离区（build_quarantine + 诊断聚合，零隔离 empty_message）；
  W-P-010 清洗留痕（按 object.property 聚合）。
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

from core.registry import LineageClue
from core.run_health import RunHealth

from server.app.cases import CaseService
from server.app.clues_artifact import save_case_clues
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool

CSV_BYTES = (
    "公司名,法人名,性别\n"
    "阿尔法公司,张三,男\n"
    "贝塔公司,李四,女\n"
).encode("utf-8")


def _clue(cid: str, title: str, jians=None, level="线索",
          score: float = 0.5) -> LineageClue:
    return LineageClue(
        clue_id=cid, skill_id="xu_shi", title=title,
        jian_types=jians or ["生间"],
        detail={"级别": level, "priority_score": score,
                "主体": title.replace("线索", "")},
        source_rows=[{"file": "b.parquet", "row_id": 1}])


class M6InsightTest(unittest.TestCase):
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
                                   salt=salt3, role="正兵", clearance=1,
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

    # ---- helpers ----
    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _make_case(self, case_id: str = "c1") -> None:
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": case_id, "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def _seed_built_case(self, case_id: str = "c1", version: int = 1):
        """建 v1 语义层（obj_person/obj_account + 版本元表）+ 版本指针。"""
        self.repo.set_version(case_id, version, "test")
        store = self.factory.for_case(case_id, mode="write", version=version)
        try:
            conn = store.write_conn
            conn.execute("CREATE TABLE obj_person (person_id VARCHAR, "
                         "raw_name VARCHAR)")
            conn.execute("INSERT INTO obj_person VALUES "
                         "('p1','张三'),('p2','李四')")
            conn.execute("CREATE TABLE obj_account (account_id VARCHAR, "
                         "raw_name VARCHAR)")
            conn.execute("INSERT INTO obj_account VALUES "
                         "('a1','62280001'),('a2','62280002')")
            # 真实 BUILD 必写版本元表（read-only gateway 跳过 DDL 的前提）
            conn.execute(
                "CREATE TABLE meta_ontology_state (pack VARCHAR, "
                "build_id VARCHAR PRIMARY KEY, built_at VARCHAR, "
                "schema_version INTEGER, ontology_version VARCHAR, "
                "source_watermark VARCHAR, ontology_watermark VARCHAR, "
                "input_hashes VARCHAR, params_hash VARCHAR, is_current BOOLEAN)")
            conn.execute(
                "INSERT INTO meta_ontology_state VALUES "
                "('default','b1','2026-06-01T00:00:00',2,'v1',"
                "NULL,NULL,NULL,NULL,TRUE)")
        finally:
            store.close()

    def _seed_clues(self, case_id: str = "c1", version: int = 1,
                    clues: list[LineageClue] | None = None) -> None:
        save_case_clues(self.factory.case_dir(case_id), version,
                        clues or [_clue("clue-1", "大额取现线索")])

    def _seed_diagnostics(self, case_id: str = "c1", version: int = 1):
        store = self.factory.for_case(case_id, mode="write", version=version)
        try:
            rh = RunHealth(store.write_conn)
            rh.record("coverage_gap", "info", source="miaosuan:dimension",
                      reason="声明覆盖缺口", missing=["死间", "生间"])
            rh.record("coverage_gap", "info",
                      source="miaosuan:dimension:empirical",
                      reason="实证覆盖缺口", missing=["反间"])
            rh.record("clean_drop_rate", "warning", source="build_ontology",
                      reason="person.raw_name 空值剔除率 50%",
                      object="person", prop="raw_name", dropped_rows=2,
                      rows_before=4, rows_after=2, rate=0.5,
                      rules=["null_policy:reject"],
                      sample_masked=["张**", "李**"])
            rh.record("dedup_key_conflict", "warning",
                      source="build_ontology", reason="account 去重冲突",
                      object="account", key_columns=["account_id"],
                      policy="converged", conflict_groups=1,
                      duplicate_rows=3)
            conn = store.write_conn
            conn.execute(
                "CREATE TABLE IF NOT EXISTS build_quarantine ("
                "object VARCHAR, property VARCHAR, src_column VARCHAR, "
                "reason VARCHAR, sample_masked VARCHAR, name_value VARCHAR, "
                "source_table VARCHAR, quarantined_at VARCHAR)")
            conn.execute(
                "INSERT INTO build_quarantine VALUES "
                "('person','age','年龄','integer_cast_failed','3*岁',NULL,"
                "'工商信息','2026-06-01 10:00:00')")
        finally:
            store.close()

    def _drain(self) -> None:
        self.pool.run_until_drained(max_idle_rounds=5)

    def _upload(self, case_id: str = "c1",
                content: bytes = CSV_BYTES) -> dict:
        r = self.client.post(
            f"/api/v1/cases/{case_id}/sources/upload",
            headers=self.auth_h, files={"file": ("companies.csv", content)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    # ==================================================================
    # W-P-005 庙算派生视图
    # ==================================================================
    def test_100_hypotheses_unbuilt_available_false(self):
        self._make_case()
        r = self.client.get("/api/v1/cases/c1/hypotheses",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertFalse(d["available"])
        self.assertTrue(d["derived"])
        self.assertEqual(d["heatmap"]["jians"], ["因间", "内间", "反间", "死间", "生间"])
        self.assertEqual(d["heatmap"]["levels"], ["观察", "线索", "可立案依据候选"])
        self.assertEqual(d["heatmap"]["counts"],
                         [[0] * 5 for _ in range(3)])
        self.assertEqual(d["candidates"], [])
        self.assertEqual(d["restricted"], [])
        self.assertIn("declared", d["coverage"])
        self.assertIn("empirical", d["coverage"])

    def test_101_hypotheses_heatmap_candidates_restricted(self):
        self._make_case()
        self._seed_built_case()
        self._seed_clues(clues=[
            _clue("c1", "生间高优", jians=["生间"], level="确认", score=0.95),
            _clue("c2", "因间观察", jians=["因间"], level="观察", score=0.3),
            _clue("c3", "内间密线", jians=["内间"], level="线索", score=0.8),
        ])
        self._seed_diagnostics()
        # 正兵：内间进 restricted，heatmap 不含内间
        d = self.client.get("/api/v1/cases/c1/hypotheses",
                            headers=self.auth_s).json()["data"]
        self.assertTrue(d["available"])
        counts = d["heatmap"]["counts"]
        self.assertEqual(counts[2][4], 1)  # 确认×生
        self.assertEqual(counts[0][0], 1)  # 观察×因
        self.assertEqual(sum(counts[1]), 0)  # 线索行无内间
        rest = {r["clue_id"]: r for r in d["restricted"]}
        self.assertIn("c3", rest)
        self.assertEqual(rest["c3"]["reason"], "内间线索·权限不足")
        # 候补池：未处置 + priority 降序，且不含升格字段
        cands = d["candidates"]
        self.assertEqual([c["clue_id"] for c in cands][:2], ["c1", "c2"])
        for c in cands:
            self.assertNotIn("upgrade", c)
            self.assertNotIn("cross_level", c)
        # 覆盖诊断双路透传
        decl = d["coverage"]["declared"][0]
        self.assertEqual(decl["missing"], ["死间", "生间"])
        self.assertEqual(decl["total"], 5)
        self.assertEqual(d["coverage"]["empirical"][0]["missing"], ["反间"])
        # human：内间可见、restricted 空
        d2 = self.client.get("/api/v1/cases/c1/hypotheses",
                             headers=self.auth_h).json()["data"]
        self.assertEqual(d2["restricted"], [])
        self.assertEqual(sum(d2["heatmap"]["counts"][1]), 1)  # 线索×内

    def test_102_hypotheses_candidates_exclude_disposed(self):
        self._make_case()
        self._seed_built_case()
        self._seed_clues(clues=[
            _clue("c1", "已查证", score=0.9),
            _clue("c2", "待查", score=0.1),
        ])
        r = self.client.post(
            "/api/v1/cases/c1/clues/c1/actions",
            headers=self.auth_h, json={"action": "verify"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        d = self.client.get("/api/v1/cases/c1/hypotheses",
                            headers=self.auth_h).json()["data"]
        self.assertEqual([c["clue_id"] for c in d["candidates"]], ["c2"])

    def test_103_hypotheses_cross_tenant_404(self):
        self._make_case()
        r = self.client.get("/api/v1/cases/c1/hypotheses",
                            headers=self.auth_l)
        self.assertEqual(r.status_code, 404)

    # ==================================================================
    # W-P-006 数据画像
    # ==================================================================
    def test_110_profiles_unbuilt_available_false(self):
        self._make_case()
        r = self.client.get("/api/v1/cases/c1/profiles",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertFalse(d["available"])

    def test_111_profiles_built_report(self):
        self._make_case()
        self._seed_built_case()
        r = self.client.get("/api/v1/cases/c1/profiles",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["available"])
        self.assertEqual(d["pack"], "default")
        self.assertIn("张三", d["focus"])
        for key in ("l1_l2", "l3", "l4", "l5", "compliance", "params"):
            self.assertIn(key, d)
        # focus 显式传入
        r = self.client.get("/api/v1/cases/c1/profiles?focus=李四",
                            headers=self.auth_h)
        self.assertEqual(r.json()["data"]["focus"], ["李四"])

    # ==================================================================
    # W-P-009 隔离区
    # ==================================================================
    def test_120_quarantine_empty_message(self):
        self._make_case()
        self._seed_built_case()
        r = self.client.get("/api/v1/cases/c1/quarantine",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["items"], [])
        self.assertEqual(d["total"], 0)
        self.assertTrue(d["empty_message"])
        self.assertEqual(set(d["stats"]),
                         {"cast_error", "null_value", "dedup", "other"})

    def test_121_quarantine_aggregated_sources_and_filter(self):
        self._make_case()
        self._seed_built_case()
        self._seed_diagnostics()
        d = self.client.get("/api/v1/cases/c1/quarantine",
                            headers=self.auth_h).json()["data"]
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["stats"]["cast_error"], 1)
        self.assertEqual(d["stats"]["null_value"], 1)
        self.assertEqual(d["stats"]["dedup"], 1)
        # cast_error 行级样本脱敏
        cast = next(i for i in d["items"] if i["reason"] == "cast_error")
        self.assertEqual(cast["object"], "person")
        self.assertEqual(cast["sample_masked"], ["3*岁"])
        self.assertEqual(cast["source_table"], "工商信息")
        # null_value 聚合样本
        nullv = next(i for i in d["items"] if i["reason"] == "null_value")
        self.assertEqual(nullv["object"], "person")
        self.assertTrue(nullv["sample_masked"])
        # reason 过滤
        d2 = self.client.get("/api/v1/cases/c1/quarantine?reason=dedup",
                             headers=self.auth_h).json()["data"]
        self.assertEqual(d2["total"], 1)
        self.assertEqual(d2["items"][0]["reason"], "dedup")
        # 非法 reason → 400
        r = self.client.get("/api/v1/cases/c1/quarantine?reason=bogus",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 400)

    # ==================================================================
    # W-P-010 清洗留痕
    # ==================================================================
    def test_130_clean_trace_aggregation(self):
        self._make_case()
        self._seed_built_case()
        self._seed_diagnostics()
        d = self.client.get("/api/v1/cases/c1/clean-trace",
                            headers=self.auth_h).json()["data"]
        self.assertEqual(d["total"], 2)
        person = next(i for i in d["items"] if i["object"] == "person")
        self.assertEqual(person["property"], "raw_name")
        self.assertIn("null_policy:reject", person["rules"])
        self.assertEqual(person["dropped_rows"], 2)
        self.assertEqual(person["rows_before"], 4)
        self.assertEqual(person["rows_after"], 2)
        self.assertEqual(person["rate"], 0.5)
        self.assertTrue(person["samples_masked"])
        acct = next(i for i in d["items"] if i["object"] == "account")
        self.assertEqual(acct["dropped_rows"], 3)
        self.assertTrue(any(r.startswith("dedup:") for r in acct["rules"]))
        # object 过滤
        d2 = self.client.get("/api/v1/cases/c1/clean-trace?object=person",
                             headers=self.auth_h).json()["data"]
        self.assertEqual(d2["total"], 1)

    # ==================================================================
    # W-P-008 数据质量检查
    # ==================================================================
    def test_140_quality_check_flow_and_severity_cap(self):
        self._make_case()
        # 未 BUILD → 400
        r = self.client.post("/api/v1/cases/c1/quality-checks",
                             headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 400)
        # 未跑过 → available false
        d = self.client.get("/api/v1/cases/c1/quality-checks/latest",
                            headers=self.auth_h).json()["data"]
        self.assertFalse(d["available"])

        self._seed_built_case()
        r = self.client.post("/api/v1/cases/c1/quality-checks",
                             headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 202, r.text)
        first_id = r.json()["data"]["id"]
        # 进行中重复 POST 幂等回跳同一任务
        r2 = self.client.post("/api/v1/cases/c1/quality-checks",
                              headers=self.auth_h, json={})
        self.assertEqual(r2.json()["data"]["id"], first_id)
        self._drain()
        task = next(t for t in self.repo.list_tasks(case_id="c1")
                    if t.task_type == "QUALITY_CHECK")
        self.assertEqual(task.status, TASK_SUCCEEDED,
                         f"{task.error_code} {task.error_message}")
        d = self.client.get("/api/v1/cases/c1/quality-checks/latest",
                            headers=self.auth_h).json()["data"]
        self.assertTrue(d["available"])
        self.assertEqual(d["data_version"], 1)
        self.assertTrue(d["check_id"])
        s = d["summary"]
        self.assertEqual(s["total"],
                         s["passed"] + s["warnings"] + s["violations"])
        # 红线六：heuristic（sensitive/unit）封顶 suggest，永不 block
        for c in d["checks"]:
            if c["category"] in ("sensitive", "unit"):
                self.assertEqual(c["severity"], "suggest")
                self.assertEqual(c["mode"], "heuristic")
            if c["category"] in ("compliance", "freshness"):
                self.assertEqual(c["mode"], "deterministic")
                self.assertIn(c["severity"], ("block", "warn", "ok"))

    def test_140b_quality_mapping_severity_rules(self):
        """组装映射单测：合规 block/ok、新鲜度 warn、heuristic 封顶 suggest。"""
        from server.app.worker.quality import (
            _compliance_checks,
            _freshness_checks,
            _sensitive_checks,
            _unit_checks,
        )
        comp = _compliance_checks({"by_property": {
            "person.id_card": {"element": "DE_IDCARD", "checked": 10,
                               "violations": 2, "rate": 0.2,
                               "codes": {"id_format": 2}},
            "person.gender": {"element": "DE_GENDER", "checked": 10,
                              "violations": 0, "rate": 0.0, "codes": {}},
        }})
        self.assertEqual({c["severity"] for c in comp}, {"block", "ok"})
        self.assertTrue(all(c["mode"] == "deterministic" for c in comp))
        fresh = _freshness_checks({"stale_days": 180, "objects": [
            {"object": "transaction", "property": "date", "latest": "2020-01-01",
             "age_days": 2000},
            {"object": "call", "property": "date", "latest": "2026-06-01",
             "age_days": 5},
        ]})
        self.assertEqual({c["severity"] for c in fresh}, {"warn", "ok"})
        sens = _sensitive_checks({"details": [
            {"object": "person", "property": "id_card",
             "evidence": [{"pattern": "idcard", "sample_masked": "110***********1234"}],
             "suggestion": "建议声明遮蔽"}]})
        self.assertEqual([c["severity"] for c in sens], ["suggest"])
        unit = _unit_checks({"mismatches": [
            {"element": "DE_CURRENCY", "ratio": 1000.0, "units": ["元"],
             "high": "a", "low": "b"}], "missing_unit": [
            {"object": "transaction", "property": "amount",
             "element": "DE_CURRENCY"}]})
        self.assertTrue(unit)
        self.assertTrue(all(c["severity"] == "suggest" for c in unit))
        self.assertTrue(all(c["samples_masked"] is not None for c in unit))

    def test_141_quality_check_cross_tenant_404(self):
        self._make_case()
        self._seed_built_case()
        r = self.client.post("/api/v1/cases/c1/quality-checks",
                             headers=self.auth_l, json={})
        self.assertEqual(r.status_code, 404)

    # ==================================================================
    # W-P-007 数据元智能推荐
    # ==================================================================
    def test_150_de_reco_generate_list_decide(self):
        self._make_case()
        up = self._upload()
        # 生成
        r = self.client.post("/api/v1/cases/c1/de-recommendations",
                             headers=self.auth_h,
                             json={"upload_id": up["upload_id"]})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        d = self.client.get("/api/v1/cases/c1/de-recommendations",
                            headers=self.auth_h).json()["data"]
        self.assertEqual(d["total"], 1)
        rec = d["items"][0]
        self.assertEqual(rec["status"], "待核实")
        self.assertEqual(rec["upload_id"], up["upload_id"])
        self.assertEqual(rec["filename"], "companies.csv")
        cols = {x["col"]: x for x in rec["recommendations"]}
        self.assertIn("性别", cols)
        self.assertEqual(cols["性别"]["element_id"], "DE_GENDER")
        rid = rec["rid"]
        # 重复生成 → 幂等回跳
        r = self.client.post("/api/v1/cases/c1/de-recommendations",
                             headers=self.auth_h,
                             json={"upload_id": up["upload_id"]})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertTrue(r.json()["data"]["reused"])
        # 采纳
        r = self.client.post(
            f"/api/v1/cases/c1/de-recommendations/{rid}/decide",
            headers=self.auth_h,
            json={"decision": "adopt", "note": "人工核实无误"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        d = self.client.get("/api/v1/cases/c1/de-recommendations",
                            headers=self.auth_h).json()["data"]
        rec = d["items"][0]
        self.assertEqual(rec["status"], "采纳")
        self.assertEqual(rec["decided_by"], "王检察官")
        self.assertEqual(rec["note"], "人工核实无误")
        # 推荐永不自动生效：不产 BUILD/RESCAN、版本不变
        self.assertEqual(self.repo.current_version("c1"), 0)
        self.assertFalse(any(t.task_type in ("BUILD", "RESCAN")
                             for t in self.repo.list_tasks(case_id="c1")))

    def test_151_de_reco_errors(self):
        self._make_case()
        up = self._upload()
        r = self.client.post("/api/v1/cases/c1/de-recommendations",
                             headers=self.auth_h,
                             json={"upload_id": "up_nope"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post(
            "/api/v1/cases/c1/de-recommendations/der_nope/decide",
            headers=self.auth_h, json={"decision": "adopt"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post(
            "/api/v1/cases/c1/de-recommendations",
            headers=self.auth_h, json={"upload_id": ""})
        self.assertEqual(r.status_code, 400)
        # 非法 decision
        r = self.client.post("/api/v1/cases/c1/de-recommendations",
                             headers=self.auth_h,
                             json={"upload_id": up["upload_id"]})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        rec = self.client.get("/api/v1/cases/c1/de-recommendations",
                              headers=self.auth_h).json()["data"]["items"][0]
        r = self.client.post(
            f"/api/v1/cases/c1/de-recommendations/{rec['rid']}/decide",
            headers=self.auth_h, json={"decision": "maybe"})
        self.assertEqual(r.status_code, 400)
        # 跨租户
        r = self.client.get("/api/v1/cases/c1/de-recommendations",
                            headers=self.auth_l)
        self.assertEqual(r.status_code, 404)

    def test_152_de_reco_inflight_409(self):
        self._make_case()
        up = self._upload()
        r = self.client.post("/api/v1/cases/c1/de-recommendations",
                             headers=self.auth_h,
                             json={"upload_id": up["upload_id"]})
        self.assertEqual(r.status_code, 202, r.text)
        # 不 drain：同 upload 再 POST → 409 回跳既有任务
        r = self.client.post("/api/v1/cases/c1/de-recommendations",
                             headers=self.auth_h,
                             json={"upload_id": up["upload_id"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "CONFLICT")


if __name__ == "__main__":
    unittest.main()
