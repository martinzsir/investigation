"""
tests/test_s0_industry.py
S0-1/F1 行业叠加层：三层合并 + override 覆盖 + 建案拷贝 + temp 校验副本补拷。

验收点：
  - load_data_elements 三层合并：全域 → 行业 → 案件（UC-S0-1）；
  - 仅追加模式：跨层 ID 冲突硬失败；override:true 显式覆盖并记录
    winner/loser（v1.2 §3.0.7/P2-7）；
  - 行业层缺失降级两层不报错（D8 容错优先）；
  - 建案按 pack_meta.json industry 拷贝 _industry 到案件快照（F1 server）；
  - copy_layer_dirs 临时校验副本补拷 _shared + _industry（F1 server）。
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

from core.ontology_loader import (
    _load_data_elements_detailed,
    load_data_elements,
    load_pack,
)

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.snapshot_config import copy_layer_dirs
from server.app.store import StoreFactory

API = "/api/v1"


def _de(eid: str, name: str, **extra) -> dict:
    """最小合法数据元声明（_validate_element_spec 必填 name/type）。"""
    return {"name": name, "type": "string", **extra}


def _elements_file(path: Path, elements: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schema_version": 2, "elements": elements}, ensure_ascii=False,
        indent=2), encoding="utf-8")


class IndustryLayerTestBase(unittest.TestCase):
    """tmp ontology 根：_shared + _industry/金融 + pack（industry=金融）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "ontology"
        _elements_file(self.root / "_shared" / "data_elements.json",
                       {"DE_IDCARD": _de("DE_IDCARD", "公民身份号码",
                                         length=18, clean_rule=["despace"])})
        _elements_file(self.root / "_industry" / "金融" / "data_elements.json",
                       {"DE_FIN_ACCT_NO": _de("DE_FIN_ACCT_NO", "银行账号",
                                              length=19,
                                              clean_rule=["despace"])})
        _elements_file(self.root / "p1" / "data_elements.json",
                       {"DE_CASE_ONLY": _de("DE_CASE_ONLY", "案件特有元素")})
        # create_case 对模板包做 objects.json 存在性检查（建案拷贝用例/API 用例）
        (self.root / "p1" / "objects.json").write_text(
            json.dumps({"schema_version": 2, "objects": []},
                       ensure_ascii=False), encoding="utf-8")
        (self.root / "p1" / "pack_meta.json").write_text(
            json.dumps({"schema_version": 2, "industry": "金融"},
                       ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestThreeLayerMerge(IndustryLayerTestBase):

    def test_three_layer_merge(self):
        """UC-S0-1：行业层数据元并入装载面（全域+行业+案件三层数据元齐全）。"""
        merged = load_data_elements("p1", base_dir=self.root)
        for eid in ("DE_IDCARD", "DE_FIN_ACCT_NO", "DE_CASE_ONLY"):
            self.assertIn(eid, merged)

    def test_conflict_without_override_hard_fail(self):
        """仅追加模式：案件层与行业层 ID 冲突且未声明 override → 硬失败。"""
        _elements_file(self.root / "p1" / "data_elements.json",
                       {"DE_FIN_ACCT_NO": _de("DE_FIN_ACCT_NO", "山寨银行账号")})
        with self.assertRaises(ValueError) as cm:
            load_data_elements("p1", base_dir=self.root)
        self.assertIn("override", str(cm.exception))

    def test_override_true_covers_and_records(self):
        """override:true 覆盖前层同名 ID + 记录 winner/loser（S0-1 E1-2）。"""
        _elements_file(self.root / "p1" / "data_elements.json",
                       {"DE_FIN_ACCT_NO": _de("DE_FIN_ACCT_NO", "案件修订银行账号",
                                              override=True)})
        merged, overrides = _load_data_elements_detailed("p1", self.root)
        self.assertEqual(merged["DE_FIN_ACCT_NO"]["name"], "案件修订银行账号")
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["id"], "DE_FIN_ACCT_NO")
        self.assertIn("案件追加", overrides[0]["winner_layer"])
        self.assertIn("行业叠加", overrides[0]["loser_layer"])

    def test_industry_missing_degrades_two_layers(self):
        """D8：pack 声明 industry=金融 但 _industry 缺失 → 两层装载不报错。"""
        shutil.rmtree(self.root / "_industry")
        merged = load_data_elements("p1", base_dir=self.root)
        self.assertIn("DE_IDCARD", merged)
        self.assertIn("DE_CASE_ONLY", merged)
        self.assertNotIn("DE_FIN_ACCT_NO", merged)

    def test_no_industry_declared_skips_layer(self):
        """pack_meta 无 industry 字段 → 只用全域+案件两层。"""
        (self.root / "p1" / "pack_meta.json").write_text(
            json.dumps({"schema_version": 2}, ensure_ascii=False),
            encoding="utf-8")
        merged = load_data_elements("p1", base_dir=self.root)
        self.assertNotIn("DE_FIN_ACCT_NO", merged)


class TestCreateCaseCopiesIndustry(IndustryLayerTestBase):
    """F1 server：建案按 pack_meta.industry 拷贝行业层到案件快照。"""

    def _make_service(self) -> tuple[CaseService, SqliteMetaRepo]:
        repo = SqliteMetaRepo(self.tmp / "meta.db")
        factory = StoreFactory(cases_root=self.tmp / "cases")
        return CaseService(repo, factory, ontology_root=self.root,
                           cases_root=self.tmp / "cases"), repo

    def test_create_case_copies_industry_layer(self):
        # 行业层声明先于建案写好；p1 引用行业元素时 load_pack 需 objects.json
        # 等全套声明——这里只验拷贝行为本身，不动 p1 的 objects。
        svc, _repo = self._make_service()
        svc.create_case(case_id="c1", name="案", pack_id="p1", created_by="u")
        snap_root = svc.snapshot_ontology_root("c1")
        self.assertTrue(
            (snap_root / "_industry" / "金融" / "data_elements.json").exists())
        # 全域层照旧拷贝
        self.assertTrue((snap_root / "_shared" / "data_elements.json").exists())

    def test_create_case_without_industry_decl(self):
        """pack 未声明 industry → 快照不出现 _industry。"""
        (self.root / "p1" / "pack_meta.json").write_text(
            json.dumps({"schema_version": 2}, ensure_ascii=False),
            encoding="utf-8")
        svc, _repo = self._make_service()
        svc.create_case(case_id="c2", name="案", pack_id="p1", created_by="u")
        self.assertFalse((svc.snapshot_ontology_root("c2") / "_industry")
                         .exists())

    def test_temp_copy_gets_layers(self):
        """copy_layer_dirs：临时校验副本同时获得 _shared + _industry。"""
        tmp_root = self.tmp / "stage"
        (tmp_root / "p1").mkdir(parents=True)
        copy_layer_dirs(self.root / "p1", self.root, tmp_root)
        self.assertTrue((tmp_root / "_shared" / "data_elements.json").exists())
        self.assertTrue(
            (tmp_root / "_industry" / "金融" / "data_elements.json").exists())

    def test_temp_copy_without_industry_dir(self):
        """行业层目录缺失（存量快照）→ 只补 _shared，不报错。"""
        shutil.rmtree(self.root / "_industry")
        tmp_root = self.tmp / "stage"
        (tmp_root / "p1").mkdir(parents=True)
        copy_layer_dirs(self.root / "p1", self.root, tmp_root)
        self.assertTrue((tmp_root / "_shared" / "data_elements.json").exists())
        self.assertFalse((tmp_root / "_industry").exists())


class IndustryElementsApiTest(IndustryLayerTestBase):
    """GET /cases/{cid}/data-elements-industry（S2 建模器行业层下拉读面）。

    行业由案件快照 pack_meta.json 的 industry 决定（与建案拷贝/装载
    合并同源）；无行业声明或层文件缺失回落空集（E3-1 不报错）；只读。
    """

    def _client_and_case(self, case_id: str) -> tuple[TestClient, dict]:
        repo = SqliteMetaRepo(self.tmp / "meta.db")
        factory = StoreFactory(cases_root=self.tmp / "cases", meta=repo)
        svc = CaseService(repo, factory, ontology_root=self.root,
                          cases_root=self.tmp / "cases")
        ctx = WebContext(repo=repo, factory=factory, cases=svc,
                         session_ttl_hours=1)
        client = TestClient(create_app(ctx))
        salt, h = hash_password("pw")
        repo.create_user(User(operator="王检", password_hash=h, salt=salt,
                              role="human", clearance=4, tenant_id="default"))
        r = client.post(f"{API}/auth/login",
                        json={"operator": "王检", "password": "pw"})
        self.assertEqual(r.status_code, 200, r.text)
        auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        svc.create_case(case_id=case_id, name="案", pack_id="p1",
                        created_by="王检")
        return client, auth

    def test_industry_elements_direct(self):
        """pack 声明 industry=金融 → 行业层数据元直出（带行业名）。"""
        client, auth = self._client_and_case("c1")
        r = client.get(f"{API}/cases/c1/data-elements-industry",
                       headers=auth)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["industry"], "金融")
        self.assertIn("DE_FIN_ACCT_NO", data["elements"])

    def test_no_industry_decl_returns_empty(self):
        """pack 未声明 industry → industry=null + 空集（E3-1）。"""
        (self.root / "p1" / "pack_meta.json").write_text(
            json.dumps({"schema_version": 2}, ensure_ascii=False),
            encoding="utf-8")
        client, auth = self._client_and_case("c2")
        r = client.get(f"{API}/cases/c2/data-elements-industry",
                       headers=auth)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertIsNone(data["industry"])
        self.assertEqual(data["elements"], {})

    def test_industry_layer_file_missing_returns_empty(self):
        """存量快照：industry 有声明但 _industry 层文件缺失 → 空集不报错。"""
        shutil.rmtree(self.root / "_industry")
        client, auth = self._client_and_case("c3")
        r = client.get(f"{API}/cases/c3/data-elements-industry",
                       headers=auth)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["industry"], "金融")
        self.assertEqual(data["elements"], {})

    def test_write_requires_ontology_admin(self):
        """S3 起行业层写已开放，但门禁本体管理员（双条件）；非管理员 403。"""
        client, auth = self._client_and_case("c4")
        r = client.put(f"{API}/cases/c4/data-elements-industry",
                       headers=auth, json={"schema_version": 2,
                                           "elements": {}})
        self.assertEqual(r.status_code, 403, r.text)


class TestRealOntologyPacks(unittest.TestCase):
    """仓库真实包回归：金融/医疗行业包与 reqd_case 声明可装载。"""

    def test_repo_industry_packs_load(self):
        merged = load_data_elements("reqd_case")
        self.assertIn("DE_IDCARD", merged)          # 全域层
        self.assertIn("DE_FIN_ACCT_NO", merged)     # 行业层（金融）
        self.assertIn("DE_FIN_ACCT_NO", load_data_elements("reqd_case"))

    def test_repo_pack_load_unchanged(self):
        """reqd_case 快照语义不变：load_pack 正常（行业层合并后引用完整）。"""
        spec = load_pack("reqd_case")
        self.assertGreater(len(spec.objects), 0)


if __name__ == "__main__":
    unittest.main()
