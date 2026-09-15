"""
tests/test_s3_elements_rules.py
S3 数据元与规则建模：函数只读目录（F4）/ 规则表单化 jian_types 解锁 +
params 置空删除（F3）/ 数据元枚举与引用扫描（F1）/ 全域与行业层写门禁（F2.3）。

验收点：
  - GET /cases/{cid}/functions：函数声明目录（name/title/inputs/output_type/
    impl/parameters/requires），sql 实现文本不外曝（D7/E4-1 只读，无写路由）；
  - PUT /cases/{cid}/rules/{rid}：jian_types 五勾选落盘 + 去重保序 +
    RESCAN 入队（params/enabled 同口径）；白名单外间类 loader 强校验 400 不落盘；
  - params 显式 null = 删除该参数（回落函数声明默认值），loader 校验通过；
  - GET /data-elements/enums：类型/校验算法/清洗 op/遮蔽枚举单一事实源；
  - GET /data-elements/references：objects.json data_element 绑定扫描 +
    by_element 聚合；objects.json 损坏局部降级（E1-4 不阻断）；
  - PUT /data-elements-shared /data-elements-industry：本体管理员双条件
    （能力位 + rank ≥ 偏将）才可写；loader 三层合并校验失败不落盘；
    无行业声明 400（写面显式拒绝，E2-3 读面降级不报错）。
权限：GET 登录即可；PUT rules 需偏将及以上；PUT 上游层需本体管理员。
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
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.meta.models import User
from server.app.security import hash_password
from server.app.store import StoreFactory

API = "/api/v1"


class S3ElementsRulesTest(unittest.TestCase):
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
        for op, pw, role, clr in (("王检察官", "pw-pro", "human", 4),
                                  ("张偏将", "pw-pj", "偏将", 2),
                                  ("李侦查员", "pw-sol", "正兵", 1)):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=op, password_hash=h,
                                       salt=salt, role=role, clearance=clr,
                                       tenant_id="t1"))
        # S0-2 双条件门禁：能力位（rank ≥ 偏将已由 role 保证）
        self.repo.set_user_ontology_admin("张偏将", True)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_pj = self._login("张偏将", "pw-pj")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post(f"{API}/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.snap_dir = self.svc.snapshot_dir("c1", "default")
        self.base_dir = self.svc.snapshot_ontology_root("c1")
        self.rules_path = self.snap_dir / "rules.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator: str, password: str) -> dict[str, str]:
        r = self.client.post(f"{API}/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _get_rule(self, rid: str) -> dict:
        r = self.client.get(f"{API}/cases/c1/rules", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        return next(x for x in r.json()["data"]["rules"] if x["id"] == rid)

    def _rules_digest(self) -> str:
        return self.rules_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # S3-F4 函数声明只读目录
    # ------------------------------------------------------------------
    def test_functions_catalog_no_sql_leak(self):
        r = self.client.get(f"{API}/cases/c1/functions", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        names = {f["name"] for f in data["functions"]}
        self.assertIn("quarter_end_integer_deposits", names)
        self.assertIn("call_frequency_spike", names)
        for f in data["functions"]:
            self.assertNotIn("sql", f)  # sql 实现文本不外曝
        fn = next(f for f in data["functions"]
                  if f["name"] == "call_frequency_spike")
        self.assertEqual(fn["impl"], "py")
        self.assertEqual(fn["impl_ref"], "call_frequency_spike")
        self.assertIn("call", fn["requires"]["objects"])
        self.assertEqual(fn["parameters"]["absolute_threshold"]["type"],
                         "integer")
        sql_fn = next(f for f in data["functions"]
                      if f["name"] == "quarter_end_integer_deposits")
        self.assertEqual(sql_fn["impl"], "sql")
        self.assertIn("obj_transaction", sql_fn["inputs"])

    def test_functions_soldier_readable(self):
        r = self.client.get(f"{API}/cases/c1/functions", headers=self.auth_s)
        self.assertEqual(r.status_code, 200, r.text)

    def test_functions_unknown_case_404(self):
        r = self.client.get(f"{API}/cases/no-such-case/functions",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 404)

    # ------------------------------------------------------------------
    # S3-F3 jian_types 解锁（表单五勾选）
    # ------------------------------------------------------------------
    def test_jian_types_edit_persists_dedupes_and_rescans(self):
        r = self.client.put(f"{API}/cases/c1/rules/R3", headers=self.auth_h,
                            json={"jian_types": ["生间", "反间", "生间"],
                                  "reason": "按表单勾选调整间类"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertIn("jian_types", data["changed"])
        self.assertIsNotNone(data["rescan_task"])  # 间类变更影响机器结果
        self.assertEqual(data["rescan_task"]["task_type"], "RESCAN")
        after = self._get_rule("R3")
        self.assertEqual(after["jian_types"], ["生间", "反间"])  # 去重保序
        # loader 视角一致
        from core.ontology_loader import load_pack
        spec = load_pack("default", base_dir=self.base_dir)
        self.assertEqual(spec.rules["R3"].jian_types, ("生间", "反间"))
        kinds = [e["kind"] for e in self.repo.list_ops(limit=20)]
        self.assertIn("rule_edit", kinds)

    def test_jian_types_outside_whitelist_rejected_not_persisted(self):
        """五间之外（包 jians.json 白名单外）→ loader 强校验 400 不落盘。"""
        digest = self._rules_digest()
        r = self.client.put(f"{API}/cases/c1/rules/R3", headers=self.auth_h,
                            json={"jian_types": ["外间"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._rules_digest(), digest)

    def test_jian_types_bad_shape_rejected(self):
        r = self.client.put(f"{API}/cases/c1/rules/R3", headers=self.auth_h,
                            json={"jian_types": [""]})
        self.assertEqual(r.status_code, 400)

    def test_jian_types_soldier_rejected(self):
        r = self.client.put(f"{API}/cases/c1/rules/R3", headers=self.auth_s,
                            json={"jian_types": ["生间"]})
        self.assertEqual(r.status_code, 403)

    # ------------------------------------------------------------------
    # S3-F3 params 显式 null = 删除（回落函数声明默认值）
    # ------------------------------------------------------------------
    def test_param_null_deletes_key_and_rescans(self):
        self.assertEqual(self._get_rule("R3")["params"]["absolute_threshold"],
                         30)
        r = self.client.put(f"{API}/cases/c1/rules/R3", headers=self.auth_h,
                            json={"params": {"absolute_threshold": None},
                                  "reason": "回落函数默认阈值"})
        self.assertEqual(r.status_code, 200, r.text)
        after = self._get_rule("R3")
        self.assertNotIn("absolute_threshold", after.get("params") or {})
        self.assertIsNotNone(r.json()["data"]["rescan_task"])
        from core.ontology_loader import load_pack
        spec = load_pack("default", base_dir=self.base_dir)
        self.assertNotIn("absolute_threshold", spec.rules["R3"].params)

    def test_param_null_unknown_key_is_noop_ok(self):
        """置空不存在的键 = 幂等无操作，不报错。"""
        r = self.client.put(f"{API}/cases/c1/rules/R3", headers=self.auth_h,
                            json={"params": {"no_such_key": None}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("no_such_key",
                         self._get_rule("R3").get("params") or {})

    # ------------------------------------------------------------------
    # S3-F1 数据元枚举 + 引用扫描
    # ------------------------------------------------------------------
    def test_enums_endpoint_single_source(self):
        r = self.client.get(f"{API}/cases/c1/data-elements/enums",
                            headers=self.auth_s)  # 正兵可读
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        for t in ("string", "decimal", "date", "boolean"):
            self.assertIn(t, data["types"])
        self.assertIn("idcard_mod11", data["checksums"])
        self.assertIn("despace", data["clean_rules"])
        self.assertEqual(data["masks"], ["partial", "full"])

    def test_references_scan_and_aggregate(self):
        r = self.client.get(f"{API}/cases/c1/data-elements/references",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertTrue(data["references"])  # default 包有真实绑定
        by = data["by_element"]
        self.assertIn("DE_IDCARD", by)
        for loc in by["DE_IDCARD"]:
            self.assertIn("object", loc)
            self.assertIn("property", loc)
        for ref in data["references"]:
            self.assertIn("data_element", ref)

    def test_references_degrade_on_broken_objects_json(self):
        """E1-4 局部降级：objects.json 损坏 → 空引用不阻断数据元页。"""
        (self.snap_dir / "objects.json").write_text("{broken", encoding="utf-8")
        r = self.client.get(f"{API}/cases/c1/data-elements/references",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["references"], [])

    # ------------------------------------------------------------------
    # S3-F2.3 全域层写（本体管理员双条件）
    # ------------------------------------------------------------------
    def _shared_doc(self) -> dict:
        r = self.client.get(f"{API}/cases/c1/data-elements-shared",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def test_shared_write_by_ontology_admin(self):
        doc = self._shared_doc()
        self.assertIn("DE_PHONE", doc["elements"])
        doc["elements"]["DE_PHONE"]["length"] = 12
        r = self.client.put(f"{API}/cases/c1/data-elements-shared",
                            headers=self.auth_pj,
                            json={"schema_version": doc.get("schema_version", 2),
                                  "elements": doc["elements"],
                                  "reason": "号码段扩位测试"})
        self.assertEqual(r.status_code, 200, r.text)
        after = self._shared_doc()
        self.assertEqual(after["elements"]["DE_PHONE"]["length"], 12)
        kinds = [e["kind"] for e in self.repo.list_ops(limit=20)]
        self.assertIn("data_elements_shared_edit", kinds)

    def test_shared_write_non_admin_403_not_persisted(self):
        before = self._shared_doc()
        r = self.client.put(f"{API}/cases/c1/data-elements-shared",
                            headers=self.auth_h,  # human 无能力位
                            json={"schema_version": 2,
                                  "elements": before["elements"]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._shared_doc(), before)

    def test_shared_write_missing_elements_key_400(self):
        r = self.client.put(f"{API}/cases/c1/data-elements-shared",
                            headers=self.auth_pj, json={"schema_version": 2})
        self.assertEqual(r.status_code, 400)

    def test_shared_write_loader_fail_not_persisted(self):
        """缺 name/type 的元素 → loader 三层合并校验 400，不落盘。"""
        before = self._shared_doc()
        bad = dict(before["elements"])
        bad["DE_BROKEN"] = {"type": "string"}  # 缺 name
        r = self.client.put(f"{API}/cases/c1/data-elements-shared",
                            headers=self.auth_pj,
                            json={"schema_version": 2, "elements": bad})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._shared_doc(), before)

    # ------------------------------------------------------------------
    # S3-F2.3 行业层写（本体管理员；无行业声明 400）
    # ------------------------------------------------------------------
    def _declare_industry(self, industry: str = "金融") -> None:
        meta_path = self.snap_dir / "pack_meta.json"
        meta = (json.loads(meta_path.read_text(encoding="utf-8"))
                if meta_path.is_file() else {})
        meta["industry"] = industry
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def test_industry_write_without_industry_decl_400(self):
        r = self.client.put(f"{API}/cases/c1/data-elements-industry",
                            headers=self.auth_pj,
                            json={"schema_version": 2, "elements": {}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("行业", r.json()["error"]["message"])

    def test_industry_write_by_ontology_admin(self):
        self._declare_industry()
        layer_dir = self.base_dir / "_industry" / "金融"
        layer_dir.mkdir(parents=True, exist_ok=True)
        (layer_dir / "data_elements.json").write_text(
            json.dumps({"schema_version": 2,
                        "elements": {"DE_FIN_TEST": {"name": "银行账号",
                                                     "type": "string"}}},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        # 读面确认行业层直出（与写面同源）
        r = self.client.get(f"{API}/cases/c1/data-elements-industry",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["industry"], "金融")

        r = self.client.put(f"{API}/cases/c1/data-elements-industry",
                            headers=self.auth_pj,
                            json={"schema_version": 2,
                                  "elements": {"DE_FIN_TEST": {
                                      "name": "银行结算账号",
                                      "type": "string"}},
                                  "reason": "行业口径更名"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["industry"], "金融")
        on_disk = json.loads(
            (layer_dir / "data_elements.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["elements"]["DE_FIN_TEST"]["name"],
                         "银行结算账号")
        kinds = [e["kind"] for e in self.repo.list_ops(limit=20)]
        self.assertIn("data_elements_industry_edit", kinds)

    def test_industry_write_non_admin_403(self):
        self._declare_industry()
        r = self.client.put(f"{API}/cases/c1/data-elements-industry",
                            headers=self.auth_h,
                            json={"schema_version": 2, "elements": {}})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
