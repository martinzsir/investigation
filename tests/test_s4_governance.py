"""
tests/test_s4_governance.py
S4 治理建模：Action Type 编辑器（actions.json）+ 状态机编辑器（states.json）。

验收点（PRD S4_治理建模）：
  - GET /cases/{cid}/actions：动作目录 + 角色/副作用/derive 枚举 + 状态名集，
    每条动作带界面未覆盖键 _unknown_keys（R6）；
  - 🔴 R2：改 terminal / requires_role 必须带非空理由——服务端硬门禁，
    绕过前端同样 400（UC-S4-8/9）；带理由 200 落盘且理由随审计链留痕（UC-S4-10）；
    普通字段变更无需理由（UC-S4-12）；
  - R5：target_status / only_from 悬空引用 400 不落盘（UC-S4-13/14，E2-4）；
    only_from 不可达由 loader 全量校验兜底 400（D1）；
  - GET/PUT /cases/{cid}/states：状态 + 迁移表（map 往返）+ 被动作引用；
    🔴 R3 终态设出边硬阻止（UC-S4-15）；E3-2 删除被引用状态阻止并列动作（UC-S4-16）；
    取消终态标记需非空理由（§8.4）；合法新增状态 + 迁移落盘；
  - R4：llm_policy 无写路由（UC-S4-17）；overview 标 actions/states 可写、
    llm_policy 不可写。
权限：GET 登录即可；PUT 需偏将及以上（require_analyst，案件域）。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
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

# 理由缺失时的哨兵（区分"不传 reason"与显式传 None）
_OMIT = object()


class S4GovernanceTest(unittest.TestCase):
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
        self.repo.set_user_ontology_admin("张偏将", True)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_pj = self._login("张偏将", "pw-pj")
        self.auth_s = self._login("李侦查员", "pw-sol")
        r = self.client.post(f"{API}/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.snap_dir = self.svc.snapshot_dir("c1", "default")
        self.actions_path = self.snap_dir / "actions.json"
        self.states_path = self.snap_dir / "states.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, operator: str, password: str) -> dict[str, str]:
        r = self.client.post(f"{API}/auth/login",
                             json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    # ------------------------------------------------------------------
    # 读模型助手
    # ------------------------------------------------------------------
    def _actions_data(self, h=None) -> dict:
        r = self.client.get(f"{API}/cases/c1/actions", headers=h or self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def _states_data(self, h=None) -> dict:
        r = self.client.get(f"{API}/cases/c1/states", headers=h or self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    @staticmethod
    def _find(items: list[dict], name: str) -> dict:
        return next(x for x in items if x.get("name") == name)

    def _put_actions(self, actions: list[dict], reason=_OMIT, h=None):
        body: dict = {"actions": actions}
        if reason is not _OMIT:
            body["reason"] = reason
        return self.client.put(f"{API}/cases/c1/actions",
                               headers=h or self.auth_h, json=body)

    def _put_states(self, states: list[dict], transitions: dict,
                    reason=_OMIT, h=None):
        body: dict = {"states": states, "transitions": transitions}
        if reason is not _OMIT:
            body["reason"] = reason
        return self.client.put(f"{API}/cases/c1/states",
                               headers=h or self.auth_h, json=body)

    def _audit_text(self, op: str) -> str:
        """读取 state.sqlite 审计链中某配置操作的 after_state 汇总。"""
        p = self.factory.case_dir("c1") / "state.sqlite"
        con = sqlite3.connect(str(p))
        try:
            rows = con.execute(
                "SELECT after_state FROM audit_chain "
                "WHERE after_state LIKE ?", (f"%{op}%",)).fetchall()
        finally:
            con.close()
        return "\n".join(r[0] for r in rows)

    # ------------------------------------------------------------------
    # GET 结构
    # ------------------------------------------------------------------
    def test_01_actions_catalog(self):
        data = self._actions_data()
        names = [a["name"] for a in data["actions"]]
        self.assertEqual(
            names,
            ["verify", "reset", "exclude", "confirm", "file",
             "review_merge", "review_reject", "verify_image"])
        self.assertEqual(data["enums"]["requires_role"], ["any", "human"])
        self.assertEqual(set(data["enums"]["side_effects"]),
                         {"set_clue_status", "create_decision",
                          "merge_entity", "dismiss_review",
                          "create_image_evidence"})
        self.assertEqual(data["enums"]["derive"], ["reverse_reach"])
        self.assertEqual(data["state_names"],
                         ["待查", "查证中", "已排除", "已固证", "已立案"])
        f = self._find(data["actions"], "file")
        self.assertTrue(f["terminal"])
        self.assertEqual(f["requires_role"], "human")
        self.assertEqual(f["target_status"], "已立案")
        self.assertEqual(f["_unknown_keys"], [])  # default 动作无界面未覆盖键

    def test_02_states_catalog_and_references(self):
        data = self._states_data()
        s_names = [s["name"] for s in data["states"]]
        self.assertEqual(s_names,
                         ["待查", "查证中", "已排除", "已固证", "已立案"])
        tr = data["transitions"]
        self.assertEqual(tr["已立案"], [])  # 终态无出边
        self.assertIn("已立案", tr["已固证"])
        refs = data["referenced_by"]
        # 已立案被 file 的 target_status 引用
        self.assertIn("file", refs["已立案"])
        # 查证中被 verify(target) 与 confirm(only_from) 引用，去重
        self.assertIn("verify", refs["查证中"])
        self.assertIn("confirm", refs["查证中"])

    def test_03_get_available_to_all_roles(self):
        # 查看 = 案件可访问：正兵可读
        self.assertEqual(200,
                         self.client.get(f"{API}/cases/c1/actions",
                                         headers=self.auth_s).status_code)
        self.assertEqual(200,
                         self.client.get(f"{API}/cases/c1/states",
                                         headers=self.auth_s).status_code)

    # ------------------------------------------------------------------
    # 写权限
    # ------------------------------------------------------------------
    def test_04_soldier_cannot_write(self):
        a = self._actions_data()["actions"]
        r = self._put_actions(a, reason="x", h=self.auth_s)
        self.assertEqual(r.status_code, 403, r.text)
        s = self._states_data()
        r = self._put_states(s["states"], s["transitions"], reason="x",
                             h=self.auth_s)
        self.assertEqual(r.status_code, 403, r.text)

    # ------------------------------------------------------------------
    # 🔴 R2 危险字段理由门禁（actions）
    # ------------------------------------------------------------------
    def test_05_change_role_without_reason_rejected(self):
        actions = self._actions_data()["actions"]
        rm = self._find(actions, "review_merge")
        self.assertEqual(rm["requires_role"], "human")
        rm["requires_role"] = "any"  # human → any：危险变更
        r = self._put_actions(actions)  # 不传 reason
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("requires_role", r.json()["error"]["message"])
        # 未落盘
        again = self._find(self._actions_data()["actions"], "review_merge")
        self.assertEqual(again["requires_role"], "human")

    def test_06_change_terminal_without_reason_rejected(self):
        actions = self._actions_data()["actions"]
        verify = self._find(actions, "verify")
        verify["terminal"] = True  # false → true：危险变更
        r = self._put_actions(actions)  # 不传 reason
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("terminal", r.json()["error"]["message"])
        again = self._find(self._actions_data()["actions"], "verify")
        self.assertFalse(again["terminal"])

    def test_07_danger_change_with_reason_persisted_and_audited(self):
        actions = self._actions_data()["actions"]
        rm = self._find(actions, "review_merge")
        rm["requires_role"] = "any"
        reason = "S4-审计-放权给机器会话用于测试留痕"
        r = self._put_actions(actions, reason=reason)
        self.assertEqual(r.status_code, 200, r.text)
        danger = r.json()["data"]["danger_changes"]
        self.assertTrue(any(c["field"] == "requires_role"
                            and c["name"] == "review_merge" for c in danger))
        # 落盘生效
        again = self._find(self._actions_data()["actions"], "review_merge")
        self.assertEqual(again["requires_role"], "any")
        # 文件可直接读到
        on_disk = self._find(
            json.loads(self.actions_path.read_text(encoding="utf-8"))["actions"],
            "review_merge")
        self.assertEqual(on_disk["requires_role"], "any")
        self.assertNotIn("_unknown_keys", on_disk)
        # 理由随审计链留痕（UC-S4-10）
        self.assertIn(reason, self._audit_text("actions_save"))

    def test_08_normal_field_change_needs_no_reason(self):
        actions = self._actions_data()["actions"]
        verify = self._find(actions, "verify")
        verify["title"] = "开始查证（S4 改名）"
        r = self._put_actions(actions)  # 无危险、不带理由
        self.assertEqual(r.status_code, 200, r.text)
        again = self._find(self._actions_data()["actions"], "verify")
        self.assertEqual(again["title"], "开始查证（S4 改名）")

    # ------------------------------------------------------------------
    # R5 悬空引用 / 枚举 / loader 兜底
    # ------------------------------------------------------------------
    def test_09_dangling_target_status_rejected(self):
        actions = self._actions_data()["actions"]
        reset = self._find(actions, "reset")
        reset["target_status"] = "已起诉"  # 不存在的状态
        r = self._put_actions(actions, reason="改目标")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("已起诉", r.json()["error"]["message"])
        again = self._find(self._actions_data()["actions"], "reset")
        self.assertEqual(again["target_status"], "待查")

    def test_10_dangling_only_from_rejected(self):
        actions = self._actions_data()["actions"]
        confirm = self._find(actions, "confirm")
        confirm["only_from"] = ["查证中", "已起诉"]  # 悬空前置
        r = self._put_actions(actions, reason="改前置")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("已起诉", r.json()["error"]["message"])

    def test_11_unreachable_only_from_rejected_by_loader(self):
        # confirm 目标=已固证；已立案终态到不了已固证 → loader D1 兜底 400
        actions = self._actions_data()["actions"]
        confirm = self._find(actions, "confirm")
        confirm["only_from"] = ["已立案"]
        r = self._put_actions(actions, reason="收紧前置")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("actions.json 校验失败", r.json()["error"]["message"])

    def test_12_bad_side_effect_rejected_not_persisted(self):
        actions = self._actions_data()["actions"]
        verify = self._find(actions, "verify")
        verify["side_effects"] = ["set_clue_status", "hack_the_planet"]
        r = self._put_actions(actions)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("hack_the_planet", r.json()["error"]["message"])

    def test_13_unknown_keys_preserved_roundtrip(self):
        # R6：界面未覆盖键原样保留并在 GET 提示
        actions = self._actions_data()["actions"]
        verify = self._find(actions, "verify")
        verify["custom_ticket"] = "OM-S4-1"
        r = self._put_actions(actions)  # 未知键不触发危险门禁
        self.assertEqual(r.status_code, 200, r.text)
        again = self._find(self._actions_data()["actions"], "verify")
        self.assertEqual(again.get("custom_ticket"), "OM-S4-1")
        self.assertIn("custom_ticket", again["_unknown_keys"])
        # 顶层 schema_version 保留
        self.assertEqual(
            json.loads(self.actions_path.read_text(encoding="utf-8")
                       ).get("schema_version"), 2)

    # ------------------------------------------------------------------
    # 🔴 R3 终态保护 / E3-2 引用删除 / 取消终态（states）
    # ------------------------------------------------------------------
    def test_14_terminal_outflow_blocked(self):
        s = self._states_data()
        transitions = dict(s["transitions"])
        transitions["已立案"] = ["待查"]  # 给终态加出边
        r = self._put_states(s["states"], transitions)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("终态", r.json()["error"]["message"])
        # 未落盘
        again = self._states_data()
        self.assertEqual(again["transitions"]["已立案"], [])

    def test_15_delete_referenced_state_blocked(self):
        s = self._states_data()
        keep = [x for x in s["states"] if x["name"] != "已立案"]
        keep_names = {x["name"] for x in keep}
        transitions = {
            frm: [t for t in tos if t in keep_names]
            for frm, tos in s["transitions"].items() if frm in keep_names
        }
        r = self._put_states(keep, transitions)
        self.assertEqual(r.status_code, 400, r.text)
        msg = r.json()["error"]["message"]
        self.assertIn("已立案", msg)
        self.assertIn("file", msg)  # 列出引用动作

    def test_16_unterminal_requires_reason(self):
        s = self._states_data()
        states = [dict(x) for x in s["states"]]
        filed = next(x for x in states if x["name"] == "已立案")
        filed["terminal"] = False  # 取消终态：危险
        r = self._put_states(states, s["transitions"])  # 无理由
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("终态", r.json()["error"]["message"])

    def test_17_valid_add_state_and_edge_roundtrip(self):
        s = self._states_data()
        states = [dict(x) for x in s["states"]]
        states.append({
            "name": "复查中", "label": "复查中", "tone": "info",
            "terminal": False, "requires_role": "any",
            "requires_basis": False, "sla_days": 2,
        })
        transitions = {k: list(v) for k, v in s["transitions"].items()}
        transitions["已固证"] = transitions["已固证"] + ["复查中"]
        transitions["复查中"] = []
        r = self._put_states(states, transitions)  # 无危险变更
        self.assertEqual(r.status_code, 200, r.text)
        again = self._states_data()
        self.assertIn("复查中", [x["name"] for x in again["states"]])
        self.assertIn("复查中", again["transitions"]["已固证"])
        self.assertEqual(again["transitions"]["已立案"], [])  # 终态仍锁定
        # 落盘为声明式列表 [{from,to}]
        raw = json.loads(self.states_path.read_text(encoding="utf-8"))
        self.assertIsInstance(raw["transitions"], list)
        edge = next(t for t in raw["transitions"] if t["from"] == "已固证")
        self.assertIn("复查中", edge["to"])

    # ------------------------------------------------------------------
    # R4 llm_policy 永不开放 + overview 可写映射
    # ------------------------------------------------------------------
    def test_18_llm_policy_has_no_write_route(self):
        r = self.client.put(f"{API}/cases/c1/llm_policy",
                            headers=self.auth_h, json={"llm_enabled": True})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertNotEqual(r.status_code, 200)

    def test_19_overview_marks_actions_states_writable(self):
        r = self.client.get(f"{API}/cases/c1/ontology/overview",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        files = {f["name"]: f for f in r.json()["data"]["files"]}
        self.assertTrue(files["actions"]["writable"])
        self.assertTrue(files["states"]["writable"])
        # llm_policy 若出现在总览，必须只读（R4）
        if "llm_policy" in files:
            self.assertFalse(files["llm_policy"]["writable"])


if __name__ == "__main__":
    unittest.main()
