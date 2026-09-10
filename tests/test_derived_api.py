"""
tests/test_derived_api.py
R8（REQ-R8）派生属性端点：

  AC1 GET /cases/{cid}/objects/{type}/{id}/derived/{prop} 取声明化派生属性；
  AC2 响应带 cache 调试字段，until_source_change 同源二次取命中缓存；
  AC3 未 BUILD 案件 available:false（不 500）；
  AC4 未声明属性 / 不存在对象 → 404；
  AC5 person.risk_score 等 AC5 禁名不经声明永远取不到（派生属性未声明 → 404）。
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

from core import derived as derived_reg
from core.ontology import build_ontology
from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory


class DerivedApiTest(unittest.TestCase):
    def setUp(self):
        # 注册表为进程级全局：清掉 person.transaction_count 保证首请求 miss
        derived_reg._REGISTRY.pop("person.transaction_count", None)

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
        self.auth = self._login("王检察官", "pw-pro")
        r = self.client.post("/api/v1/cases", headers=self.auth,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        r2 = self.client.post("/api/v1/cases", headers=self.auth,
                              json={"case_id": "c2", "name": "未构建案"})
        self.assertEqual(r2.status_code, 200, r2.text)
        self._build_case()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _build_case(self):
        store = self.factory.for_case("c1", mode="write", version=1)
        try:
            conn = store.write_conn
            # person 物化 source_sql 为五源 UNION，缺表会整体降级 → 五张源表都建
            conn.execute(
                "CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, "
                "金额 DOUBLE, 日期 VARCHAR)")
            conn.execute(
                "CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, "
                "日期 VARCHAR, 次数 INTEGER)")
            conn.execute(
                "CREATE TABLE 轨迹出行 (主体 VARCHAR, 地点 VARCHAR, 日期 VARCHAR)")
            conn.execute(
                "CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, "
                "发布日期 VARCHAR, 来源 VARCHAR)")
            conn.execute(
                "CREATE TABLE 举报材料 (分类 VARCHAR, 举报日期 VARCHAR, "
                "被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
            conn.execute(
                "INSERT INTO 银行流水 VALUES "
                "('张三','李四',100000,'2021-09-28'),"
                "('张三','王五',200000,'2021-09-29'),"
                "('李四','王五',50000,'2021-09-30')")
            # person source_sql 含「SELECT 对端 FROM 通话记录」，对端不可为 NULL
            conn.execute(
                "INSERT INTO 通话记录 VALUES ('张三','李四','2021-09-28',1)")
            conn.execute(
                "INSERT INTO 轨迹出行 VALUES ('张三','某酒店','2021-09-28')")
            conn.execute(
                "INSERT INTO 公开OSINT VALUES ('张三','公开信息','2021-09-01','网络')")
            conn.execute(
                "INSERT INTO 举报材料 VALUES ('分类','2021-09-01','张三','群众','内容')")
            build_ontology(conn)
        finally:
            store.close()
        self.repo.set_version("c1", 1, by="王检察官")

    def test_derived_happy_path_and_cache(self):
        url = "/api/v1/cases/c1/objects/person/张三/derived/transaction_count"
        r1 = self.client.get(url, headers=self.auth)
        self.assertEqual(r1.status_code, 200, r1.text)
        d1 = r1.json()["data"]
        self.assertTrue(d1["available"])
        self.assertEqual(d1["object_type"], "person")
        self.assertEqual(d1["object_id"], "张三")
        self.assertEqual(d1["property"], "transaction_count")
        self.assertEqual(d1["function"], "integer_transfer_aggregates")
        self.assertEqual(d1["cache_policy"], "until_source_change")
        self.assertIn(d1["cache"], {"hit", "miss"})
        self.assertIn("source_version_set", d1)
        self.assertIn("params_hash", d1)

        # 同源二次取：版本锚点一致 → 命中缓存
        r2 = self.client.get(url, headers=self.auth)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["data"]["cache"], "hit")

    def test_derived_not_built_available_false(self):
        r = self.client.get(
            "/api/v1/cases/c2/objects/person/张三/derived/transaction_count",
            headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertFalse(d["available"])
        self.assertEqual(d["object_type"], "person")

    def test_derived_undeclared_property_404(self):
        r = self.client.get(
            "/api/v1/cases/c1/objects/person/张三/derived/no_such_prop",
            headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)

    def test_derived_unknown_object_404(self):
        r = self.client.get(
            "/api/v1/cases/c1/objects/ghost/张三/derived/transaction_count",
            headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)

    def test_derived_missing_instance_404(self):
        r = self.client.get(
            "/api/v1/cases/c1/objects/person/不存在的人/derived/transaction_count",
            headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)

    def test_ac5_risk_score_never_exposed(self):
        # AC5：启发式打分未在 derived_properties.json 声明 → 端点 404
        r = self.client.get(
            "/api/v1/cases/c1/objects/person/张三/derived/risk_score",
            headers=self.auth)
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
