"""
tests/test_model_designer.py
M4 阶段 A：W-013 对象模型设计器。

AC：
  AC-1 新建对象产物通过 loader 强校验；
  AC-2 值类型仅 TYPE_SQL 支持集合，非法拒绝；
  AC-3 kind=entity/event 差异提示（非法 kind 拒绝）；
  AC-4 链接 from_obj/to_obj 必须引用已声明对象，否则拒绝；
  AC-5 间类仅可选五间，非法拒绝；
  AC-6 设计器产物与手写 JSON 等价（同一套校验通过）。
权限：GET 登录即可；PUT 需偏将及以上。
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
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory


class ModelDesignerTest(unittest.TestCase):
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
        salt2, h2 = hash_password("pw-pj")
        self.repo.create_user(User(operator="张偏将", password_hash=h2,
                                   salt=salt2, role="偏将", clearance=2,
                                   tenant_id="t1"))
        salt3, h3 = hash_password("pw-sol")
        self.repo.create_user(User(operator="李侦查员", password_hash=h3,
                                   salt=salt3, role="正兵", clearance=1,
                                   tenant_id="t1"))
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_pj = self._login("张偏将", "pw-pj")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.snap_dir = self.svc.snapshot_dir("c1", "default")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _current_objects(self) -> list[dict]:
        data = json.loads((self.snap_dir / "objects.json").read_text(
            encoding="utf-8"))
        return data["objects"]

    def _add_object_binding(self, obj_name: str, sql: str) -> None:
        """为新对象补一个最小 binding（loader 要求非 runtime 对象有 binding）。"""
        path = self.snap_dir / "bindings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["object_bindings"].append({
            "object": obj_name, "source_table": obj_name,
            "source_sql": sql})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def _add_link_binding(self, link_name: str, sql: str) -> None:
        path = self.snap_dir / "bindings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["link_bindings"].append({"link": link_name, "build_sql": sql})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ---- 读 ----
    def test_get_objects(self):
        r = self.client.get("/api/v1/cases/c1/objects", headers=self.auth_s)
        self.assertEqual(r.status_code, 200, r.text)
        names = {o["name"] for o in r.json()["data"]["objects"]}
        self.assertIn("person", names)
        self.assertIn("transaction", names)

    def test_get_links(self):
        r = self.client.get("/api/v1/cases/c1/links", headers=self.auth_s)
        self.assertEqual(r.status_code, 200, r.text)
        names = {l["name"] for l in r.json()["data"]["links"]}
        self.assertIn("transfers", names)

    # ---- 写：对象 ----
    def test_put_objects_valid(self):
        """AC-1/6：合法对象保存过 loader，与手写等价。"""
        self._add_object_binding("vehicle", "SELECT 车牌 AS plate FROM 车辆")
        objs = self._current_objects()
        objs.append({"name": "vehicle", "title": "车辆", "pk": "vid",
                     "kind": "entity", "name_property": "plate",
                     "properties": {"plate": "string"}})
        r = self.client.put("/api/v1/cases/c1/objects", headers=self.auth_pj,
                            json={"objects": objs})
        self.assertEqual(r.status_code, 200, r.text)
        saved = {o["name"] for o in self._current_objects()}
        self.assertIn("vehicle", saved)
        # AC-6：整包校验仍通过
        r2 = self.client.post("/api/v1/cases/c1/validate",
                              headers=self.auth_h)
        self.assertEqual(r2.status_code, 200, r2.text)

    def test_put_objects_bad_value_type(self):
        """AC-2：值类型非法 → 400，不落盘。"""
        before = self._current_objects()
        objs = [{"name": "x", "pk": "id", "kind": "entity",
                 "name_property": "n",
                 "properties": {"n": "bogus_type"}}]
        r = self.client.put("/api/v1/cases/c1/objects", headers=self.auth_pj,
                            json={"objects": objs})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._current_objects(), before)  # 未落盘

    def test_put_objects_bad_kind(self):
        """AC-3：kind 非法 → 400。"""
        objs = [{"name": "x", "pk": "id", "kind": "ghost",
                 "name_property": "n", "properties": {"n": "string"}}]
        r = self.client.put("/api/v1/cases/c1/objects", headers=self.auth_pj,
                            json={"objects": objs})
        self.assertEqual(r.status_code, 400, r.text)

    def test_put_objects_bad_jian(self):
        """AC-5：间类非法 → 400。"""
        objs = [{"name": "x", "pk": "id", "kind": "entity",
                 "name_property": "n", "jian": "第六间",
                 "properties": {"n": "string"}}]
        r = self.client.put("/api/v1/cases/c1/objects", headers=self.auth_pj,
                            json={"objects": objs})
        self.assertEqual(r.status_code, 400, r.text)

    def test_put_objects_forbidden_for_soldier(self):
        """正兵无写权限 → 403。"""
        r = self.client.put("/api/v1/cases/c1/objects", headers=self.auth_s,
                            json={"objects": []})
        self.assertEqual(r.status_code, 403, r.text)

    # ---- 写：链接 ----
    def test_put_links_valid(self):
        self._add_link_binding(
            "knows",
            "SELECT p1.person_id AS from_person, p2.person_id AS to_person "
            "FROM obj_person p1 JOIN obj_person p2 ON p1.person_id <> p2.person_id")
        links = json.loads((self.snap_dir / "links.json").read_text(
            encoding="utf-8"))["links"]
        links.append({"name": "knows", "from_obj": "person",
                      "to_obj": "person", "properties": {}})
        r = self.client.put("/api/v1/cases/c1/links", headers=self.auth_pj,
                            json={"links": links})
        self.assertEqual(r.status_code, 200, r.text)
        saved = {l["name"] for l in json.loads(
            (self.snap_dir / "links.json").read_text(encoding="utf-8"))["links"]}
        self.assertIn("knows", saved)

    def test_put_links_undeclared_endpoint(self):
        """AC-4：from_obj 未声明 → 400，不落盘。"""
        before = json.loads((self.snap_dir / "links.json").read_text(
            encoding="utf-8"))["links"]
        links = [{"name": "bad", "from_obj": "nonexistent",
                  "to_obj": "person", "properties": {}}]
        r = self.client.put("/api/v1/cases/c1/links", headers=self.auth_pj,
                            json={"links": links})
        self.assertEqual(r.status_code, 400, r.text)
        after = json.loads((self.snap_dir / "links.json").read_text(
            encoding="utf-8"))["links"]
        self.assertEqual(after, before)

    # ---- 校验 ----
    def test_validate_pack(self):
        r = self.client.post("/api/v1/cases/c1/validate", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["valid"])


if __name__ == "__main__":
    unittest.main()
