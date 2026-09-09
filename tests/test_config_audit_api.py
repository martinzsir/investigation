"""
tests/test_config_audit_api.py
M6/MVP-4 FE-T-012 后端侧：配置写追加进案件审计链（state.sqlite 哈希链）。

红线断言：
  AC-1 9 个配置写端点（rules/objects/links/policies/views/knowledge POST+PUT、
       data-elements/etl-pipeline）落盘后，GET /cases/{cid}/audit?action=config
       可查到事件（chain_source=state，operator 取会话，note=reason）；
  AC-2 reason 可选：不带 reason 也 200，事件 note 为空串；
  AC-3 data-elements 的 reason 是留痕字段，不落数据文件；
  AC-4 低权限（clearance<2）写配置 403 且不产生 config 事件；
  AC-5 配置事件进链不破坏哈希链：audit/verify chain_ok=True。
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

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory

API = "/api/v1"


class ConfigAuditApiTest(unittest.TestCase):
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

        def mk(operator, pw, role, clearance, tenant):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role,
                                       clearance=clearance, tenant_id=tenant))

        mk("王检察官", "pw-pro", "human", 4, "t1")
        mk("李侦查员", "pw-sol", "正兵", 1, "t1")
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post(f"{API}/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator, password):
        r = self.client.post(f"{API}/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _config_events(self):
        r = self.client.get(f"{API}/cases/c1/audit?action=config&page_size=200",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()["data"]
        return data["items"], data["total"]

    # ---- AC-1：9 个写端点全部进审计链 ----
    def test_config_writes_append_audit_chain(self):
        reason = "FE-T-012 配置变更留痕测试"

        # 1) policies
        r = self.client.get(f"{API}/cases/c1/policies", headers=self.auth_h)
        pol = r.json()["data"]
        r = self.client.put(f"{API}/cases/c1/policies", headers=self.auth_h,
                            json={"object_policies": pol["object_policies"],
                                  "link_policies": pol["link_policies"],
                                  "property_policies": pol["property_policies"],
                                  "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 2) views
        r = self.client.get(f"{API}/cases/c1/views", headers=self.auth_h)
        views = r.json()["data"]["views"]
        r = self.client.put(f"{API}/cases/c1/views", headers=self.auth_h,
                            json={"views": views, "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 3) objects（原样回写）
        r = self.client.get(f"{API}/cases/c1/objects", headers=self.auth_h)
        objects = r.json()["data"]["objects"]
        r = self.client.put(f"{API}/cases/c1/objects", headers=self.auth_h,
                            json={"objects": objects, "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 4) links（原样回写）
        r = self.client.get(f"{API}/cases/c1/links", headers=self.auth_h)
        links = r.json()["data"]["links"]
        r = self.client.put(f"{API}/cases/c1/links", headers=self.auth_h,
                            json={"links": links, "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 5) knowledge POST（追加断言）
        r = self.client.post(f"{API}/cases/c1/knowledge", headers=self.auth_h,
                             json={"relation_assertions": [{
                                 "from": "甲公司", "to": "乙公司",
                                 "type": "关联", "valid_until": None}],
                                 "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 6) knowledge PUT（整体替换）
        r = self.client.put(f"{API}/cases/c1/knowledge", headers=self.auth_h,
                            json={"relation_assertions": [{
                                "from": "甲公司", "to": "丙公司",
                                "type": "关联", "valid_until": None}],
                                "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 7) data-elements（整包回写 + reason）
        r = self.client.get(f"{API}/cases/c1/data-elements",
                            headers=self.auth_h)
        de = r.json()["data"]
        r = self.client.put(f"{API}/cases/c1/data-elements",
                            headers=self.auth_h, json={**de, "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 8) etl-pipeline（sources 原样回写 + reason）
        r = self.client.get(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_h)
        sources = r.json()["data"]["sources"]
        r = self.client.put(f"{API}/cases/c1/etl-pipeline",
                            headers=self.auth_h,
                            json={"sources": sources, "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        # 9) rules（改 rule_text，≥20 字）
        r = self.client.get(f"{API}/cases/c1/rules", headers=self.auth_h)
        rules = r.json()["data"]["rules"]
        self.assertTrue(rules, "快照应至少有一条规则")
        rid = rules[0]["id"]
        r = self.client.put(f"{API}/cases/c1/rules/{rid}",
                            headers=self.auth_h,
                            json={"rule_text": "测试用判据文本：同一主体在短时间"
                                              "窗口内与多名无关人员发生密集资金"
                                              "往来且无合理事由，列为可疑模式。",
                                  "reason": reason})
        self.assertEqual(r.status_code, 200, r.text)

        items, total = self._config_events()
        self.assertGreaterEqual(total, 9, f"9 个写端点应各产 1 条 config 事件，"
                                          f"实际 {total}")
        for it in items:
            self.assertEqual(it["action"], "config")
            self.assertEqual(it["chain_source"], "state")
            self.assertEqual(it["operator"], "王检察官")
        # reason 落 note 列
        self.assertTrue(any(it.get("note") == reason for it in items))
        # config_action 标记落 after_state（timeline 不回传 after_state，
        # 直接查 state.sqlite 核对具体端点可溯源）
        import json as _json
        import sqlite3 as _sqlite3
        db = self.factory.case_dir("c1") / "state.sqlite"
        conn = _sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT after_state FROM audit_chain").fetchall()
        finally:
            conn.close()
        ops = set()
        for (after_json,) in rows:
            after = _json.loads(after_json) if after_json else {}
            if after.get("config_action"):
                ops.add(after["config_action"])
        for op in ("policies_save", "views_save", "model_objects_save",
                   "model_links_save", "knowledge_add", "knowledge_save",
                   "data_elements_edit", "etl_pipeline_edit", "rule_edit"):
            self.assertIn(op, ops, f"审计链 after_state 应含 {op} 事件")

    # ---- AC-2：reason 可选 ----
    def test_reason_optional(self):
        r = self.client.get(f"{API}/cases/c1/policies", headers=self.auth_h)
        pol = r.json()["data"]
        r = self.client.put(f"{API}/cases/c1/policies", headers=self.auth_h,
                            json={"object_policies": pol["object_policies"],
                                  "link_policies": pol["link_policies"],
                                  "property_policies": pol["property_policies"]})
        self.assertEqual(r.status_code, 200, r.text)
        items, total = self._config_events()
        self.assertEqual(total, 1)
        self.assertEqual(items[0].get("note"), "")

    # ---- AC-3：reason 不落数据文件 ----
    def test_reason_not_persisted_in_data_elements(self):
        r = self.client.get(f"{API}/cases/c1/data-elements",
                            headers=self.auth_h)
        de = r.json()["data"]
        r = self.client.put(f"{API}/cases/c1/data-elements",
                            headers=self.auth_h,
                            json={**de, "reason": "调数据元备注"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"{API}/cases/c1/data-elements",
                            headers=self.auth_h)
        self.assertNotIn("reason", r.json()["data"])

    # ---- AC-4：低权限 403 且不留 config 事件 ----
    def test_low_clearance_403_no_audit(self):
        _, before = self._config_events()
        r = self.client.get(f"{API}/cases/c1/policies", headers=self.auth_h)
        pol = r.json()["data"]
        r = self.client.put(f"{API}/cases/c1/policies", headers=self.auth_s,
                            json={"object_policies": pol["object_policies"],
                                  "link_policies": pol["link_policies"],
                                  "property_policies": pol["property_policies"],
                                  "reason": "越权尝试"})
        self.assertEqual(r.status_code, 403, r.text)
        _, after = self._config_events()
        self.assertEqual(before, after, "403 不得产生审计事件")

    # ---- AC-5：配置事件不破坏哈希链 ----
    def test_chain_verify_ok_after_config_writes(self):
        r = self.client.get(f"{API}/cases/c1/views", headers=self.auth_h)
        views = r.json()["data"]["views"]
        r = self.client.put(f"{API}/cases/c1/views", headers=self.auth_h,
                            json={"views": views, "reason": "配置后链校验"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(f"{API}/cases/c1/audit/verify",
                             headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["chain_ok"], r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
