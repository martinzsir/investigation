"""
tests/test_package_api.py
M5 阶段 B：W-028 案件包导出 + W-029 校验与导入。

AC：
  W-028:
    AC-1 13 声明文件齐全 + schema_version 一致；
    AC-2 版本压实（仅最终版本）；
    AC-3 manifest 逐文件 SHA-256；
    AC-4 审计链冻结校验；
    AC-5 case_knowledge.json 标 sensitive；
    AC-6 chain_ok=false 告警但允许继续；
    AC-7 README 含验证与复现命令。
  W-029:
    AC-1 校验通过可导入；
    AC-2 篡改一字节失败；
    AC-3 缺声明文件失败；
    AC-4 schema_version 不一致失败；
    AC-5 root_hash 不匹配失败；
    AC-6 复用 init_pack；
    AC-7 导入后可查询。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import CaseRecord, User, CASE_ACTIVE
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.worker.package import verify_package

DECL_FILES = (
    "objects.json", "links.json", "bindings.json", "rules.json",
    "actions.json", "functions.json", "policies.json", "views.json",
    "data_elements.json", "dimensions.json", "case_knowledge.json",
    "enum_space.json", "llm_policy.json",
)


def _minimal_declarations() -> dict[str, dict]:
    """最小合法声明集合（schema_version=2）。"""
    return {
        "objects.json": {"schema_version": 2, "objects": []},
        "links.json": {"schema_version": 2, "links": []},
        "bindings.json": {"schema_version": 2, "object_bindings": [],
                          "link_bindings": []},
        "rules.json": {"schema_version": 2, "rules": []},
        "actions.json": {"schema_version": 2, "actions": []},
        "functions.json": {"schema_version": 2, "functions": []},
        "policies.json": {"schema_version": 2, "policies": []},
        "views.json": {"schema_version": 2, "views": []},
        "data_elements.json": {"schema_version": 2, "elements": {}},
        "dimensions.json": {"schema_version": 2, "dimensions": []},
        "case_knowledge.json": {"schema_version": 2, "assertions": []},
        "enum_space.json": {"schema_version": 2, "enums": {}},
        "llm_policy.json": {"schema_version": 2, "llm_enabled": False},
    }


class PackageApiTest(unittest.TestCase):
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
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator="u1", password_hash=h, salt=salt,
                                   role="human", clearance=4, tenant_id="t1"))
        self.h = self._login("u1", "pw")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": password})
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _make_case_with_data(self, cid: str):
        """造一个有声明快照 + DuckDB 数据的案件。"""
        snap = self.factory.case_dir(cid) / "ontology" / cid
        snap.mkdir(parents=True, exist_ok=True)
        decls = _minimal_declarations()
        for name, data in decls.items():
            (snap / name).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # DuckDB 数据
        db = self.factory.case_dir(cid) / "v1.duckdb"
        conn = duckdb.connect(str(db))
        conn.execute("CREATE TABLE t (v VARCHAR)")
        conn.execute("INSERT INTO t VALUES ('hello')")
        conn.close()
        # 元数据
        self.repo.create_case(CaseRecord(
            id=cid, name=cid, tenant_id="t1", pack_id=cid,
            status=CASE_ACTIVE, pack_snapshot_at="abc",
            created_by="u1", created_at="2026-01-01T00:00:00"))
        self.repo.set_version(cid, 1, by="test")

    # ---- W-028：导出 ----
    def test_export_manifest_and_declarations(self):
        self._make_case_with_data("c1")
        from server.app.worker.package import handle_export
        from server.app.worker.tasks import TaskRow
        task = TaskRow(id="t_exp", case_id="c1", task_type="EXPORT",
                       params={}, created_by="u1")
        result = handle_export(task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        self.assertTrue(result["chain_ok"] in (True, False))
        zip_path = Path(result["zip_path"])
        self.assertTrue(zip_path.exists())
        # 解压检查
        extract = self.tmp / "extracted"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract)
        manifest = json.loads((extract / "manifest.json").read_text("utf-8"))
        # AC-1：13 声明文件齐全
        for name in DECL_FILES:
            self.assertIn(f"ontology/c1/{name}", manifest["files"],
                          f"缺声明文件：{name}")
        # AC-3：逐文件 SHA-256
        for rel, info in manifest["files"].items():
            self.assertTrue(info["sha256"])
        # AC-5：case_knowledge 标 sensitive
        ck = manifest["files"].get("ontology/c1/case_knowledge.json")
        self.assertTrue(ck and ck["sensitive"])
        # AC-7：README
        self.assertTrue((extract / "README.md").exists())

    def test_export_version_compaction(self):
        """AC-2：导出仅含最终版本（v1），不含旧版本。"""
        self._make_case_with_data("c1")
        # 造一个旧版本文件 v0
        (self.factory.case_dir("c1") / "v0.duckdb").write_bytes(b"old")
        from server.app.worker.package import handle_export
        from server.app.worker.tasks import TaskRow
        task = TaskRow(id="t_exp2", case_id="c1", task_type="EXPORT",
                       params={}, created_by="u1")
        result = handle_export(task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        extract = self.tmp / "extracted2"
        with zipfile.ZipFile(result["zip_path"]) as zf:
            zf.extractall(extract)
        db_files = list(extract.glob("v*.duckdb"))
        self.assertEqual(len(db_files), 1)
        self.assertEqual(db_files[0].name, "v1.duckdb")

    # ---- W-029：校验 ----
    def test_verify_tampered_byte_fails(self):
        """AC-2：篡改一字节校验失败。"""
        self._make_case_with_data("c1")
        from server.app.worker.package import handle_export
        from server.app.worker.tasks import TaskRow
        task = TaskRow(id="t1", case_id="c1", task_type="EXPORT",
                       params={}, created_by="u1")
        result = handle_export(task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        extract = self.tmp / "ext"
        with zipfile.ZipFile(result["zip_path"]) as zf:
            zf.extractall(extract)
        # 篡改声明文件一个字节（触发 SHA-256 不匹配）
        p = extract / "ontology" / "c1" / "objects.json"
        data = bytearray(p.read_bytes())
        data[0] ^= 0xFF
        p.write_bytes(bytes(data))
        res = verify_package(extract)
        self.assertFalse(res["ok"])
        self.assertTrue(any("SHA-256" in e for e in res["errors"]))

    def test_verify_missing_declaration_fails(self):
        """AC-3：缺声明文件失败。"""
        self._make_case_with_data("c1")
        from server.app.worker.package import handle_export
        from server.app.worker.tasks import TaskRow
        task = TaskRow(id="t2", case_id="c1", task_type="EXPORT",
                       params={}, created_by="u1")
        result = handle_export(task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        extract = self.tmp / "ext3"
        with zipfile.ZipFile(result["zip_path"]) as zf:
            zf.extractall(extract)
        (extract / "ontology" / "c1" / "rules.json").unlink()
        res = verify_package(extract)
        self.assertFalse(res["ok"])
        self.assertTrue(any("rules.json" in e for e in res["errors"]))

    def test_verify_schema_version_mismatch_fails(self):
        """AC-4：schema_version 不一致失败。"""
        self._make_case_with_data("c1")
        from server.app.worker.package import handle_export
        from server.app.worker.tasks import TaskRow
        task = TaskRow(id="t3", case_id="c1", task_type="EXPORT",
                       params={}, created_by="u1")
        result = handle_export(task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        extract = self.tmp / "ext4"
        with zipfile.ZipFile(result["zip_path"]) as zf:
            zf.extractall(extract)
        p = extract / "ontology" / "c1" / "objects.json"
        data = json.loads(p.read_text("utf-8"))
        data["schema_version"] = 999
        p.write_text(json.dumps(data), encoding="utf-8")
        res = verify_package(extract)
        self.assertFalse(res["ok"])

    # ---- A1：verify 七步清单 + 敏感文件名单 ----
    def _export_extract(self, cid: str, task_id: str):
        self._make_case_with_data(cid)
        from server.app.worker.package import handle_export
        from server.app.worker.tasks import TaskRow
        task = TaskRow(id=task_id, case_id=cid, task_type="EXPORT",
                       params={}, created_by="u1")
        result = handle_export(task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        extract = self.tmp / f"ext_{task_id}"
        with zipfile.ZipFile(result["zip_path"]) as zf:
            zf.extractall(extract)
        return extract, cid

    def test_verify_steps_seven_and_sensitive_files(self):
        """合法包：7 步齐全（chain/duckdb 允许 warn），敏感文件列出。"""
        extract, cid = self._export_extract("c1", "t_ok")
        res = verify_package(extract)
        self.assertTrue(res["ok"], res["errors"])
        steps = res["steps"]
        self.assertEqual([s["key"] for s in steps],
                         ["format", "manifest", "hash", "declarations",
                          "schema", "chain", "duckdb"])
        for s in steps:
            self.assertIn(s["status"], ("pass", "warn"),
                          f"{s['key']} 不应 fail：{s['detail']}")
        # 敏感文件名单含 case_knowledge.json
        self.assertTrue(
            any("case_knowledge.json" in f for f in res["sensitive_files"]),
            res["sensitive_files"])

    def test_verify_steps_fail_mapped(self):
        """篡改包：hash 步 fail；缺声明文件：declarations 步 fail。"""
        extract, cid = self._export_extract("c1", "t_bad")
        p = extract / "ontology" / cid / "objects.json"
        data = bytearray(p.read_bytes())
        data[0] ^= 0xFF
        p.write_bytes(bytes(data))
        res = verify_package(extract)
        self.assertFalse(res["ok"])
        by_key = {s["key"]: s for s in res["steps"]}
        self.assertEqual(by_key["hash"]["status"], "fail")
        self.assertEqual(by_key["format"]["status"], "pass")

        extract2, cid2 = self._export_extract("c2", "t_miss")
        (extract2 / "ontology" / cid2 / "rules.json").unlink()
        res2 = verify_package(extract2)
        self.assertFalse(res2["ok"])
        by_key2 = {s["key"]: s for s in res2["steps"]}
        self.assertEqual(by_key2["declarations"]["status"], "fail")

    def test_verify_steps_fatal_no_manifest(self):
        """无 manifest：format fail，其余步标 fail（未执行），不抛异常。"""
        d = self.tmp / "no_manifest"
        d.mkdir(parents=True)
        res = verify_package(d)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["steps"]), 7)
        self.assertEqual(res["steps"][0]["key"], "format")
        self.assertEqual(res["steps"][0]["status"], "fail")
        self.assertTrue(all(s["status"] == "fail" for s in res["steps"]))

    # ---- W-029：导入 ----
    def test_import_creates_queryable_case(self):
        """AC-1/6/7：校验通过可导入，复用 init_pack，导入后可查询。"""
        self._make_case_with_data("c1")
        from server.app.worker.package import handle_export, handle_import_package
        from server.app.worker.tasks import TaskRow
        # 导出
        exp_task = TaskRow(id="t_exp", case_id="c1", task_type="EXPORT",
                           params={}, created_by="u1")
        result = handle_export(exp_task, repo=self.repo, factory=self.factory,
                               cases_root=self.tmp / "cases")
        # 导入为新案件 c2
        imp_task = TaskRow(id="t_imp", case_id="c2", task_type="IMPORT_PACKAGE",
                           params={"zip_path": result["zip_path"],
                                   "case_id": "c2", "name": "导入案",
                                   "tenant_id": "t1"},
                           created_by="u1")
        imp_result = handle_import_package(
            imp_task, repo=self.repo, factory=self.factory,
            cases_root=self.tmp / "cases")
        self.assertEqual(imp_result["case_id"], "c2")
        # 导入后可查询
        store = self.factory.for_case("c2", mode="read", version=1)
        rows = store.query("SELECT * FROM t")
        store.close()
        self.assertEqual(rows, [{"v": "hello"}])


if __name__ == "__main__":
    unittest.main()
