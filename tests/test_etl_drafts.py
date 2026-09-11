"""
tests/test_etl_drafts.py
v1.3 Phase 4 ETL 处置草稿端点（IN-TC-05/06/07/18/19/20/22）。

覆盖：
  IN-TC-05  element_hints[].clean_rule 为 list（含 strip 系 op）+ format 字段；
            declared_tables_view 含 object 字段；A 类草稿创建+自审通过。
  IN-TC-06  B 类：preview 返回 affected_rows → 创建草稿携带预演数据。
  IN-TC-07  草稿创建不自动改 bindings.clean（drafts don't auto-apply）。
  IN-TC-18  草稿列表 + upload_id 过滤（与 de_recommendation 分表）。
  IN-TC-19  A 类草稿确认后可发布（已确认 → 已发布）。
  IN-TC-20  B 类草稿未确认 → publish 409（fail-closed）。
  IN-TC-22  待复核 / 已驳回 草稿不可发布（publish 409）。
"""
from __future__ import annotations

import shutil
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
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory

API = "/api/v1"


def _flow_df(n: int = 3) -> pd.DataFrame:
    """银行流水 CSV：金额列含千分位逗号（48,000.00），手机号列命中 DE_PHONE。"""
    return pd.DataFrame({
        "主体": [f"张某{i}" for i in range(n)],
        "对方": [f"李某{i}" for i in range(n)],
        "金额": ["48,000.00", "12,500.00", "3,200.00"][:n],
        "日期": [f"2026-03-{28 + i:02d}" for i in range(n)],
        "手机号": ["13800138000", "13912345678", "13700000000"][:n],
    })


class EtlDraftTest(unittest.TestCase):
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

        # 分析师（clearance≥2）
        salt, h = hash_password("pw-pro")
        self.repo.create_user(User(operator="王检察官", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        self.auth_h = self._login("王检察官", "pw-pro")

        # 建案（同时创建 state.sqlite）
        r = self.client.post(f"{API}/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

        # 上传含千分位金额列 + 手机号列的 CSV
        content = _flow_df(3).to_csv(index=False).encode("utf-8")
        r = self.client.post(f"{API}/cases/c1/sources/upload",
                             headers=self.auth_h,
                             files={"file": ("流水.csv", content)})
        self.assertEqual(r.status_code, 200, r.text)
        self.upload_id = r.json()["data"]["upload_id"]

        # 分析上传件
        r = self.client.post(
            f"{API}/cases/c1/sources/{self.upload_id}/analyze",
            headers=self.auth_h, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.analyze = r.json()["data"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def _login(self, operator, password):
        r = self.client.post(f"{API}/auth/login",
                             json={"operator": operator,
                                   "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _create_draft(self, op_class="A", op_token="strip_thousands",
                      **extra):
        body = {"upload_id": self.upload_id,
                "target_object": "transaction",
                "target_prop": "amount",
                "op_token": op_token,
                "op_class": op_class}
        body.update(extra)
        r = self.client.post(f"{API}/cases/c1/etl-drafts",
                             headers=self.auth_h, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    # ------------------------------------------------------------------
    # IN-TC-05：element_hints clean_rule + format；declared_tables object；
    #           A 类草稿创建 + 自审通过
    # ------------------------------------------------------------------
    def test_05_element_hints_have_clean_rule(self):
        """IN-TC-05: element_hints[].clean_rule 是 list（含 strip 系 op），
        format 字段存在；declared_tables_view 含 object 字段。"""
        hints = self.analyze["element_hints"]
        self.assertTrue(hints, "element_hints 不应为空")

        # 手机号列命中 DE_PHONE（format 正则），clean_rule=["strip_cc","digits_only"]
        phone_hint = next((h for h in hints if h["col"] == "手机号"), None)
        self.assertIsNotNone(phone_hint, "应有手机号列的 element_hint")
        self.assertEqual(phone_hint["element_id"], "DE_PHONE")
        cr = phone_hint["clean_rule"]
        self.assertIsInstance(cr, list)
        self.assertIn("strip_cc", cr)
        self.assertIn("digits_only", cr)
        # format 字段（IN-TC-05 增强）
        self.assertIsNotNone(phone_hint.get("format"))

        # declared_tables_view 含 object 字段（IN-TC-05 增强）
        tables = self.analyze["declared_tables"]
        self.assertTrue(tables)
        for t in tables:
            self.assertIn("object", t)
        flow_tbl = next((t for t in tables if t["name"] == "银行流水"), None)
        self.assertIsNotNone(flow_tbl)
        # 銀行流水被 account(source_sql) 与 transaction(结构化 source) 双绑定，
        # object 字段取首个声明该源表的绑定（account）
        self.assertIn(flow_tbl["object"], ("account", "transaction"))

    def test_05_class_a_apply_strip_thousands(self):
        """IN-TC-05: A 类草稿创建（待复核）→ 自审通过（已确认），
        reviewed_by == created_by。"""
        draft = self._create_draft(op_class="A", op_token="strip_thousands")
        self.assertEqual(draft["status"], "待复核")
        self.assertEqual(draft["op_class"], "A")
        self.assertEqual(draft["op_token"], "strip_thousands")
        created_by = draft["created_by"]

        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft['draft_id']}/confirm",
            headers=self.auth_h, json={"note": "自审通过"})
        self.assertEqual(r.status_code, 200, r.text)
        confirmed = r.json()["data"]
        self.assertEqual(confirmed["status"], "已确认")
        self.assertEqual(confirmed["reviewed_by"], created_by)

    # ------------------------------------------------------------------
    # IN-TC-06：B 类 preview 返回 affected_rows → 创建草稿携带预演数据
    # ------------------------------------------------------------------
    def test_06_class_b_preview_then_confirm(self):
        """IN-TC-06: preview 返回 affected_rows 字段；B 类草稿携带预演数据。"""
        r = self.client.post(f"{API}/cases/c1/etl-pipeline/preview",
                             headers=self.auth_h,
                             json={"upload_id": self.upload_id,
                                   "source_col": "金额",
                                   "op_token": "strip_thousands"})
        self.assertEqual(r.status_code, 200, r.text)
        preview = r.json()["data"]
        self.assertEqual(preview["op"], "strip_thousands")
        self.assertEqual(preview["source_col"], "金额")
        self.assertIn("affected_rows", preview)
        self.assertGreater(preview["affected_rows"], 0)
        self.assertGreater(len(preview["samples"]), 0)
        # before 含逗号，after 不含
        for pair in preview["samples"]:
            self.assertIn(",", pair["before"])
            self.assertNotIn(",", pair["after"])

        draft = self._create_draft(
            op_class="B", op_token="strip_thousands",
            preview_affected_rows=preview["affected_rows"],
            preview_samples=preview["samples"])
        self.assertEqual(draft["status"], "待复核")
        self.assertEqual(draft["op_class"], "B")
        self.assertEqual(draft["preview_affected_rows"],
                         preview["affected_rows"])

    # ------------------------------------------------------------------
    # IN-TC-07：草稿创建不自动改 bindings.clean
    # ------------------------------------------------------------------
    def test_07_draft_not_auto_effective(self):
        """IN-TC-07: 草稿创建不改变 bindings.clean（drafts don't auto-apply）。"""
        r = self.client.get(f"{API}/cases/c1/etl-pipeline",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        txn_before = next(
            s for s in r.json()["data"]["sources"]
            if s["object"] == "transaction")
        clean_before = set(txn_before.get("clean", []))

        self._create_draft(op_class="A", op_token="strip_thousands")

        r = self.client.get(f"{API}/cases/c1/etl-pipeline",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        txn_after = next(
            s for s in r.json()["data"]["sources"]
            if s["object"] == "transaction")
        clean_after = set(txn_after.get("clean", []))
        self.assertEqual(clean_before, clean_after,
                         "草稿创建不应改变 bindings.clean")
        self.assertNotIn("strip_thousands", clean_after,
                         "strip_thousands 不应自动出现在 bindings.clean")

    # ------------------------------------------------------------------
    # IN-TC-18：草稿列表 + upload_id 过滤
    # ------------------------------------------------------------------
    def test_18_list_drafts_with_upload_id_filter(self):
        """IN-TC-18: 草稿列表（可按 upload_id 过滤；与 de_recommendation 分表）。"""
        # 创建两条草稿
        d1 = self._create_draft(op_class="A")
        d2 = self._create_draft(op_class="B")

        # 全量列表
        r = self.client.get(f"{API}/cases/c1/etl-drafts",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        self.assertEqual(data["total"], 2)
        ids = {item["draft_id"] for item in data["items"]}
        self.assertEqual(ids, {d1["draft_id"], d2["draft_id"]})

        # 按 upload_id 过滤
        r = self.client.get(
            f"{API}/cases/c1/etl-drafts?upload_id={self.upload_id}",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        filtered = r.json()["data"]
        self.assertEqual(filtered["total"], 2)
        for item in filtered["items"]:
            self.assertEqual(item["upload_id"], self.upload_id)

        # 不存在的 upload_id → 空
        r = self.client.get(
            f"{API}/cases/c1/etl-drafts?upload_id=nonexistent",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["total"], 0)

    # ------------------------------------------------------------------
    # IN-TC-19：A 类草稿确认后可发布
    # ------------------------------------------------------------------
    def test_19_class_a_publish_allowed(self):
        """IN-TC-19: A 类草稿：创建 → 确认 → 发布（已确认 → 已发布）。"""
        draft = self._create_draft(op_class="A")
        self.assertEqual(draft["status"], "待复核")

        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft['draft_id']}/confirm",
            headers=self.auth_h, json={"note": "确认"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["status"], "已确认")

        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft['draft_id']}/publish",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        published = r.json()["data"]
        self.assertEqual(published["status"], "已发布")

    # ------------------------------------------------------------------
    # IN-TC-20：B 类草稿未确认 → publish 409
    # ------------------------------------------------------------------
    def test_20_class_b_publish_rejected_unconfirmed(self):
        """IN-TC-20: B 类草稿待复核（未确认）→ publish 409。"""
        draft = self._create_draft(op_class="B")
        self.assertEqual(draft["status"], "待复核")

        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft['draft_id']}/publish",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 409, r.text)

    # ------------------------------------------------------------------
    # IN-TC-22：待复核 / 已驳回 草稿不可发布
    # ------------------------------------------------------------------
    def test_22_unconfirmed_draft_not_publishable(self):
        """IN-TC-22: 待复核和已驳回草稿均不可发布（publish 409 fail-closed）。"""
        # 1) 待复核 → publish 409
        draft1 = self._create_draft(op_class="A")
        self.assertEqual(draft1["status"], "待复核")
        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft1['draft_id']}/publish",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 409, r.text)

        # 2) 驳回 → publish 409
        draft2 = self._create_draft(op_class="A")
        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft2['draft_id']}/reject",
            headers=self.auth_h, json={"note": "驳回"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["status"], "已驳回")

        r = self.client.post(
            f"{API}/cases/c1/etl-drafts/{draft2['draft_id']}/publish",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 409, r.text)


if __name__ == "__main__":
    unittest.main()
