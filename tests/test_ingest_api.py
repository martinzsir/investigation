"""
tests/test_ingest_api.py
M3 阶段 E：W-010 数据源注册与列映射向导 + W-012 导入任务幂等。

AC 对应：
  AC-1 五格式上传（CSV/Excel/Parquet/JSON/SQLite）→ 暂存+指纹+列画像；
  W-012 AC-1 同指纹（同名同内容同行数）导入拦截 409；
  AC-2 同内容异名放行；AC-3 同名异内容放行；AC-4 判定阶段无任务行；
  AC-5 失败零残留（映射非法不落冷层）；
  AC-6 清洗声明随映射落 bindings（过 loader 校验）；
  端到端：导入→冷层 parquet→链式 BUILD→obj_transaction 物化。
"""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.pool import WorkerPool

# 银行流水声明列：主体/对方/金额/日期（bindings transaction）
FLOW_COLS = ["主体", "对方", "金额", "日期"]


def flow_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "付款方": [f"张某{i}" for i in range(n)],
        "收款方": [f"李某{i}" for i in range(n)],
        "金额（元）": [str(10000 * (i + 1)) for i in range(n)],
        "交易日期": [f"2026-03-{28 + i:02d}" for i in range(n)],
    })


FLOW_MAP = {"付款方": "主体", "收款方": "对方",
            "金额（元）": "金额", "交易日期": "日期"}


class IngestApiTest(unittest.TestCase):
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
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _upload(self, filename: str, content: bytes,
                headers=None) -> dict:
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=headers or self.auth_h,
            files={"file": (filename, content)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def _import(self, upload_id: str, target: str = "银行流水",
                column_map=None, clean=None, headers=None):
        body = {"target_table": target,
                "column_map": column_map or FLOW_MAP}
        if clean:
            body["clean"] = clean
        return self.client.post(
            f"/api/v1/cases/c1/sources/{upload_id}/import",
            headers=headers or self.auth_h, json=body)

    def _drain(self):
        self.pool.run_until_drained(max_idle_rounds=30)

    # ------------------------------------------------------------------
    # AC-1：五格式上传
    # ------------------------------------------------------------------
    def test_upload_five_formats(self):
        df = flow_df()
        # CSV
        up = self._upload("流水.csv", df.to_csv(index=False).encode("utf-8"))
        self.assertEqual(up["format"], "csv")
        self.assertEqual(up["rows"], 3)
        self.assertIn("银行流水", up["declared_tables"])
        self.assertEqual(set(up["columns"]),
                         {"付款方", "收款方", "金额（元）", "交易日期"})
        # Excel
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        up = self._upload("流水.xlsx", buf.getvalue())
        self.assertEqual(up["format"], "excel")
        self.assertEqual(up["rows"], 3)
        # Parquet
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        up = self._upload("流水.parquet", buf.getvalue())
        self.assertEqual(up["format"], "parquet")
        # JSON
        up = self._upload("流水.json",
                          df.to_json(orient="records", force_ascii=False)
                          .encode("utf-8"))
        self.assertEqual(up["format"], "json")
        # SQLite
        tmpdb = Path(tempfile.mkdtemp()) / "s.db"
        con = sqlite3.connect(str(tmpdb))
        df.to_sql("银行流水表", con, index=False)
        con.commit()
        con.close()
        up = self._upload("流水.db", tmpdb.read_bytes())
        self.assertEqual(up["format"], "sqlite")
        self.assertEqual(up["rows"], 3)

    def test_upload_unsupported_format_400(self):
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("木马.exe", b"MZ\x90\x00")})
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------------------
    # 端到端：导入→冷层→BUILD→语义表
    # ------------------------------------------------------------------
    def test_import_e2e_cold_and_build(self):
        up = self._upload("流水.csv",
                          flow_df(4).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"])
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        tasks = self.repo.list_tasks(case_id="c1")
        by_type = {t.task_type: t for t in tasks}
        self.assertEqual(by_type["IMPORT"].status, TASK_SUCCEEDED,
                         f"{by_type['IMPORT'].error_code} "
                         f"{by_type['IMPORT'].error_message}")
        self.assertEqual(by_type["BUILD"].status, TASK_SUCCEEDED,
                         f"{by_type['BUILD'].error_code} "
                         f"{by_type['BUILD'].error_message}")
        # 冷层 parquet 存在（原子落盘）
        cold = self.factory.case_dir("c1") / "cold" / "银行流水.parquet"
        self.assertTrue(cold.exists())
        # 注册表置 imported
        src = self.repo.get_source("c1", up["upload_id"])
        self.assertEqual(src["status"], "imported")
        # BUILD 产物：obj_transaction 物化为 4 行
        self.assertEqual(self.repo.current_version("c1"), 1)
        store = self.factory.for_case("c1", mode="read")
        try:
            n = store.read_conn.execute(
                "SELECT COUNT(*) FROM obj_transaction").fetchone()[0]
            self.assertEqual(n, 4)
        finally:
            store.close()

    # ------------------------------------------------------------------
    # W-012 幂等
    # ------------------------------------------------------------------
    def test_duplicate_fingerprint_conflict_no_task(self):
        content = flow_df(2).to_csv(index=False).encode("utf-8")
        up1 = self._upload("流水.csv", content)
        up2 = self._upload("流水.csv", content)  # 同名同内容
        self.assertEqual(up1["fingerprint"], up2["fingerprint"])
        r1 = self._import(up1["upload_id"])
        self.assertEqual(r1.status_code, 200)
        # AC-1：第二个导入 409
        r2 = self._import(up2["upload_id"])
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["error"]["code"], "CONFLICT")
        # AC-4：判定阶段无任务行——仅 1 个 IMPORT
        imports = [t for t in self.repo.list_tasks(case_id="c1")
                   if t.task_type == "IMPORT"]
        self.assertEqual(len(imports), 1)

    def test_same_content_different_name_allowed(self):
        df = flow_df(2)
        up1 = self._upload("一月流水.csv",
                           df.to_csv(index=False).encode("utf-8"))
        up2 = self._upload("二月流水.csv",
                           df.to_csv(index=False).encode("utf-8"))
        self.assertNotEqual(up1["fingerprint"], up2["fingerprint"])  # AC-2
        r1 = self._import(up1["upload_id"])
        r2 = self._import(up2["upload_id"])
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_same_name_different_content_allowed(self):
        up1 = self._upload("流水.csv",
                           flow_df(2).to_csv(index=False).encode("utf-8"))
        up2 = self._upload("流水.csv",
                           flow_df(5).to_csv(index=False).encode("utf-8"))
        self.assertNotEqual(up1["fingerprint"], up2["fingerprint"])  # AC-3
        self.assertEqual(self._import(up1["upload_id"]).status_code, 200)
        self.assertEqual(self._import(up2["upload_id"]).status_code, 200)

    # ------------------------------------------------------------------
    # 映射校验 + AC-5 失败零残留
    # ------------------------------------------------------------------
    def test_unknown_target_table_rejected_at_api(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"], target="不存在的表")
        self.assertEqual(r.status_code, 400)
        # 无任务行
        self.assertEqual([t for t in self.repo.list_tasks(case_id="c1")], [])

    def test_unknown_column_rejected_at_api(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"],
                         column_map={"付款方": "不存在列"})
        self.assertEqual(r.status_code, 400)

    def test_unparseable_file_rejected_no_residue(self):
        # AC-5 前置：不可解析文件上传即 400，暂存件清理、无注册行、无冷层
        r = self.client.post(
            "/api/v1/cases/c1/sources/upload", headers=self.auth_h,
            files={"file": ("坏数据.json", b"not a json {{{")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        self.assertEqual(self.repo.list_sources("c1"), [])
        self.assertFalse((self.factory.case_dir("c1") / "cold").exists())
        uploads = self.factory.case_dir("c1") / "uploads"
        if uploads.exists():
            self.assertEqual(list(uploads.iterdir()), [])

    # ------------------------------------------------------------------
    # AC-6：清洗声明落 bindings
    # ------------------------------------------------------------------
    def test_clean_declaration_persists_to_bindings(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self._import(up["upload_id"], clean=["strip"])
        self.assertEqual(r.status_code, 200, r.text)
        self._drain()
        bp = (self.svc.snapshot_dir("c1", "default") / "bindings.json")
        data = json.loads(bp.read_text(encoding="utf-8"))
        tb = next(b for b in data["object_bindings"]
                  if (b.get("source") or {}).get("table") == "银行流水")
        self.assertIn("strip", tb.get("clean") or [])

    # ------------------------------------------------------------------
    # 权限 / 注册表
    # ------------------------------------------------------------------
    def test_soldier_upload_ok_import_forbidden(self):
        up = self._upload("流水.csv",
                          flow_df(2).to_csv(index=False).encode("utf-8"),
                          headers=self.auth_s)
        self.assertEqual(up["rows"], 2)  # 正兵可上传
        r = self._import(up["upload_id"], headers=self.auth_s)
        self.assertEqual(r.status_code, 403)

    def test_list_sources(self):
        self._upload("流水.csv",
                     flow_df(2).to_csv(index=False).encode("utf-8"))
        r = self.client.get("/api/v1/cases/c1/sources", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["status"], "staged")


if __name__ == "__main__":
    unittest.main()
