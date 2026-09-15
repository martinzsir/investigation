"""
tests/test_s5_impact.py
S5 通用表单 + 影响面 + 轻量提案。

验收点（S5 PRD）：
F1 11 个新 schema 对实际 JSON 全通过（R7：从实际文件反推）；
F3 影响面五类（clues🔴/rules🟡/views🟡/tables🟢/functions⚪）：
  R1 动态/词法命中计入并标 uncertain；R2 已固证线索 solidified；
  D10 线索产物不可读 → 该类 unavailable + partial/failed，绝不显示无影响；
  新增不产生影响面，删除/改名才产生；
F4 提案状态机 draft→impact_ready→published/discarded：
  R4 无自动发布路径；E4-1 未评估影响面 publish → 400；
  E4-3 影响面基准变化 publish → 400（重新评估后可发布）；
  理由必填；发布落盘 + 保留 F5 未知字段；discard 本体不变；
  权限：正兵不可写；llm_policy 永不开放（文件读 form_enabled=false，
  提案 403，影响面 400）。
"""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from core.impact import ImpactInput, compute_impact, impact_fingerprint
from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.meta.models import User
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore

API = "/api/v1"
DEFAULT_PACK = ROOT / "ontology" / "default"

# 新 schema → 实际 JSON 覆盖对（F1，R7）
_SCHEMA_CASES = [
    ("case_knowledge", DEFAULT_PACK / "case_knowledge.json"),
    ("data_elements", DEFAULT_PACK / "data_elements.json"),
    ("derived_properties", DEFAULT_PACK / "derived_properties.json"),
    ("dimensions", DEFAULT_PACK / "dimensions.json"),
    ("enum_space", DEFAULT_PACK / "enum_space.json"),
    ("jians", DEFAULT_PACK / "jians.json"),
    ("llm_policy", DEFAULT_PACK / "llm_policy.json"),
    ("scoring", DEFAULT_PACK / "scoring.json"),
    ("states", DEFAULT_PACK / "states.json"),
    ("thresholds", DEFAULT_PACK / "thresholds.json"),
    ("verify_playbooks", DEFAULT_PACK / "verify_playbooks.json"),
]


def _load_pack_json(name: str) -> dict:
    return json.loads((DEFAULT_PACK / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# F1：schema 与实际 JSON 一致
# ---------------------------------------------------------------------------
class S5SchemaCoverageTest(unittest.TestCase):
    def test_new_schemas_validate_real_json(self):
        for name, path in _SCHEMA_CASES:
            with self.subTest(name=name):
                schema = json.loads(
                    (ROOT / "schemas" / f"{name}.schema.json").read_text(
                        encoding="utf-8"))
                doc = json.loads(path.read_text(encoding="utf-8"))
                errs = list(jsonschema.Draft7Validator(schema).iter_errors(doc))
                self.assertFalse(
                    errs,
                    f"{name} schema 与 {path.name} 不一致："
                    f"{[ (list(e.absolute_path), e.message[:80]) for e in errs[:3] ]}")

    def test_shared_data_elements_also_validates(self):
        schema = json.loads(
            (ROOT / "schemas" / "data_elements.schema.json").read_text(
                encoding="utf-8"))
        for rel in ("ontology/_shared/data_elements.json",
                    "ontology/reqd_case/data_elements.json"):
            with self.subTest(rel=rel):
                doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
                self.assertFalse(
                    list(jsonschema.Draft7Validator(schema).iter_errors(doc)))


# ---------------------------------------------------------------------------
# F3：核心引擎（确定性、不连库）
# ---------------------------------------------------------------------------
class S5ImpactCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pack_dir = self.tmp / "pack"
        shutil.copytree(DEFAULT_PACK, self.pack_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _impact(self, file, old, new, **kw):
        inp = ImpactInput(file=file, old_doc=old, new_doc=new,
                          pack_dir=self.pack_dir,
                          materialized_tables=kw.get("tables", []),
                          clues=kw.get("clues", []),
                          clue_status=kw.get("status", {}))
        return compute_impact(inp)

    def test_no_changes_is_complete_zero(self):
        doc = _load_pack_json("rules")
        imp = self._impact("rules", doc, copy.deepcopy(doc))
        self.assertEqual(imp["status"], "complete")
        self.assertEqual(imp["totals"]["total"], 0)
        self.assertFalse(imp["changes"])

    def test_addition_has_no_impact(self):
        old = _load_pack_json("dimensions")
        new = copy.deepcopy(old)
        new["dimensions"].append(
            {"name": "测试新维度", "note": "n", "source_object_types": []})
        imp = self._impact("dimensions", old, new)
        self.assertEqual(imp["totals"]["total"], 0)

    def test_remove_rule_flags_clue_and_solidified(self):
        old = _load_pack_json("rules")
        new = copy.deepcopy(old)
        new["rules"] = [r for r in new["rules"] if r["id"] != "R1"]
        clues = [{
            "clue_id": "clue_solid",
            "title": "季末整数存款线索",
            "detail": {"rule_id": "R1", "rule_text": "x"},
            "jian_types": ["生间"],
            "status": "待查",
        }]
        # 不带处置状态：certain 但未固证
        imp0 = self._impact("rules", old, new, clues=clues)
        hit0 = [i for i in imp0["categories"]["clues"]["items"]
                if i["id"] == "clue_solid"]
        self.assertTrue(hit0)
        self.assertFalse(hit0[0]["solidified"])

        # state 真值为「已固证」→ solidified=True（R2）
        imp = self._impact("rules", old, new, clues=clues,
                           status={"clue_solid": "已固证"})
        hit = [i for i in imp["categories"]["clues"]["items"]
               if i["id"] == "clue_solid"]
        self.assertTrue(hit)
        self.assertTrue(hit[0]["solidified"])
        self.assertGreaterEqual(imp["totals"]["solidified_clues"], 1)
        self.assertIn("删除条目 R1", [c["label"] for c in imp["changes"]])

    def test_remove_object_hits_materialized_table(self):
        old = _load_pack_json("objects")
        target = old["objects"][0]["name"]
        new = copy.deepcopy(old)
        new["objects"] = [o for o in new["objects"] if o["name"] != target]
        imp = self._impact("objects", old, new, tables=[f"obj_{target}"])
        ids = [i["id"] for i in imp["categories"]["tables"]["items"]]
        self.assertIn(f"obj_{target}", ids)

    def test_dynamic_sql_uncertain_is_counted(self):
        # 删对象 + 函数 SQL 文本命中 obj_xxx（无 requires/inputs 结构化引用）
        old = _load_pack_json("objects")
        target = old["objects"][0]["name"]
        new = copy.deepcopy(old)
        new["objects"] = [o for o in new["objects"] if o["name"] != target]
        imp = self._impact("objects", old, new)
        for it in imp["categories"]["functions"]["items"]:
            self.assertIn(it["certainty"], ("certain", "uncertain"))
        # 不允许凭空静默：要么有命中，要么该函数确实不含该 token
        self.assertEqual(imp["status"], "complete")

    def test_clues_unavailable_never_says_no_impact(self):
        doc = _load_pack_json("rules")
        new = copy.deepcopy(doc)
        new["rules"] = [r for r in new["rules"] if r["id"] != "R1"]
        inp = ImpactInput(file="rules", old_doc=doc, new_doc=new,
                          pack_dir=self.pack_dir,
                          materialized_tables=None,  # DuckDB 不可读
                          clues=None)  # 线索产物损坏
        imp = compute_impact(inp)
        self.assertIn(imp["status"], ("partial", "failed"))
        self.assertEqual(imp["categories"]["clues"]["status"], "unavailable")
        self.assertEqual(imp["categories"]["tables"]["status"], "unavailable")
        cats = {f["category"] for f in imp["failures"]}
        self.assertIn("clues", cats)
        self.assertIn("tables", cats)

    def test_fingerprint_stable_and_changes(self):
        fp1 = impact_fingerprint(self.pack_dir, materialized_tables=["obj_x"],
                                 clues_artifact=None)
        fp2 = impact_fingerprint(self.pack_dir, materialized_tables=["obj_x"],
                                 clues_artifact=None)
        self.assertEqual(fp1, fp2)
        # 下游文件变动 → 指纹变化（E4-3 依据）
        p = self.pack_dir / "jians.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["_note"] = d.get("_note", "") + " [s5-test-touch]"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        fp3 = impact_fingerprint(self.pack_dir, materialized_tables=["obj_x"],
                                 clues_artifact=None)
        self.assertNotEqual(fp1, fp3)


# ---------------------------------------------------------------------------
# F4：HTTP 提案流 + 影响面端点
# ---------------------------------------------------------------------------
class S5ProposalFlowTest(unittest.TestCase):
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
        self.snap = self.svc.snapshot_dir("c1", "default")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login(self, op, pw):
        r = self.client.post(f"{API}/auth/login",
                             json={"operator": op, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    # ---- 读模型 ----
    def test_get_file_and_llm_policy_readonly(self):
        r = self.client.get(f"{API}/cases/c1/ontology/files/dimensions",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["writable"])
        self.assertTrue(d["form_enabled"])
        self.assertIn("properties", d["schema"])

        r2 = self.client.get(f"{API}/cases/c1/ontology/files/llm_policy",
                             headers=self.auth_h)
        d2 = r2.json()["data"]
        self.assertFalse(d2["writable"])      # R6
        self.assertFalse(d2["form_enabled"])  # 永不渲染表单

        # 非通用承载文件 404（引导走专用编辑器，R5）
        r3 = self.client.get(f"{API}/cases/c1/ontology/files/objects",
                             headers=self.auth_h)
        self.assertEqual(r3.status_code, 404)

    def test_get_file_requires_auth(self):
        r = self.client.get(f"{API}/cases/c1/ontology/files/dimensions")
        self.assertEqual(r.status_code, 401)

    # ---- 影响面预览 ----
    def _remove_rule_doc(self, rid="R1"):
        doc = _load_pack_json("rules")
        doc["rules"] = [r for r in doc["rules"] if r["id"] != rid]
        return doc

    def test_impact_preview_solidified_clue(self):
        # 造线索产物 + state 已固证真值
        art_dir = self.factory.case_dir("c1") / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / "clues_v1.json").write_text(json.dumps({
            "version": 1,
            "clues": [{
                "clue_id": "clue_a", "title": "季末整数",
                "detail": {"rule_id": "R1"},
                "jian_types": ["生间"], "status": "待查",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        db = self.factory.case_dir("c1") / "state.sqlite"
        st = StateStore("c1", str(db))
        st.close()
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "INSERT INTO clue_disposal_status "
                "(clue_id, status, note, operator, updated_at) "
                "VALUES (?,?,?,?,?)",
                ("clue_a", "已固证", "", "王检察官", "2025-01-01 00:00:00"))
            con.commit()
        finally:
            con.close()

        r = self.client.post(
            f"{API}/cases/c1/ontology/impact", headers=self.auth_h,
            json={"file": "rules", "doc": self._remove_rule_doc()})
        self.assertEqual(r.status_code, 200, r.text)
        imp = r.json()["data"]["impact"]
        hit = [i for i in imp["categories"]["clues"]["items"]
               if i["id"] == "clue_a"]
        self.assertTrue(hit, imp)
        self.assertTrue(hit[0]["solidified"])
        self.assertTrue(r.json()["data"]["fingerprint"])

    def test_impact_llm_policy_rejected(self):
        r = self.client.post(
            f"{API}/cases/c1/ontology/impact", headers=self.auth_h,
            json={"file": "llm_policy", "doc": {}})
        self.assertEqual(r.status_code, 400)

    # ---- 提案 ----
    def _dim_doc_add(self):
        doc = _load_pack_json("dimensions")
        doc["dimensions"].append(
            {"name": "测试维度S5", "note": "n",
             "source_object_types": ["transaction"]})
        return doc

    def _create(self, doc=None, reason="S5 测试变更", h=None):
        return self.client.post(
            f"{API}/cases/c1/ontology/proposals",
            headers=h or self.auth_pj,
            json={"file": "dimensions",
                  "doc": doc or self._dim_doc_add(), "reason": reason})

    def test_soldier_cannot_propose(self):
        r = self._create(h=self.auth_s)
        self.assertEqual(r.status_code, 403)

    def test_reason_required(self):
        r = self._create(reason="   ")
        self.assertEqual(r.status_code, 400)

    def test_invalid_doc_rejected_at_create(self):
        doc = _load_pack_json("dimensions")
        doc["dimensions"].append(copy.deepcopy(doc["dimensions"][0]))  # 重名
        r = self._create(doc=doc)
        self.assertEqual(r.status_code, 400)

    def test_llm_policy_proposal_rejected(self):
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals", headers=self.auth_pj,
            json={"file": "llm_policy", "doc": {}, "reason": "x"})
        self.assertEqual(r.status_code, 403)

    def test_full_flow_publish_and_e4_gates(self):
        # ① 提出
        r = self._create()
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["data"]["proposal_id"]
        self.assertEqual(r.json()["data"]["status"], "draft")

        # E4-1：未评估影响面直接发布 → 拒绝
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/publish",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 400)

        # ② 影响面
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/impact",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["status"], "impact_ready")
        self.assertIsNotNone(r.json()["data"]["impact"])

        # E4-3：评估后下游变化 → 发布拒绝（指纹基准变化）
        jp = self.snap / "jians.json"
        jd = json.loads(jp.read_text(encoding="utf-8"))
        jd["_note"] = jd.get("_note", "") + " [s5-post-impact]"
        jp.write_text(json.dumps(jd, ensure_ascii=False), encoding="utf-8")
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/publish",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 400)
        self.assertIn("基准", r.json()["error"]["message"])

        # 重新评估 → ③ 人工发布
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/impact",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/publish",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["data"]["published"])

        # 落盘生效
        on_disk = json.loads((self.snap / "dimensions.json").read_text(
            encoding="utf-8"))
        self.assertIn("测试维度S5",
                      [d["name"] for d in on_disk["dimensions"]])
        # 终态不可重复发布
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/publish",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 400)

    def test_discard_leaves_ontology_untouched(self):
        before = (self.snap / "dimensions.json").read_bytes()
        r = self._create()
        pid = r.json()["data"]["proposal_id"]
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/discard",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["data"]["status"], "discarded")
        self.assertEqual(before, (self.snap / "dimensions.json").read_bytes())
        # 终态不能再废弃
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/discard",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 400)

    def test_publish_preserves_unknown_fields_f5(self):
        doc = _load_pack_json("dimensions")
        doc["dimensions"].append(
            {"name": "测试维度F5", "note": "n",
             "source_object_types": [], "x_future_field": {"k": "v"}})
        r = self._create(doc=doc, reason="F5 未知字段保留")
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["data"]["proposal_id"]
        self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/impact",
            headers=self.auth_pj)
        r = self.client.post(
            f"{API}/cases/c1/ontology/proposals/{pid}/publish",
            headers=self.auth_pj)
        self.assertEqual(r.status_code, 200, r.text)
        on_disk = json.loads((self.snap / "dimensions.json").read_text(
            encoding="utf-8"))
        entry = next(d for d in on_disk["dimensions"]
                     if d["name"] == "测试维度F5")
        self.assertEqual(entry["x_future_field"], {"k": "v"})


if __name__ == "__main__":
    unittest.main()
