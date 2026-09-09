"""
tests/test_cross_case_api.py
M5 阶段 A：W-026 跨案件查询通道 + W-027 全有或全无鉴权。

AC：
  W-026:
    AC-1 ATTACH 期间被 ATTACH 库不能就地写（READ_ONLY）；
    AC-4 禁 DDL/DML；
    AC-5 强制 max_rows 上限与超时；
    AC-6 查询进审计（案件列表 + SQL + reason）；
    AC-7 连接按 case 组合缓存复用。
  W-027:
    AC-1 部分授权整体拒绝；
    AC-2 拒绝发生在 ATTACH 之前；
    AC-3 全部授权正常执行；
    AC-5 拒绝进审计。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.backend_cross import evict_cache


def _make_case_db(path: Path, table: str, rows: list[dict]):
    """创建一个单案件 DuckDB 文件，含指定表和数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    cols = list(rows[0].keys()) if rows else ["id"]
    col_defs = ", ".join(f"{c} VARCHAR" for c in cols)
    conn.execute(f"CREATE TABLE {table} ({col_defs})")
    if rows:
        placeholders = ", ".join(["?"] * len(cols))
        conn.executemany(
            f"INSERT INTO {table} VALUES ({placeholders})",
            [tuple(r[c] for c in cols) for r in rows])
    conn.close()


class CrossCaseApiTest(unittest.TestCase):
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
        # 两个租户各一用户
        salt, h = hash_password("pw1")
        self.repo.create_user(User(operator="u1", password_hash=h, salt=salt,
                                   role="human", clearance=4, tenant_id="t1"))
        salt2, h2 = hash_password("pw2")
        self.repo.create_user(User(operator="u2", password_hash=h2, salt=salt2,
                                   role="human", clearance=4, tenant_id="t2"))
        self.h1 = self._login("u1", "pw1")
        self.h2 = self._login("u2", "pw2")
        # 建两案件（同租户 t1）+ 一案件（租户 t2）
        for cid in ("c1", "c2"):
            r = self.client.post("/api/v1/cases", headers=self.h1,
                                 json={"case_id": cid, "name": cid})
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/v1/cases", headers=self.h2,
                             json={"case_id": "c3", "name": "c3"})
        self.assertEqual(r.status_code, 200, r.text)
        # 直接造案件版本文件（绕过 BUILD，测试 ATTACH 用）
        self.factory._meta = self.repo
        _make_case_db(self.factory.version_path("c1", 1), "persons",
                      [{"name": "张三", "phone": "111"}])
        self.repo.set_version("c1", 1, by="test")
        _make_case_db(self.factory.version_path("c2", 1), "persons",
                      [{"name": "李四", "phone": "222"}])
        self.repo.set_version("c2", 1, by="test")
        _make_case_db(self.factory.version_path("c3", 1), "persons",
                      [{"name": "王五", "phone": "333"}])
        self.repo.set_version("c3", 1, by="test")

    def tearDown(self):
        evict_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    # ---- W-026 AC-3/4/5/6：正常查询 + 禁 DDL/DML + max_rows + 审计 ----
    def test_query_success_and_audit(self):
        r = self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"],
                  "sql": "SELECT * FROM case_c1.persons UNION ALL "
                         "SELECT * FROM case_c2.persons",
                  "reason": "串并分析"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["total"], 2)
        names = sorted(row["name"] for row in data["rows"])
        self.assertEqual(names, ["张三", "李四"])
        # AC-6：审计进 ops_events
        evs = self.repo.list_ops(kind="cross_case_query", limit=10)
        self.assertEqual(len(evs), 1)

    def test_ddl_rejected(self):
        r = self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"],
                  "sql": "CREATE TABLE t (x INT)",
                  "reason": "test"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_dml_rejected(self):
        r = self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"],
                  "sql": "INSERT INTO case_c1.persons VALUES ('x','y')",
                  "reason": "test"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_max_rows_enforced(self):
        # 造 5 行数据
        _make_case_db(self.factory.version_path("c1", 1), "t",
                      [{"v": str(i)} for i in range(5)])
        r = self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"], "sql": "SELECT * FROM case_c1.t",
                  "reason": "test", "max_rows": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["total"], 2)

    # ---- W-027 AC-1/2/5：全有或全无鉴权 ----
    def test_partial_denied_before_attach(self):
        """u1 对 c3（t2 租户）无权限 → 整体 403，且拒绝在 ATTACH 前。"""
        # 用 mock 验证 for_cross_case 未被调用
        original = self.factory.for_cross_case
        called = {"n": 0}

        def spy(authorized_cases):
            called["n"] += 1
            return original(authorized_cases)

        self.factory.for_cross_case = spy
        try:
            r = self.client.post(
                "/api/v1/cross-case/query", headers=self.h1,
                json={"case_ids": ["c1", "c3"], "sql": "SELECT 1",
                      "reason": "越权尝试"})
            self.assertEqual(r.status_code, 403, r.text)
            self.assertEqual(called["n"], 0, "ATTACH 不应发生（拒绝须在 ATTACH 前）")
        finally:
            self.factory.for_cross_case = original
        # AC-5：拒绝事件进审计
        evs = self.repo.list_ops(kind="cross_case_denied", limit=10)
        self.assertEqual(len(evs), 1)

    def test_all_authorized_ok(self):
        r = self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"],
                  "sql": "SELECT count(*) AS n FROM case_c1.persons",
                  "reason": "统计"})
        self.assertEqual(r.status_code, 200, r.text)

    # ---- W-026 AC-7：连接缓存复用 ----
    def test_connection_cache_reuse(self):
        from server.app.store.backend_cross import _conn_cache
        evict_cache()
        self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"], "sql": "SELECT 1", "reason": "t"})
        self.assertEqual(len(_conn_cache), 1)
        # 同样组合复用
        self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c2", "c1"], "sql": "SELECT 1", "reason": "t"})
        self.assertEqual(len(_conn_cache), 1)

    def test_history(self):
        self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"], "sql": "SELECT 1", "reason": "h"})
        r = self.client.get("/api/v1/cross-case/history", headers=self.h1)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["total"], 1)

    # ---- C1：history 服务端分页（协议同 tasks）----
    def test_history_pagination(self):
        for i in range(3):
            r = self.client.post(
                "/api/v1/cross-case/query", headers=self.h1,
                json={"case_ids": ["c1", "c2"], "sql": f"SELECT {i} AS v",
                      "reason": f"p{i}"})
            self.assertEqual(r.status_code, 200, r.text)
        # 第 1 页：page_size=2 → 2 条，total=3
        r = self.client.get(
            "/api/v1/cross-case/history?page=1&page_size=2", headers=self.h1)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["page"], 1)
        self.assertEqual(d["page_size"], 2)
        self.assertEqual(len(d["items"]), 2)
        # 第 2 页：1 条
        r = self.client.get(
            "/api/v1/cross-case/history?page=2&page_size=2", headers=self.h1)
        d = r.json()["data"]
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["total"], 3)
        # 旧 limit 参数兼容（映射 page_size）
        r = self.client.get(
            "/api/v1/cross-case/history?limit=1", headers=self.h1)
        d = r.json()["data"]
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["total"], 3)

    def test_history_only_own_operator(self):
        """total/items 仅本 operator：u2 看不到 u1 的记录。"""
        self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c2"], "sql": "SELECT 1", "reason": "h"})
        r = self.client.get("/api/v1/cross-case/history", headers=self.h2)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["items"], [])

    def test_duplicate_case_ids_rejected(self):
        r = self.client.post(
            "/api/v1/cross-case/query", headers=self.h1,
            json={"case_ids": ["c1", "c1"], "sql": "SELECT 1", "reason": "t"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
