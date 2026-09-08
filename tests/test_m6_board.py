"""
tests/test_m6_board.py
M6 阶段 B（W-P-001~004）：
  W-P-001 五泳道处置看板（五态/状态真值/stay_days/overdue/内间秩级过滤）；
  W-P-002 上传件列分析（列画像/声明表/映射建议/数据元 hints，纯只读）；
  W-P-003 向导映射草稿保存 + 导入回落 mapping_json + 状态红线；
  W-P-004 关系图谱（节点边同域/采样截断/未 BUILD 空图降级）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from fastapi.testclient import TestClient

from core.registry import ClueStatus, LineageClue

from server.app.cases import CaseService
from server.app.clues_artifact import save_case_clues
from server.app.deps import WebContext
from server.app.disposal_board import assemble_board
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


class M6BoardTest(unittest.TestCase):
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

    def _seed_clues(self, case_id: str = "c1", version: int = 1,
                    clues: list[LineageClue] | None = None) -> None:
        self.repo.set_version(case_id, version, "test")
        save_case_clues(self.factory.case_dir(case_id), version,
                        clues or [_clue("clue-1", "大额取现线索")])

    def _board(self, case_id: str = "c1", headers=None):
        return self.client.get(
            f"/api/v1/cases/{case_id}/disposal/board",
            headers=headers or self.auth_h)

    def _drain(self) -> None:
        self.pool.run_until_drained(max_idle_rounds=5)

    def _upload(self, case_id: str = "c1", filename: str = "companies.csv",
                content: bytes = CSV_BYTES) -> dict:
        r = self.client.post(
            f"/api/v1/cases/{case_id}/sources/upload",
            headers=self.auth_h, files={"file": (filename, content)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def _seed_graph(self, case_id: str = "c1", version: int = 1) -> None:
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
            conn.execute("CREATE TABLE lnk_transfers (from_account_id VARCHAR, "
                         "to_account_id VARCHAR)")
            conn.execute("INSERT INTO lnk_transfers VALUES ('a1','a2')")
            conn.execute("CREATE TABLE lnk_calls_to (from_person VARCHAR, "
                         "to_person VARCHAR)")
            conn.execute("INSERT INTO lnk_calls_to VALUES ('p1','p2')")
        finally:
            store.close()

    # ==================================================================
    # W-P-001 处置看板
    # ==================================================================
    def test_001_board_unbuilt_available_false(self):
        self._make_case()
        r = self._board()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertFalse(d["available"])
        self.assertEqual(d["counts"]["total"], 0)
        for lane in (ClueStatus.PENDING, ClueStatus.VERIFYING,
                     ClueStatus.EXCLUDED, ClueStatus.CONFIRMED,
                     ClueStatus.FILED):
            self.assertIn(lane, d["columns"])
            self.assertEqual(d["columns"][lane], [])
        self.assertEqual(d["stale_days_threshold"], 14)

    def test_002_board_cards_lanes_and_state_truth(self):
        self._make_case()
        self._seed_clues(clues=[
            _clue("clue-1", "大额取现线索", score=0.9),
            _clue("clue-2", "内间密线", jians=["内间"], score=0.8),
        ])
        r = self._board()
        d = r.json()["data"]
        self.assertTrue(d["available"])
        self.assertEqual(d["counts"]["total"], 2)
        self.assertEqual(len(d["columns"][ClueStatus.PENDING]), 2)
        card = next(c for c in d["columns"][ClueStatus.PENDING]
                    if c["clue_id"] == "clue-1")
        for field in ("clue_id", "title", "jian_types", "level",
                      "priority_score", "subjects", "status", "note",
                      "operator", "updated_at", "stay_days", "overdue"):
            self.assertIn(field, card)
        self.assertEqual(card["level"], "线索")
        self.assertEqual(card["priority_score"], 0.9)
        self.assertEqual(card["status"], ClueStatus.PENDING)
        self.assertIsNone(card["stay_days"])  # 无处置记录 → null
        self.assertFalse(card["overdue"])
        self.assertTrue(any(s["name"] for s in card["subjects"]))

        # 处置后状态真值进看板（verify → 查证中）
        r = self.client.post(
            "/api/v1/cases/c1/clues/clue-1/actions",
            headers=self.auth_h, json={"action": "verify", "note": "接手"})
        self.assertEqual(r.status_code, 202, r.text)
        self._drain()
        d = self._board().json()["data"]
        self.assertEqual(d["counts"]["by_status"][ClueStatus.VERIFYING], 1)
        self.assertEqual(len(d["columns"][ClueStatus.PENDING]), 1)
        card = next(c for c in d["columns"][ClueStatus.VERIFYING]
                    if c["clue_id"] == "clue-1")
        self.assertTrue(card["updated_at"])
        self.assertEqual(card["stay_days"], 0)
        self.assertFalse(card["overdue"])

    def test_003_board_neijian_filtered_for_soldier(self):
        self._make_case()
        self._seed_clues(clues=[
            _clue("clue-1", "生间线索", jians=["生间"]),
            _clue("clue-2", "内间密线", jians=["内间"]),
        ])
        # 正兵：不见内间卡
        d = self._board(headers=self.auth_s).json()["data"]
        self.assertEqual(d["counts"]["total"], 1)
        self.assertNotIn("clue-2",
                         [c["clue_id"] for c in d["columns"][ClueStatus.PENDING]])
        self.assertIn("access_note", d)
        # human：内间可见
        d = self._board(headers=self.auth_h).json()["data"]
        self.assertEqual(d["counts"]["total"], 2)

    def test_004_board_stay_days_overdue_unit(self):
        """组装器直测：stay_days/overdue 按快照阈值计算。"""
        self._make_case()
        self._seed_clues(clues=[_clue("clue-1", "x")])
        state_map = {"clue-1": {"status": ClueStatus.VERIFYING,
                                "note": "", "operator": "王检察官",
                                "updated_at": "2026-01-01T10:00:00"}}
        out = assemble_board(
            case_dir=self.factory.case_dir("c1"), state_map=state_map,
            role="human",
            snapshot_base=self.svc.snapshot_ontology_root("c1"),
            pack="default", as_of=date(2026, 1, 31))
        card = out["columns"][ClueStatus.VERIFYING][0]
        self.assertEqual(card["stay_days"], 30)
        self.assertTrue(card["overdue"])  # 30 > 14

    def test_005_board_cross_tenant_404(self):
        self._make_case()
        r = self._board(headers=self.auth_l)
        self.assertEqual(r.status_code, 404)

    # ==================================================================
    # W-P-002 列分析
    # ==================================================================
    def test_010_analyze_profile_suggestion_hints(self):
        self._make_case()
        up = self._upload()
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up['upload_id']}/analyze",
            headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["upload_id"], up["upload_id"])
        self.assertEqual(d["format"], "csv")
        self.assertEqual(d["row_count"], 2)
        self.assertTrue(d["sha256"])
        cols = {c["name"]: c for c in d["columns"]}
        self.assertEqual(set(cols), {"公司名", "法人名", "性别"})
        self.assertEqual(cols["性别"]["distinct"], 2)
        self.assertEqual(cols["性别"]["null_rate"], 0.0)
        self.assertTrue(cols["性别"]["samples"])
        # 声明表
        tables = {t["name"]: t for t in d["declared_tables"]}
        self.assertIn("工商信息", tables)
        self.assertIn("主体", tables["工商信息"]["required_columns"])
        # 建议
        sug = d["suggestion"]
        self.assertEqual(sug["target_table"], "工商信息")
        self.assertIn("主体", sug["missing_required"])
        match = {m["target_prop"]: m for m in sug["matches"]}
        self.assertEqual(match["法人"]["match_type"], "fuzzy")
        self.assertEqual(match["法人"]["source_col"], "法人名")
        # 数据元 hints：性别列 → DE_GENDER（enum 全命中 high）
        hint = next(h for h in d["element_hints"] if h["col"] == "性别")
        self.assertEqual(hint["element_id"], "DE_GENDER")
        self.assertEqual(hint["confidence"], 0.9)
        self.assertTrue(hint["evidence"]["match_values"])
        # 纯只读：无任务产生
        self.assertEqual(self.repo.list_tasks(case_id="c1"), [])

    def test_011_analyze_unknown_uid_404(self):
        self._make_case()
        r = self.client.post(
            "/api/v1/cases/c1/sources/up_nope/analyze",
            headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 404)

    def test_012_analyze_lost_staged_file_404(self):
        self._make_case()
        up = self._upload()
        for f in (self.factory.case_dir("c1") / "uploads").glob("*"):
            f.unlink()
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up['upload_id']}/analyze",
            headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 404)

    # ==================================================================
    # W-P-003 映射草稿保存 + 导入回落
    # ==================================================================
    def test_020_put_mapping_draft_saved(self):
        self._make_case()
        up = self._upload()
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_h,
            json={"target_table": "工商信息",
                  "mapping": {"主体": "公司名", "法人": "法人名"},
                  "notes": "向导草稿"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["status"], "staged")
        self.assertEqual(d["table_name"], "工商信息")
        self.assertEqual(d["mapping_json"]["column_map"],
                         {"公司名": "主体", "法人名": "法人"})
        # 列表可见
        lst = self.client.get("/api/v1/cases/c1/sources",
                              headers=self.auth_h).json()["data"]
        self.assertEqual(lst["items"][0]["mapping_json"]["target_table"],
                         "工商信息")

    def test_021_put_mapping_invalid_400(self):
        self._make_case()
        up = self._upload()
        # 非法目标表
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_h,
            json={"target_table": "不存在的表", "mapping": {}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        # 源列不存在
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_h,
            json={"target_table": "工商信息",
                  "mapping": {"主体": "不存在列"}})
        self.assertEqual(r.status_code, 400)
        # 声明列不存在
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_h,
            json={"target_table": "工商信息",
                  "mapping": {"不存在属性": "公司名"}})
        self.assertEqual(r.status_code, 400)

    def test_022_import_falls_back_to_draft_mapping(self):
        self._make_case()
        up = self._upload()
        # 先存草稿
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_h,
            json={"target_table": "工商信息",
                  "mapping": {"主体": "公司名", "法人": "法人名"}})
        self.assertEqual(r.status_code, 200, r.text)
        # 导入请求不带 column_map → 回落草稿
        r = self.client.post(
            f"/api/v1/cases/c1/sources/{up['upload_id']}/import",
            headers=self.auth_h, json={"target_table": "工商信息"})
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        import_task = next(t for t in self.repo.list_tasks(case_id="c1")
                           if t.task_type == "IMPORT")
        self.assertEqual(import_task.status, TASK_SUCCEEDED,
                         f"IMPORT 应成功（回落草稿）：{import_task.error_code} "
                         f"{import_task.error_message}")
        src = self.repo.get_source("c1", up["upload_id"])
        self.assertEqual(src["status"], "imported")
        # 冷层 parquet 已按草稿重命名列
        cold = pd.read_parquet(
            self.factory.case_dir("c1") / "cold" / "工商信息.parquet")
        self.assertIn("主体", cold.columns)
        self.assertIn("法人", cold.columns)
        # imported 态 PUT → 409
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_h,
            json={"target_table": "工商信息", "mapping": {}})
        self.assertEqual(r.status_code, 409)

    def test_023_put_mapping_cross_tenant_404(self):
        self._make_case()
        up = self._upload()
        r = self.client.put(
            f"/api/v1/cases/c1/sources/{up['upload_id']}",
            headers=self.auth_l,
            json={"target_table": "工商信息", "mapping": {}})
        self.assertEqual(r.status_code, 404)

    # ==================================================================
    # W-P-004 关系图谱
    # ==================================================================
    def test_030_graph_unbuilt_empty(self):
        self._make_case()
        r = self.client.get("/api/v1/cases/c1/graph", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertFalse(d["available"])
        self.assertEqual(d["nodes"], [])
        self.assertEqual(d["edges"], [])
        self.assertIn("truncated", d)

    def test_031_graph_nodes_edges_same_domain(self):
        self._make_case()
        self._seed_graph()
        r = self.client.get("/api/v1/cases/c1/graph", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["available"])
        node_ids = {n["id"] for n in d["nodes"]}
        self.assertEqual(node_ids,
                         {"person:p1", "person:p2", "account:a1", "account:a2"})
        self.assertEqual(len(d["edges"]), 2)
        for n in d["nodes"]:
            for field in ("id", "label", "type", "type_title", "jian"):
                self.assertIn(field, n)
        for e in d["edges"]:
            self.assertIn(e["source"], node_ids)
            self.assertIn(e["target"], node_ids)
            self.assertTrue(e["label"])
        transfers = next(e for e in d["edges"] if e["type"] == "transfers")
        self.assertEqual(transfers["source"], "account:a1")
        self.assertEqual(transfers["target"], "account:a2")

    def test_032_graph_truncation(self):
        self._make_case()
        self._seed_graph()
        r = self.client.get(
            "/api/v1/cases/c1/graph?node_limit=1&edge_limit=1",
            headers=self.auth_h)
        d = r.json()["data"]
        self.assertTrue(d["available"])
        self.assertTrue(d["truncated"]["nodes"])
        self.assertLessEqual(len(d["nodes"]), 1 + 2)  # 上限 + 边端点补位
        self.assertLessEqual(len(d["edges"]), 1)

    def test_033_graph_cross_tenant_404(self):
        self._make_case()
        self._seed_graph()
        r = self.client.get("/api/v1/cases/c1/graph", headers=self.auth_l)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
