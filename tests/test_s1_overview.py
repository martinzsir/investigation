"""
tests/test_s1_overview.py
S1/F1 本体总览 API：GET /cases/{cid}/ontology/overview（本体管理器壳数据面）。

验收点（S1 PRD §12）：
  - UC-S1-1：返回 19 项，每项含 name/group/writable/has_schema/
    source_layer/item_count/updated_at 七字段 + status 状态位；
  - UC-S1-2：writable 静态映射 8 可写 / 11 只读；
  - UC-S1-3：has_schema 目录扫描（schemas/ 有 actions/bindings/functions/
    links/objects/policies/rules/views 共 8 个本体文件命中；PRD 写"9 true"
    系把 proposal.schema.json 误计入——proposal 非本体文件，以实测为准）；
  - UC-S1-4：source_layer 三层标注（shared/industry/case，依赖 S0-1）；
  - UC-S1-5（数据面）：语义六组齐全；
  - E1-2：案件快照缺失 → files 空（前端空态），不 500；
  - E1-3/E1-4（UC-S1-12/13）：单文件缺失/解析失败局部降级不整体崩（D6）；
  - E3-1（UC-S1-16）：无行业层 → 只标全域/案件，不报错（D8）；
  - 版本：ontology_version=12 位指纹（前端展示前 8 位 + …）；
  - 权限：未登录 401；跨租户/不存在案件 404（不泄露存在性）。
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
from server.app.routers.ontology_overview import _item_count
from server.app.security import hash_password
from server.app.store import StoreFactory


class S1OverviewTestBase(unittest.TestCase):
    """tmp ontology 根：default 包（补 pack_meta industry=金融）+ _shared + _industry。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.onto_root = self.tmp / "ontology"
        shutil.copytree(ROOT / "ontology" / "default",
                        self.onto_root / "default")
        shared_src = ROOT / "ontology" / "_shared"
        if shared_src.is_dir():
            shutil.copytree(shared_src, self.onto_root / "_shared")
        ind_src = ROOT / "ontology" / "_industry" / "金融"
        if ind_src.is_dir():
            shutil.copytree(ind_src,
                            self.onto_root / "_industry" / "金融")
        # default 包默认无 pack_meta.json——补一份声明行业（建案拷贝按此走）
        (self.onto_root / "default" / "pack_meta.json").write_text(
            json.dumps({"schema_version": 2, "industry": "金融"},
                       ensure_ascii=False), encoding="utf-8")
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases")
        self.svc = CaseService(self.repo, self.factory,
                               ontology_root=self.onto_root,
                               cases_root=self.tmp / "cases")
        self.svc.create_case(case_id="c1", name="海州电诈案",
                             tenant_id="t1", created_by="建案人")
        self.snap_dir = self.svc.snapshot_dir("c1", "default")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestItemCount(unittest.TestCase):
    """条目计数规则：list 直取；dict 累加集合型值，跳过元信息键。"""

    def test_list(self):
        self.assertEqual(_item_count([1, 2, 3]), 3)

    def test_dict_collections_summed(self):
        data = {"schema_version": 2, "_note": "x",
                "object_bindings": [1, 2], "link_bindings": [3]}
        self.assertEqual(_item_count(data), 3)

    def test_dict_of_dicts(self):
        self.assertEqual(_item_count({"elements": {"a": {}, "b": {}}}), 2)

    def test_scalar(self):
        self.assertEqual(_item_count("x"), 0)


class TestOverviewEndpoint(S1OverviewTestBase):
    """HTTP 面：登录态 + 案件租户校验 + 19 项契约。"""

    def setUp(self):
        super().setUp()
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator="测试员", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        self.app = create_app(WebContext(
            repo=self.repo, factory=self.factory, cases=self.svc,
            session_ttl_hours=1))
        self.client = TestClient(self.app)
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "测试员", "password": "pw"})
        self.assertEqual(r.status_code, 200, r.text)
        self.auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        r = self.client.get(
            "/api/v1/cases/c1/ontology/overview", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.body = r.json()
        self.data = self.body["data"]
        self.by_name = {f["name"]: f for f in self.data["files"]}

    # -- UC-S1-1 --
    def test_19_items_with_contract_fields(self):
        self.assertEqual(len(self.data["files"]), 19)
        required = {"name", "group", "writable", "has_schema",
                    "source_layer", "item_count", "updated_at"}
        for f in self.data["files"]:
            self.assertTrue(required.issubset(f), f)
            self.assertIn("status", f)

    def test_names_match_prd_19_files(self):
        expected = {"objects", "links", "views", "data_elements", "bindings",
                    "rules", "functions", "verify_playbooks", "dimensions",
                    "policies", "actions", "states", "llm_policy",
                    "case_knowledge", "enum_space", "thresholds",
                    "scoring", "jians", "derived_properties"}
        self.assertEqual({f["name"] for f in self.data["files"]}, expected)

    # -- UC-S1-2 --
    def test_writable_static_mapping(self):
        # S4 起 actions/states 开放；S5 起 7 个通用文件走「提案→影响面→人工
        # 发布」可写路径；只读仅剩 functions（只读可视化）与 llm_policy（R6）。
        writables = {f["name"] for f in self.data["files"] if f["writable"]}
        self.assertEqual(writables, {"objects", "links", "data_elements",
                                     "bindings", "rules", "policies",
                                     "views", "case_knowledge",
                                     "actions", "states",
                                     "derived_properties", "dimensions",
                                     "enum_space", "jians", "scoring",
                                     "thresholds", "verify_playbooks"})
        self.assertEqual(
            len([f for f in self.data["files"] if f["writable"]]), 17)
        self.assertEqual(
            len([f for f in self.data["files"] if not f["writable"]]), 2)
        self.assertEqual(
            {f["name"] for f in self.data["files"] if not f["writable"]},
            {"functions", "llm_policy"})

    # -- UC-S1-3 --
    def test_has_schema_by_dir_scan(self):
        """schemas/ 实扫：S5 补齐后 19 个本体文件全部有 schema。"""
        with_schema = {f["name"] for f in self.data["files"]
                       if f["has_schema"]}
        self.assertEqual(with_schema, {f["name"] for f in self.data["files"]})
        self.assertEqual(len(with_schema), 19)

    # -- UC-S1-4：三层标注（依赖 S0-1 建案拷贝） --
    def test_source_layer_three_layers_for_data_elements(self):
        self.assertEqual(self.by_name["data_elements"]["source_layer"],
                         ["shared", "industry", "case"])

    def test_source_layer_case_only_for_objects(self):
        self.assertEqual(self.by_name["objects"]["source_layer"], ["case"])

    # -- UC-S1-5 数据面：语义六组 --
    def test_six_semantic_groups(self):
        groups = {f["group"] for f in self.data["files"]}
        self.assertEqual(groups, {"对象模型", "数据", "研判",
                                  "治理", "知识", "计分"})

    def test_item_count_matches_objects_list(self):
        raw = json.loads(
            (self.snap_dir / "objects.json").read_text(encoding="utf-8"))
        self.assertEqual(self.by_name["objects"]["item_count"],
                         len(raw["objects"]))
        self.assertIsInstance(self.by_name["objects"]["updated_at"], str)

    # -- 顶部条版本（F3 / §8.1 前 8 位由前端截断） --
    def test_ontology_version_is_12hex_fingerprint(self):
        v = self.data["ontology_version"]
        self.assertEqual(len(v), 12)
        self.assertEqual(v, self.repo.get_pack_snapshot("c1").version)

    def test_industry_carried_for_warning_copy(self):
        self.assertEqual(self.data["industry"], "金融")
        self.assertEqual(self.data["case_name"], "海州电诈案")


class TestOverviewDegradation(S1OverviewTestBase):
    """E1-2/E1-3/E1-4/E3-1：空态、局部降级、缺层不报错。"""

    def _client(self):
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator="测试员", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        app = create_app(WebContext(repo=self.repo, factory=self.factory,
                                    cases=self.svc, session_ttl_hours=1))
        client = TestClient(app)
        r = client.post("/api/v1/auth/login",
                        json={"operator": "测试员", "password": "pw"})
        auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        return client, auth

    def _get(self, client, auth, case_id="c1"):
        r = client.get(f"/api/v1/cases/{case_id}/ontology/overview",
                       headers=auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def test_missing_file_degrades_not_crashes(self):
        """E1-3/UC-S1-12：手工删文件 → 该项 missing，其余 18 项正常。"""
        (self.snap_dir / "thresholds.json").unlink()
        client, auth = self._client()
        data = self._get(client, auth)
        by_name = {f["name"]: f for f in data["files"]}
        self.assertEqual(len(data["files"]), 19)
        self.assertEqual(by_name["thresholds"]["status"], "missing")
        self.assertIsNone(by_name["thresholds"]["item_count"])
        self.assertIsNone(by_name["thresholds"]["updated_at"])
        self.assertEqual(by_name["objects"]["status"], "ok")

    def test_broken_json_degrades_not_crashes(self):
        """E1-4/UC-S1-13：JSON 改坏 → 该项 parse_error，其余正常。"""
        (self.snap_dir / "jians.json").write_text("{oops", encoding="utf-8")
        client, auth = self._client()
        data = self._get(client, auth)
        by_name = {f["name"]: f for f in data["files"]}
        self.assertEqual(by_name["jians"]["status"], "parse_error")
        self.assertIsNone(by_name["jians"]["item_count"])
        self.assertEqual(by_name["rules"]["status"], "ok")

    def test_no_snapshot_dir_empty_files(self):
        """E1-2/§8.6：快照目录缺失 → files 空（前端空态 + 导入引导）。"""
        shutil.rmtree(self.svc.snapshot_ontology_root("c1"))
        client, auth = self._client()
        data = self._get(client, auth)
        self.assertEqual(data["files"], [])
        self.assertEqual(data["ontology_version"], "")

    def test_no_industry_layer_two_layers_no_error(self):
        """E3-1/UC-S1-16：快照未拷行业层 → 只标全域/案件，不报错（D8）。"""
        shutil.rmtree(self.svc.snapshot_ontology_root("c1") / "_industry")
        (self.snap_dir / "pack_meta.json").unlink()
        client, auth = self._client()
        data = self._get(client, auth)
        by_name = {f["name"]: f for f in data["files"]}
        self.assertEqual(by_name["data_elements"]["source_layer"],
                         ["shared", "case"])
        self.assertIsNone(data["industry"])

    def test_no_shared_layer_case_only(self):
        """全域/行业层都缺（存量案件）→ data_elements 只标案件层。"""
        snap_root = self.svc.snapshot_ontology_root("c1")
        shutil.rmtree(snap_root / "_shared")
        shutil.rmtree(snap_root / "_industry")
        (self.snap_dir / "pack_meta.json").unlink()
        client, auth = self._client()
        data = self._get(client, auth)
        by_name = {f["name"]: f for f in data["files"]}
        self.assertEqual(by_name["data_elements"]["source_layer"], ["case"])

    def test_unauthenticated_401(self):
        client, _auth = self._client()
        r = client.get("/api/v1/cases/c1/ontology/overview")
        self.assertEqual(r.status_code, 401)

    def test_unknown_case_404(self):
        client, auth = self._client()
        r = client.get("/api/v1/cases/不存在/ontology/overview", headers=auth)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
