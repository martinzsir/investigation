"""
tests/test_canvas_api.py
RC-101/205 线索研判画布 API 端到端（FastAPI TestClient）。

AC 对应：
  AC-101-1 首次 GET 惰性 seed：seeded=true、系统节点/边落 state.sqlite；
  AC-101-2 再次 GET seeded=false 且文档与首次一致（幂等，不重 seed）；
  RC-205   PATCH 坐标/钉住成功 version+1；基准过期 → 409 CONFLICT；
  RC-205   M1 白名单：改系统节点 ref/label → 400 VALIDATION；
  RC-207   无版本语义层 → semantic_ready=false（降级横幅信号）；
  权限     未登录 401；跨租户 404；PATCH 未初始化画布 404；不存在线索 404。
另含：自动保存写 audit_chain（canvas.create / canvas.autosave 可验签）。
"""
from __future__ import annotations

import copy
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from core.registry import LineageClue

from server.app.cases import CaseService
from server.app.clues_artifact import save_case_clues
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED, User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore
from server.app.worker.pool import WorkerPool

CANVAS = "/api/v1/cases/c1/clues/clue-1/canvas"


def _clue() -> LineageClue:
    """带规则与两行溯源的虚构线索（skill xu_shi / 生间）。"""
    return LineageClue(
        clue_id="clue-1",
        skill_id="xu_shi",
        title="张卫国整数现金存入",
        detail={
            "rules": [{
                "rule_id": "R1",
                "依据": "整数现金存入",
                "rule_text": "单笔现金存入金额为整数万元，疑似结构化存款",
            }],
        },
        jian_types=["生间"],
        source_rows=[
            {"from_raw": "张卫国", "to_raw": "海州建材有限公司",
             "amount": 50000.0},
            {"from_raw": "张卫国", "to_raw": "海州建材有限公司",
             "amount": 20000.0},
        ],
    )


class CanvasApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.client = TestClient(create_app(self.ctx))
        for operator, role, pw, tenant in (
            ("王检察官", "human", "pw-pro", "t1"),
            ("李侦查员", "正兵", "pw-sol", "t1"),
            ("李检", "正兵", "pw-li", "t2"),
        ):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role, clearance=4,
                                       tenant_id=tenant))
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_s = self._login("李侦查员", "pw-sol")
        self.auth_l = self._login("李检", "pw-li")
        self.make_case()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post(
            "/api/v1/auth/login",
            json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def make_case(self) -> None:
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version("c1", 1, "test")
        save_case_clues(self.factory.case_dir("c1"), 1, [_clue()])

    def _state(self) -> StateStore:
        return StateStore("c1", self.factory.case_dir("c1") / "state.sqlite")

    def _get(self, headers: dict | None = None, clue_id: str = "clue-1"):
        return self.client.get(
            f"/api/v1/cases/c1/clues/{clue_id}/canvas",
            headers=headers or self.auth_h)

    def _patch(self, doc: dict, version: int | None,
               headers: dict | None = None, clue_id: str = "clue-1"):
        return self.client.patch(
            f"/api/v1/cases/c1/clues/{clue_id}/canvas",
            headers=headers or self.auth_h,
            json={"doc": doc, "version": version})

    # ------------------------------------------------------------------
    # RC-101：惰性 seed + 幂等
    # ------------------------------------------------------------------
    def test_first_get_seeds_and_persists(self):
        r = self._get()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["seeded"])
        self.assertEqual(1, d["version"])
        self.assertEqual("clue-1", d["clue_id"])
        self.assertFalse(d["semantic_ready"])  # 无版本库/语义层 → 降级信号
        # 节点：规则 1 + 事实（每溯源行）+ 数据行 2
        kinds = {}
        for n in d["doc"]["nodes"]:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
        self.assertEqual(1, kinds.get("rule"))
        self.assertEqual(2, kinds.get("fact"))
        self.assertEqual(2, kinds.get("source_row"))
        # 系统边：命中×2、来源行×2
        rels = {}
        for e in d["doc"]["edges"]:
            rels[e["rel"]] = rels.get(e["rel"], 0) + 1
        self.assertEqual(2, rels.get("命中"))
        self.assertEqual(2, rels.get("来源行"))
        # seed 落 state.sqlite（再次打开能读到）
        st = self._state()
        try:
            row = st.get_canvas("clue-1")
        finally:
            st.close()
        self.assertIsNotNone(row)
        self.assertEqual(1, row["version"])
        self.assertEqual("王检察官", row["created_by"])
        self.assertEqual(len(row["doc"]["nodes"]), len(d["doc"]["nodes"]))

    def test_second_get_idempotent(self):
        d1 = self._get().json()["data"]
        d2 = self._get().json()["data"]
        self.assertTrue(d1["seeded"])
        self.assertFalse(d2["seeded"])
        # 文档逐字节一致（坐标不重算、不重建）
        self.assertEqual(d1["doc"], d2["doc"])
        self.assertEqual(d1["version"], d2["version"])
        self.assertEqual(d1["canvas_id"], d2["canvas_id"])

    # ------------------------------------------------------------------
    # RC-205：PATCH 白名单 + 乐观锁
    # ------------------------------------------------------------------
    def test_patch_position_ok_and_version_increments(self):
        base = self._get().json()["data"]
        doc = copy.deepcopy(base["doc"])
        doc["nodes"][0]["x"] = 123.5
        doc["nodes"][0]["y"] = 456
        doc["nodes"][1]["pinned"] = True

        r = self._patch(doc, base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(2, d["version"])
        self.assertEqual(123.5, d["doc"]["nodes"][0]["x"])
        self.assertTrue(d["doc"]["nodes"][1]["pinned"])
        # 未动节点保持原样
        n2 = next(n for n in d["doc"]["nodes"]
                  if n["id"] == base["doc"]["nodes"][2]["id"])
        b2 = next(n for n in base["doc"]["nodes"]
                  if n["id"] == n2["id"])
        self.assertEqual(b2, n2)
        self.assertEqual("王检察官", d["updated_by"])

    def test_patch_tamper_ref_rejected(self):
        base = self._get().json()["data"]
        bad = copy.deepcopy(base["doc"])
        bad["nodes"][0]["ref"] = "RX"
        r = self._patch(bad, base["version"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual("VALIDATION", r.json()["error"]["code"])

    def test_patch_tamper_label_rejected(self):
        base = self._get().json()["data"]
        bad = copy.deepcopy(base["doc"])
        bad["nodes"][0]["label"] = "被篡改的规则名"
        r = self._patch(bad, base["version"])
        self.assertEqual(r.status_code, 400)

    def test_patch_edge_tamper_rejected(self):
        base = self._get().json()["data"]
        bad = copy.deepcopy(base["doc"])
        bad["edges"][0]["rel"] = "推断为"
        r = self._patch(bad, base["version"])
        self.assertEqual(r.status_code, 400)

    def test_patch_add_node_rejected(self):
        base = self._get().json()["data"]
        bad = copy.deepcopy(base["doc"])
        bad["nodes"].append({
            "id": "cn_1", "kind": "hypothesis", "ref": "cn_1",
            "label": "人工假设", "system": False, "pinned": False,
            "x": 0, "y": 0,
        })
        r = self._patch(bad, base["version"])
        self.assertEqual(r.status_code, 400)

    def test_patch_stale_base_version_conflict(self):
        base = self._get().json()["data"]
        doc = copy.deepcopy(base["doc"])
        doc["nodes"][0]["x"] = 10
        # 第一次保存：v1 → v2
        r1 = self._patch(doc, 1)
        self.assertEqual(r1.status_code, 200, r1.text)
        # 再拿过期基准 v1 保存 → 409
        doc2 = copy.deepcopy(base["doc"])
        doc2["nodes"][0]["x"] = 20
        r2 = self._patch(doc2, 1)
        self.assertEqual(r2.status_code, 409)
        self.assertEqual("CONFLICT", r2.json()["error"]["code"])
        self.assertIn("v1", r2.json()["error"]["message"])
        self.assertIn("v2", r2.json()["error"]["message"])
        # 以 v2 为基准可正常保存（后写覆盖由前端确认后重提）
        r3 = self._patch(doc2, 2)
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(3, r3.json()["data"]["version"])

    def test_patch_without_base_version_allowed(self):
        """version 缺省 = 不做乐观锁（M1 前端总会带，但后端容错）。"""
        base = self._get().json()["data"]
        doc = copy.deepcopy(base["doc"])
        doc["nodes"][0]["x"] = 1
        r = self._patch(doc, None)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(2, r.json()["data"]["version"])

    # ------------------------------------------------------------------
    # 权限与边界
    # ------------------------------------------------------------------
    def test_patch_before_get_404(self):
        # 新线索：只造产物不 GET，PATCH 必须 404（不存在自由创建路径）
        from core.registry import LineageClue as LC
        save_case_clues(
            self.factory.case_dir("c1"), 1,
            [_clue(), LC(clue_id="clue-2", skill_id="xu_shi",
                         title="另一条", jian_types=["生间"],
                         source_rows=[])])
        r = self.client.patch(
            "/api/v1/cases/c1/clues/clue-2/canvas",
            headers=self.auth_h,
            json={"doc": {"nodes": [], "edges": []}, "version": None})
        self.assertEqual(r.status_code, 404)

    def test_unknown_clue_get_404(self):
        r = self._get(clue_id="clue-ghost")
        self.assertEqual(r.status_code, 404)

    def test_no_token_401(self):
        self.assertEqual(self.client.get(CANVAS).status_code, 401)
        self.assertEqual(self.client.patch(
            CANVAS, json={"doc": {"nodes": [], "edges": []},
                          "version": None}).status_code, 401)

    def test_cross_tenant_404(self):
        self.assertEqual(self._get(headers=self.auth_l).status_code, 404)
        base = self._get().json()["data"]
        self.assertEqual(
            self._patch(base["doc"], base["version"],
                        headers=self.auth_l).status_code, 404)

    def test_soldier_can_read_and_save(self):
        """正兵同租户可读可写（画布是研判工作台，非 human 专属面）。"""
        r = self._get(headers=self.auth_s)
        self.assertEqual(r.status_code, 200, r.text)
        base = r.json()["data"]
        doc = copy.deepcopy(base["doc"])
        doc["nodes"][0]["y"] = 77
        r2 = self._patch(doc, base["version"], headers=self.auth_s)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual("李侦查员", r2.json()["data"]["updated_by"])

    # ------------------------------------------------------------------
    # RC-401：写审计链
    # ------------------------------------------------------------------
    def test_audit_chain_records_create_and_autosave(self):
        base = self._get().json()["data"]
        doc = copy.deepcopy(base["doc"])
        doc["nodes"][0]["x"] = 8
        self.assertEqual(self._patch(doc, 1).status_code, 200)
        st = self._state()
        try:
            self.assertTrue(st.chain_verify())
            rows = st.conn.execute(
                "SELECT json_extract(after_state,'$.action') AS action "
                "FROM audit_chain WHERE "
                "json_extract(after_state,'$.action') LIKE 'canvas.%' "
                "ORDER BY seq").fetchall()
            actions = [r["action"] for r in rows]
            self.assertEqual(actions, ["canvas.create", "canvas.autosave"])
        finally:
            st.close()


    # ------------------------------------------------------------------
    # RC-103/104：expand 端点
    # ------------------------------------------------------------------
    EXPAND = "/api/v1/cases/c1/clues/clue-1/canvas/expand"

    def _register_bank_source(self) -> None:
        self.repo.register_source(
            case_id="c1", upload_id="up-bank", filename="银行流水.xlsx",
            fmt="xlsx", fingerprint="fp-1", rows=128,
            columns={"from_raw": "流出方"}, created_by="李侦查员")
        self.repo.mark_source_imported(
            "c1", "up-bank", table_name="银行流水",
            parquet_path="data/c1/银行流水.parquet", mapping={})

    def _expand(self, node_id: str, *, version=None, direction="all",
                headers=None):
        return self.client.post(
            self.EXPAND, headers=headers or self.auth_h,
            json={"node_id": node_id, "direction": direction,
                  "version": version})

    def test_expand_before_get_404(self):
        r = self._expand("source_row:x")
        self.assertEqual(r.status_code, 404)

    def test_expand_unsupported_kind_400(self):
        base = self._get().json()["data"]
        rule_id = next(n["id"] for n in base["doc"]["nodes"]
                       if n["kind"] == "rule")
        r = self._expand(rule_id, version=base["version"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual("VALIDATION", r.json()["error"]["code"])

    def test_expand_bad_direction_400(self):
        base = self._get().json()["data"]
        nid = next(n["id"] for n in base["doc"]["nodes"]
                   if n["kind"] == "source_row")
        r = self._expand(nid, version=base["version"], direction="upstream")
        self.assertEqual(r.status_code, 400)

    def test_expand_unknown_node_404(self):
        base = self._get().json()["data"]
        r = self._expand("source_row:ghost", version=base["version"])
        self.assertEqual(r.status_code, 404)

    def test_expand_stale_version_conflict(self):
        base = self._get().json()["data"]
        nid = next(n["id"] for n in base["doc"]["nodes"]
                   if n["kind"] == "source_row")
        r = self._expand(nid, version=999)
        self.assertEqual(r.status_code, 409)
        self.assertEqual("CONFLICT", r.json()["error"]["code"])

    def test_expand_row_to_file_and_idempotent(self):
        """无版本语义库降级：row→file 仍可用；再展一次无新增不涨版本。"""
        self._register_bank_source()
        base = self._get().json()["data"]
        nid = next(n["id"] for n in base["doc"]["nodes"]
                   if n["kind"] == "source_row")

        r = self._expand(nid, version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(base["version"] + 1, d["version"])
        file_ids = [i for i in d["added_nodes"]
                    if i.startswith("source_file:")]
        self.assertEqual(len(file_ids), 1)
        file_node = next(n for n in d["doc"]["nodes"]
                         if n["id"] == file_ids[0])
        self.assertEqual(file_node["label"], "银行流水.xlsx")
        self.assertTrue(file_node["props"]["registered"])
        card = d["details"][file_ids[0]]["file"]
        self.assertEqual(card["rows"], 128)
        self.assertEqual(card["uploaded_by"], "李侦查员")
        # 行字段负载（RC-104：system/human 旁路，全 visible）
        row_detail = d["details"][nid]
        self.assertTrue(row_detail["fields"])
        self.assertTrue(all(f["policy"] == "visible"
                            for f in row_detail["fields"]))
        # 幂等：第二次展开同节点无新增、版本不变
        r2 = self._expand(nid, version=d["version"])
        self.assertEqual(r2.status_code, 200, r2.text)
        d2 = r2.json()["data"]
        self.assertEqual(d2["added_nodes"], [])
        self.assertEqual(d2["added_edges"], [])
        self.assertEqual(d2["version"], d["version"])

    def test_expand_cross_tenant_404(self):
        base = self._get().json()["data"]
        nid = next(n["id"] for n in base["doc"]["nodes"]
                   if n["kind"] == "source_row")
        self.assertEqual(
            self._expand(nid, version=base["version"],
                         headers=self.auth_l).status_code, 404)

    # ------------------------------------------------------------------
    # RC-102：规则审计视图端点
    # ------------------------------------------------------------------
    def test_rule_audit_returns_machine_fields(self):
        self._get()
        r = self.client.get(
            "/api/v1/cases/c1/canvas/rules/R1/audit",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()["data"]
        self.assertEqual(a["rule_id"], "R1")
        self.assertTrue(a["function"])
        self.assertIsInstance(a["params"], dict)
        self.assertEqual(a["rule_workshop_href"], "/c/rules")
        # 自然语言判据随审计视图可审计
        self.assertTrue(a["rule_text"])

    def test_rule_audit_unknown_404(self):
        self._get()
        r = self.client.get(
            "/api/v1/cases/c1/canvas/rules/RX-GHOST/audit",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 404)

    def test_rule_audit_cross_tenant_404(self):
        self._get()
        self.assertEqual(self.client.get(
            "/api/v1/cases/c1/canvas/rules/R1/audit",
            headers=self.auth_l).status_code, 404)

    # ------------------------------------------------------------------
    # M3 RC-202：人工节点增删改
    # ------------------------------------------------------------------
    NODES = "/api/v1/cases/c1/clues/clue-1/canvas/nodes"

    def _create_node(self, kind="hypothesis", props=None, *,
                     version=None, headers=None, x=100.0, y=200.0):
        props = props or {"title": "体外循环假设", "content": "多账户过渡"}
        if kind == "note":
            props = {"content": "备注内容"}
        return self.client.post(
            self.NODES, headers=headers or self.auth_h,
            json={"kind": kind, "props": props, "x": x, "y": y,
                  "version": version})

    def test_create_hypothesis_ok(self):
        base = self._get().json()["data"]
        r = self._create_node(version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(base["version"] + 1, d["version"])
        node = d["node"]
        self.assertTrue(node["id"].startswith("cn_"))
        self.assertEqual("hypothesis", node["kind"])
        self.assertEqual(node["id"], node["ref"])
        self.assertFalse(node["system"])
        self.assertEqual("体外循环假设", node["label"])
        self.assertEqual("多账户过渡", node["props"]["content"])
        self.assertEqual(100.0, node["x"])
        self.assertEqual("王检察官", node["created_by"])
        # 回读一致
        got = self._get().json()["data"]
        self.assertIn(node["id"], [n["id"] for n in got["doc"]["nodes"]])

    def test_create_note_ok(self):
        base = self._get().json()["data"]
        r = self._create_node(kind="note", version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual("note", r.json()["data"]["node"]["kind"])

    def test_create_node_validation_400(self):
        base = self._get().json()["data"]
        # 空白标题
        r = self._create_node(
            props={"title": "  ", "content": "内容"},
            version=base["version"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("假设标题", r.json()["error"]["message"])
        # 超长内容
        r = self._create_node(
            props={"title": "标题", "content": "长" * 501},
            version=base["version"])
        self.assertEqual(r.status_code, 400)
        # 非人工类型
        r = self._create_node(kind="fact", version=base["version"])
        self.assertEqual(r.status_code, 400)

    def test_create_node_stale_version_409(self):
        self._get()
        r = self._create_node(version=999)
        self.assertEqual(r.status_code, 409)
        self.assertEqual("CONFLICT", r.json()["error"]["code"])

    def test_update_manual_node_ok_and_system_locked(self):
        base = self._get().json()["data"]
        nid = self._create_node(
            version=base["version"]).json()["data"]["node"]["id"]

        r = self.client.patch(
            f"{self.NODES}/{nid}", headers=self.auth_h,
            json={"props": {"title": "新标题", "content": "新内容"},
                  "version": 2})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(3, d["version"])
        self.assertEqual("新标题", d["node"]["label"])

        # 系统节点编辑 400
        rule_id = next(n["id"] for n in d["doc"]["nodes"]
                       if n["kind"] == "rule")
        r = self.client.patch(
            f"{self.NODES}/{rule_id}", headers=self.auth_h,
            json={"props": {"content": "x"}, "version": 3})
        self.assertEqual(r.status_code, 400)
        # 不存在节点 400
        r = self.client.patch(
            f"{self.NODES}/cn_ghost", headers=self.auth_h,
            json={"props": {"content": "x"}, "version": 3})
        self.assertEqual(r.status_code, 400)

    def test_delete_node_cascades_manual_edges(self):
        base = self._get().json()["data"]
        created = self._create_node(version=1).json()["data"]
        h_id = created["node"]["id"]
        fact_id = next(n["id"] for n in created["doc"]["nodes"]
                       if n["kind"] == "fact")
        # fact -[推断为]-> 假设
        r = self.client.post(
            "/api/v1/cases/c1/clues/clue-1/canvas/edges",
            headers=self.auth_h,
            json={"source": fact_id, "target": h_id, "rel": "推断为",
                  "version": 2})
        self.assertEqual(r.status_code, 200, r.text)
        e_id = r.json()["data"]["edge"]["id"]
        sys_before = [e["id"] for e in r.json()["data"]["doc"]["edges"]
                      if e["system"]]

        # 删除节点 → 级联人工边
        r = self.client.delete(
            f"{self.NODES}/{h_id}?version=3", headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual([e_id], d["removed_edges"])
        ids_nodes = {n["id"] for n in d["doc"]["nodes"]}
        ids_edges = {e["id"] for e in d["doc"]["edges"]}
        self.assertNotIn(h_id, ids_nodes)
        self.assertNotIn(e_id, ids_edges)
        # 系统边一条不少
        self.assertEqual(
            sys_before,
            [e["id"] for e in d["doc"]["edges"] if e["system"]])

    def test_delete_system_node_400(self):
        base = self._get().json()["data"]
        rule_id = next(n["id"] for n in base["doc"]["nodes"]
                       if n["kind"] == "rule")
        r = self.client.delete(
            f"{self.NODES}/{rule_id}?version=1", headers=self.auth_h)
        self.assertEqual(r.status_code, 400)

    # ------------------------------------------------------------------
    # M3 RC-203：人工连线与系统边锁定
    # ------------------------------------------------------------------
    EDGES = "/api/v1/cases/c1/clues/clue-1/canvas/edges"

    def _doc_with_hypothesis(self):
        base = self._get().json()["data"]
        d = self._create_node(version=1).json()["data"]
        return d["doc"], d["version"]

    def test_create_edge_ok(self):
        doc, ver = self._doc_with_hypothesis()
        h_id = next(n["id"] for n in doc["nodes"]
                    if n["kind"] == "hypothesis")
        fact_id = next(n["id"] for n in doc["nodes"]
                       if n["kind"] == "fact")
        r = self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": h_id, "rel": "推断为",
                  "note": " 依据不足 ", "version": ver})
        self.assertEqual(r.status_code, 200, r.text)
        edge = r.json()["data"]["edge"]
        self.assertEqual("e:%s--推断为--%s" % (fact_id, h_id), edge["id"])
        self.assertFalse(edge["system"])
        self.assertEqual("依据不足", edge["note"])

    def test_create_edge_illegal_and_duplicate(self):
        doc, ver = self._doc_with_hypothesis()
        h_id = next(n["id"] for n in doc["nodes"]
                    if n["kind"] == "hypothesis")
        fact_id = next(n["id"] for n in doc["nodes"]
                       if n["kind"] == "fact")
        row_id = next(n["id"] for n in doc["nodes"]
                      if n["kind"] == "source_row")
        body = {"source": fact_id, "target": h_id, "rel": "推断为",
                "version": ver}
        r = self.client.post(self.EDGES, headers=self.auth_h, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        # 重复
        r = self.client.post(self.EDGES, headers=self.auth_h, json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("已存在", r.json()["error"]["message"])
        # 矩阵非法（fact 不能 证实 hypothesis）
        r = self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": h_id, "rel": "证实",
                  "version": ver})
        self.assertEqual(r.status_code, 400)
        # 禁入目标（数据行）
        r = self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": row_id, "rel": "推断为",
                  "version": ver})
        self.assertEqual(r.status_code, 400)
        # 自连
        r = self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": fact_id, "rel": "推断为",
                  "version": ver})
        self.assertEqual(r.status_code, 400)

    def test_create_edge_note_too_long_400(self):
        doc, ver = self._doc_with_hypothesis()
        h_id = next(n["id"] for n in doc["nodes"]
                    if n["kind"] == "hypothesis")
        fact_id = next(n["id"] for n in doc["nodes"]
                       if n["kind"] == "fact")
        r = self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": h_id, "rel": "推断为",
                  "note": "x" * 201, "version": ver})
        self.assertEqual(r.status_code, 400)

    def test_delete_manual_edge_ok_system_locked(self):
        doc, ver = self._doc_with_hypothesis()
        h_id = next(n["id"] for n in doc["nodes"]
                    if n["kind"] == "hypothesis")
        fact_id = next(n["id"] for n in doc["nodes"]
                       if n["kind"] == "fact")
        r = self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": h_id, "rel": "推断为",
                  "version": ver})
        e_id = r.json()["data"]["edge"]["id"]
        new_ver = r.json()["data"]["version"]

        # 系统边删除 400
        sys_eid = next(e["id"] for e in r.json()["data"]["doc"]["edges"]
                       if e["system"])
        r = self.client.delete(
            f"{self.EDGES}/{sys_eid}?version={new_ver}",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("系统推断关系", r.json()["error"]["message"])

        # 人工边删除 200
        r = self.client.delete(
            f"{self.EDGES}/{e_id}?version={new_ver}",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(e_id,
                         {e["id"] for e in r.json()["data"]["doc"]["edges"]})

    # ------------------------------------------------------------------
    # M3 RC-206：快照与回滚
    # ------------------------------------------------------------------
    SNAPS = "/api/v1/cases/c1/clues/clue-1/canvas/snapshots"

    def _snapshot(self, label="阶段一", *, version=1, headers=None):
        return self.client.post(
            self.SNAPS, headers=headers or self.auth_h,
            json={"label": label, "version": version})

    def test_snapshot_create_requires_label_and_keeps_version(self):
        base = self._get().json()["data"]
        r = self._snapshot(label="   ", version=base["version"])
        self.assertEqual(r.status_code, 400)
        r = self._snapshot(label="x" * 101, version=base["version"])
        self.assertEqual(r.status_code, 400)

        r = self._snapshot(label=" 初版 ", version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["snapshot"]["snapshot_id"].startswith("snap_"))
        self.assertEqual("初版", d["snapshot"]["label"])
        # 快照不推进画布 version
        self.assertEqual(base["version"], d["version"])
        # 列表可读
        r = self.client.get(self.SNAPS, headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["snapshots"]
        self.assertEqual(1, len(items))
        self.assertEqual(base["doc"], items[0]["doc"])
        self.assertEqual(len(base["doc"]["nodes"]), items[0]["node_count"])

    def test_rollback_a_b_a_roundtrip(self):
        """AC-206-2：连续回滚 A→B→A，原状态可恢复，快照不可变。"""
        base = self._get().json()["data"]
        self.assertEqual(1, base["version"])

        # 快照 A（种子态）
        r = self._snapshot(label="A", version=1)
        snap_a = r.json()["data"]["snapshot"]["snapshot_id"]
        doc_a = r.json()["data"]["snapshot"]["doc"]

        # 新增假设节点 → v2，再快照 B
        created = self._create_node(version=1).json()["data"]
        h_id = created["node"]["id"]
        self.assertEqual(2, created["version"])
        r = self._snapshot(label="B", version=2)
        snap_b = r.json()["data"]["snapshot"]["snapshot_id"]
        doc_b = r.json()["data"]["snapshot"]["doc"]
        self.assertIn(h_id, [n["id"] for n in doc_b["nodes"]])

        # 回滚 A → v3，文档与 A 一致
        r = self.client.post(
            f"{self.SNAPS}/{snap_a}/rollback", headers=self.auth_h,
            json={"version": 2})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(3, d["version"])
        self.assertEqual(doc_a, d["doc"])
        recovery_id = d["recovery_snapshot_id"]
        self.assertTrue(recovery_id.startswith("snap_"))
        self.assertIn("回滚前自动恢复点",
                      d["recovery_snapshot"]["label"])
        self.assertEqual([], d["stale_node_ids"])

        # 快照 B 内容不可变（回滚 A 后仍是含假设节点的版本）
        r = self.client.get(self.SNAPS, headers=self.auth_h)
        by_id = {s["snapshot_id"]: s for s in r.json()["data"]["snapshots"]}
        self.assertIn(h_id,
                      [n["id"] for n in by_id[snap_b]["doc"]["nodes"]])
        self.assertIn(recovery_id, by_id)

        # 再回滚 B（基准 v3）→ 假设节点回来
        r = self.client.post(
            f"{self.SNAPS}/{snap_b}/rollback", headers=self.auth_h,
            json={"version": 3})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(4, d["version"])
        self.assertIn(h_id, [n["id"] for n in d["doc"]["nodes"]])

    def test_rollback_unknown_snapshot_404(self):
        self._get()
        r = self.client.post(
            f"{self.SNAPS}/snap_ghost/rollback", headers=self.auth_h,
            json={"version": 1})
        self.assertEqual(r.status_code, 404)

    def test_snapshot_rollback_stale_version_409(self):
        r = self._get()
        sid = self._snapshot(version=1).json()["data"]["snapshot"][
            "snapshot_id"]
        r = self.client.post(
            f"{self.SNAPS}/{sid}/rollback", headers=self.auth_h,
            json={"version": 999})
        self.assertEqual(r.status_code, 409)

    # ------------------------------------------------------------------
    # M3 RC-401：人工动作写审计链
    # ------------------------------------------------------------------
    def test_audit_records_manual_actions(self):
        base = self._get().json()["data"]
        created = self._create_node(version=1).json()["data"]
        h_id = created["node"]["id"]
        fact_id = next(n["id"] for n in created["doc"]["nodes"]
                       if n["kind"] == "fact")
        self.client.post(
            self.EDGES, headers=self.auth_h,
            json={"source": fact_id, "target": h_id, "rel": "推断为",
                  "version": 2})
        self._snapshot(label="存证", version=3)

        st = self._state()
        try:
            self.assertTrue(st.chain_verify())
            rows = st.conn.execute(
                "SELECT json_extract(after_state,'$.action') AS action "
                "FROM audit_chain WHERE "
                "json_extract(after_state,'$.action') LIKE 'canvas.%' "
                "ORDER BY seq").fetchall()
            actions = [r["action"] for r in rows]
        finally:
            st.close()
        self.assertIn("canvas.node_create", actions)
        self.assertIn("canvas.edge_create", actions)
        self.assertIn("canvas.snapshot_create", actions)

    def test_manual_write_no_token_401_and_cross_tenant_404(self):
        self._get()
        r = self.client.post(self.NODES,
                             json={"kind": "note",
                                   "props": {"content": "x"},
                                   "version": 1})
        self.assertEqual(r.status_code, 401)
        r = self._create_node(version=1, headers=self.auth_l)
        self.assertEqual(r.status_code, 404)
        r = self.client.get(self.SNAPS, headers=self.auth_l)
        # 跨租户：画布不可见（_get_owned_case 404，空列表也不应泄露线索）
        self.assertEqual(r.status_code, 404)


# ======================================================================
# M4（RC-105/RC-204）：手册建议采纳 202 编排 + 白名单 Function 扩展查询
# ======================================================================
def _r6_clue() -> LineageClue:
    """rule_id=R6 + H1 假设 + 整数万元行（default 包 3 条 playbook 全命中）。"""
    return LineageClue(
        clue_id="clue-r6", skill_id="time_window",
        title="张卫国·中标时间窗整数资金",
        detail={"rules": [{
            "rule_id": "R6",
            "依据": "中标公示 ±20 天整数资金且主体为个人",
            "rule_text": "招投标中标公示日前后 20 天内出现 1 万元整数倍资金交易"}]},
        assumption_chain=["H1"],
        jian_types=["反间"],
        source_rows=[
            {"资金主体": "张卫国", "金额": 100000},
            {"资金主体": "张卫国", "金额": 200000},
        ])


class CanvasM4ApiTest(unittest.TestCase):
    """RC-105 AC1-4 + RC-204 AC1-4 端到端。"""

    PB_IDS = ("r6_fund_tw_rerun", "r6_call_window", "r6_bid_archive")
    R6 = "/api/v1/cases/c1/clues/clue-r6/canvas"
    R1 = CANVAS

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1)
        self.client = TestClient(create_app(self.ctx))
        for operator, role, pw, tenant, clearance in (
            ("王检察官", "human", "pw-pro", "t1", 4),
            ("李侦查员", "正兵", "pw-sol", "t1", 1),
        ):
            salt, h = hash_password(pw)
            self.repo.create_user(User(operator=operator, password_hash=h,
                                      salt=salt, role=role,
                                      clearance=clearance, tenant_id=tenant))
        salt, h = hash_password("pw-agent")
        self.repo.create_user(User(operator="agent:sunzi", password_hash=h,
                                 salt=salt, role="正兵", clearance=1,
                                 tenant_id="t1"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        self.auth_h = self._login("王检察官", "pw-pro")
        self.auth_a = self._login("agent:sunzi", "pw-agent")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version("c1", 1, "test")
        save_case_clues(self.factory.case_dir("c1"), 1,
                        [_clue(), _r6_clue()])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    def _login(self, operator: str, password: str) -> dict:
        r = self.client.post(
            "/api/v1/auth/login",
            json={"operator": operator, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _state(self) -> StateStore:
        return StateStore("c1", self.factory.case_dir("c1") / "state.sqlite")

    def _drain(self) -> None:
        self.pool.run_until_drained(max_idle_rounds=5)

    def _open(self, clue: str = "r6", headers=None) -> dict:
        path = self.R6 if clue == "r6" else self.R1
        r = self.client.get(path, headers=headers or self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def _rule_node(self, data: dict, rid: str = "R6") -> dict:
        return next(n for n in data["doc"]["nodes"]
                    if n["kind"] == "rule" and n["ref"] == rid)

    def _suggest(self, node_id: str, *, clue: str = "r6", version=None,
                 headers=None):
        path = self.R6 if clue == "r6" else self.R1
        return self.client.post(f"{path}/suggestions",
                             headers=headers or self.auth_h,
                             json={"node_id": node_id, "version": version})

    def _adopt(self, node_id: str, *, text=None, clue: str = "r6",
                 version=None, headers=None):
        path = self.R6 if clue == "r6" else self.R1
        return self.client.post(f"{path}/suggestions/{node_id}/adopt",
                             headers=headers or self.auth_h,
                             json={"text": text, "version": version})

    def _sync(self, targets: list[dict], *, clue: str = "r6",
               version=None, headers=None):
        path = self.R6 if clue == "r6" else self.R1
        return self.client.post(f"{path}/suggestions/sync",
                             headers=headers or self.auth_h,
                             json={"targets": targets, "version": version})

    def _to_verify(self, node_id: str, text, *, clue: str = "r6",
                    version=None, headers=None):
        path = self.R6 if clue == "r6" else self.R1
        return self.client.post(f"{path}/nodes/{node_id}/to-verify",
                             headers=headers or self.auth_h,
                             json={"text": text, "version": version})

    def _verify_items(self, clue: str = "clue-r6", headers=None):
        return self.client.get(
            f"/api/v1/cases/c1/clues/{clue}/verify-items",
            headers=headers or self.auth_h)

    def _generate_three(self):
        """GET r6 画布 + 生成建议；返回 (base, added, by_pb, 建议后版本)。"""
        base = self._open("r6")
        rule_id = self._rule_node(base)["id"]
        r = self._suggest(rule_id, version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(3, len(d["added_nodes"]))
        by_pb = {n["props"]["playbook_id"]: n
                  for n in d["added_nodes"]}
        self.assertEqual(set(self.PB_IDS), set(by_pb))
        return base, d["added_nodes"], by_pb, d["version"]

    # ------------------------------------------------------------------
    # RC-105：建议生成（虚节点，不写 state）
    # ------------------------------------------------------------------
    def test_r1_rule_has_no_playbook_suggestions(self):
        base = self._open("r1")
        rule_id = self._rule_node(base, "R1")["id"]
        r = self._suggest(rule_id, clue="r1", version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual([], d["added_nodes"])
        self.assertEqual([], d["added_edges"])
        self.assertEqual(base["version"], d["version"])  # 无新增不涨版本

    def test_playbook_suggestion_seed_nodes(self):
        _, added, by_pb, sug_v = self._generate_three()
        for n in added:
            self.assertTrue(n["id"].startswith("verify_item:pb:"))
            self.assertTrue(n["ref"].startswith("pb:"))
            self.assertTrue(n["system"])
            self.assertFalse(n["adopted"])
            self.assertEqual("建议", n["props"]["status"])
            self.assertIn("张卫国", n["props"]["text"])
        self.assertIn("2", by_pb["r6_fund_tw_rerun"]["props"]["text"])
        self.assertEqual(
            "住建局招标办",
            by_pb["r6_bid_archive"]["props"]["external"]["target"])
        # 手册建议边全部挂规则节点
        base = self._open("r6")
        pb_ids = {n["id"] for n in added}
        sug_edges = [e for e in base["doc"]["edges"]
                      if e["rel"] == "手册建议"]
        self.assertEqual(3, len(sug_edges))
        for e in sug_edges:
            self.assertEqual("rule:R6", e["source"])
            self.assertIn(e["target"], pb_ids)
            self.assertTrue(e["system"])
        # 幂等：再次生成无新增
        r = self._suggest("rule:R6", version=base["version"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([], r.json()["data"]["added_nodes"])

    def test_suggestion_guards(self):
        base = self._open("r6")
        # 非规则节点 400 / 不存在节点 404
        fact_id = next(n["id"] for n in base["doc"]["nodes"]
                       if n["kind"] == "fact")
        r = self._suggest(fact_id, version=base["version"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual("VALIDATION", r.json()["error"]["code"])
        r = self._suggest("rule:nope", version=base["version"])
        self.assertEqual(r.status_code, 404)

    def test_suggestion_not_in_workbench_before_adopt(self):
        """AC-105-2：未采纳建议仅画布虚节点存在，不进核查工作台。"""
        _, _, by_pb, _ = self._generate_three()
        texts = [n["props"]["text"] for n in by_pb.values()]
        items = self._verify_items().json()["data"]["items"]
        for t in texts:
            self.assertNotIn(t, [i["text"] for i in items])
        self.assertFalse(any(i.get("origin") == "suggested"
                            for i in items))

    # ------------------------------------------------------------------
    # RC-105：采纳 202 → 终态协调
    # ------------------------------------------------------------------
    def test_adopt_via_202_terminal_refresh(self):
        _, _, by_pb, sug_v = self._generate_three()
        pb = by_pb["r6_bid_archive"]
        text = pb["props"]["text"]

        r = self._adopt(pb["id"], version=sug_v)
        self.assertEqual(r.status_code, 202, r.text)
        d = r.json()["data"]
        self.assertEqual("add_manual", d["mode"])
        self.assertTrue(d["task"]["id"])

        # 未 drain：sync 报 pending，节点保持虚节点（无假成功）
        r = self._sync([{"node_id": pb["id"]}], version=sug_v)
        self.assertEqual("pending", r.json()["data"]["results"][0]["status"])
        node = next(n for n in self._open("r6")["doc"]["nodes"]
                    if n["id"] == pb["id"])
        self.assertFalse(node["adopted"])

        # worker 终态成功
        self._drain()
        task = self.repo.get_task(d["task"]["id"])
        self.assertEqual(TASK_SUCCEEDED, task.status)
        items = self._verify_items().json()["data"]["items"]
        item = next(i for i in items if i["text"] == text)
        self.assertEqual("待核查", item["status"])
        self.assertEqual("external", item["channel"])
        self.assertEqual("住建局招标办", item["external"]["target"])

        # sync：pb 虚节点迁移为 vi 已采纳节点
        r = self._sync([{"node_id": pb["id"]}], version=sug_v)
        self.assertEqual(r.status_code, 200, r.text)
        synced = r.json()["data"]
        res = synced["results"][0]
        self.assertEqual("adopted", res["status"])
        self.assertEqual(item["item_id"], res["item_id"])
        doc = synced["doc"]
        self.assertNotIn(pb["id"], [n["id"] for n in doc["nodes"]])
        migrated = next(n for n in doc["nodes"]
                       if n["id"] == res["new_node_id"])
        self.assertTrue(migrated["adopted"])
        self.assertEqual(item["item_id"], migrated["ref"])
        self.assertEqual("r6_bid_archive",
                         migrated["props"]["playbook_id"])
        # 手册建议边端点迁移，画布上无旧 pb 残留引用
        for e in doc["edges"]:
            self.assertNotIn(pb["id"], (e["source"], e["target"]))
        # 三条建议仅采纳一条：按迁移后目标节点定位本条系统边
        # （其余两条仍指向各自未采纳的 pb 虚节点）
        edge = next(e for e in doc["edges"]
                    if e["rel"] == "手册建议"
                    and e["target"] == migrated["id"])
        self.assertEqual("rule:R6", edge["source"])
        self.assertTrue(edge["system"])
        self.assertEqual(migrated["id"], edge["target"])
        # 重复采纳 400（计划校验先于入队；带协调后最新版本）
        r = self._adopt(migrated["id"], version=synced["version"])
        self.assertEqual(r.status_code, 400)

        # 审计链：suggestions / suggestion_sync 均可验签
        st = self._state()
        try:
            self.assertTrue(st.chain_verify())
            actions = [x[0] for x in st.conn.execute(
                "SELECT DISTINCT json_extract(after_state,'$.action') "
                "FROM audit_chain WHERE "
                "json_extract(after_state,'$.action') LIKE 'canvas.%'")]
        finally:
            st.close()
        self.assertIn("canvas.suggestions", actions)
        self.assertIn("canvas.suggestion_sync", actions)

    def test_failed_task_no_false_success_then_retry(self):
        """AC-105-3：终态失败保持虚节点（无假成功），改写文本可重新采纳。"""
        _, _, by_pb, sug_v = self._generate_three()
        pb = by_pb["r6_fund_tw_rerun"]
        r = self._adopt(pb["id"], version=sug_v)
        self.assertEqual(202, r.status_code)
        task1 = r.json()["data"]["task"]["id"]

        # 版本指针回 0 → worker NO_VERSION 失败
        self.repo.set_version("c1", 0, "test")
        self._drain()
        self.assertEqual(TASK_FAILED, self.repo.get_task(task1).status)
        self.assertEqual("NO_VERSION",
                         self.repo.get_task(task1).error_code)
        r = self._sync([{"node_id": pb["id"]}], version=sug_v)
        self.assertEqual("pending", r.json()["data"]["results"][0]["status"])
        doc = self._open("r6")["doc"]
        node = next(n for n in doc["nodes"] if n["id"] == pb["id"])
        self.assertEqual("pb:r6_fund_tw_rerun", node["ref"])
        self.assertFalse(node["adopted"])

        # 恢复版本；改写文本 → 新任务（新 idem）→ 成功 → sync 采纳
        self.repo.set_version("c1", 1, "test")
        new_text = pb["props"]["text"] + "（人工改写）"
        r = self._adopt(pb["id"], text=new_text, version=sug_v)
        self.assertEqual(202, r.status_code, r.text)
        task2 = r.json()["data"]["task"]["id"]
        self.assertNotEqual(task1, task2)
        self._drain()
        self.assertEqual(TASK_SUCCEEDED,
                         self.repo.get_task(task2).status)
        r = self._sync([{"node_id": pb["id"], "text": new_text}],
                        version=sug_v)
        self.assertEqual(200, r.status_code)
        res = r.json()["data"]["results"][0]
        self.assertEqual("adopted", res["status"])
        # 路由字段随 add_manual 落 state
        st = self._state()
        try:
            item = st.get_verify_item(res["item_id"])
        finally:
            st.close()
        self.assertEqual(new_text, item["text"])
        self.assertEqual("function", item["channel"])
        self.assertEqual("time_window_collision", item["ref_function"])
        self.assertTrue(item["falsification"])

    def test_hypothesis_to_verify_flow(self):
        base = self._open("r6")
        r = self.client.post(
            f"{self.R6}/nodes", headers=self.auth_h,
            json={"kind": "hypothesis",
                  "props": {"title": "体外循环假设", "content": "多账户过渡"},
                  "x": 100, "y": 100, "version": base["version"]})
        self.assertEqual(200, r.status_code, r.text)
        h_id = r.json()["data"]["node"]["id"]
        ver = r.json()["data"]["version"]

        # 202 入队 → drain → sync 生成待核查节点
        r = self._to_verify(h_id, "核查资金过桥账户", version=ver)
        self.assertEqual(202, r.status_code, r.text)
        self._drain()
        r = self._sync([{"node_id": h_id, "text": "核查资金过桥账户"}],
                        version=ver)
        self.assertEqual(200, r.status_code)
        res = r.json()["data"]["results"][0]
        self.assertEqual("created", res["status"])
        doc = r.json()["data"]["doc"]
        vn = next(n for n in doc["nodes"] if n["id"] == res["new_node_id"])
        self.assertEqual("verify_item", vn["kind"])
        self.assertTrue(vn["adopted"])
        self.assertIn("核查资金过桥账户",
                      [i["text"] for i in self._verify_items().json()[
                          "data"]["items"]])
        # 再 sync：节点已存在 → exists（不重复建点）
        r = self._sync([{"node_id": h_id, "text": "核查资金过桥账户"}],
                        version=r.json()["data"]["version"])
        self.assertEqual("exists", r.json()["data"]["results"][0]["status"])

    def test_to_verify_guards(self):
        base = self._open("r6")
        ver = base["version"]
        rule_id = self._rule_node(base)["id"]
        # 系统规则节点 400
        r = self._to_verify(rule_id, "文本", version=ver)
        self.assertEqual(400, r.status_code)
        # 空文本 400（建节点夹具本身须过 M3 内容校验，content 不得为空）
        h_id = self.client.post(
            f"{self.R6}/nodes", headers=self.auth_h,
            json={"kind": "hypothesis",
                  "props": {"title": "假设", "content": "假设内容"},
                  "x": 0, "y": 0, "version": ver}).json()["data"]["node"]["id"]
        r = self._to_verify(h_id, "   ", version=ver + 1)
        self.assertEqual(400, r.status_code)
        # 不存在节点 404
        r = self._to_verify("cn_ghost", "文本", version=ver + 1)
        self.assertEqual(404, r.status_code)

    def test_agent_write_forbidden_but_read_allowed(self):
        """AC-105-4：agent: 身份采纳/转待核实 403；建议生成为只读，允许。"""
        base = self._open("r1")
        r = self._suggest("rule:R1", clue="r1",
                          version=base["version"], headers=self.auth_a)
        self.assertEqual(200, r.status_code, r.text)
        r = self._adopt("verify_item:pb:x", clue="r1",
                          version=base["version"], headers=self.auth_a)
        self.assertEqual(403, r.status_code)
        self.assertEqual("FORBIDDEN", r.json()["error"]["code"])
        r = self._to_verify("rule:R1", "x", clue="r1",
                            version=base["version"], headers=self.auth_a)
        self.assertEqual(403, r.status_code)

    # ------------------------------------------------------------------
    # RC-204：白名单 Function 扩展查询
    # ------------------------------------------------------------------
    FUNCS = f"{R6}/functions"
    FQUERY = f"{R6}/function-query"

    def test_function_catalog_business_only(self):
        r = self.client.get(self.FUNCS, headers=self.auth_h)
        self.assertEqual(200, r.status_code, r.text)
        d = r.json()["data"]
        self.assertTrue(d["available"])
        names = {f["name"] for f in d["functions"]}
        self.assertEqual(
            {"integer_transfer_aggregates", "quarter_end_integer_deposits",
             "time_window_collision", "overpass_two_hop",
             "call_frequency_spike", "co_located_pairs"}, names)
        for f in d["functions"]:
            self.assertNotIn("sql", f)
            self.assertNotIn("impl", f)
            self.assertNotIn("impl_ref", f)
            for p in f["params"]:
                self.assertNotIn("sql", p)
                # 业务化表单必含契约字段（key/type/label），不暴露技术细节
                self.assertIn("key", p)
                self.assertIn("type", p)
                self.assertIn("label", p)
                # PRD RC-204：标签取业务名（functions.json description），
                # 不允许回退成原始参数名
                self.assertNotEqual(p["key"], p["label"])
        quarter = next(f for f in d["functions"]
                       if f["name"] == "quarter_end_integer_deposits")
        pmap = {p["key"]: p for p in quarter["params"]}
        self.assertEqual(["现金存入"],
                         pmap["cash_summary_tokens"]["enum"])

    def test_function_query_whitelist_and_param_rejected(self):
        base = self._open("r6")
        ver = base["version"]
        # 非白名单
        r = self.client.post(
            self.FQUERY, headers=self.auth_h,
            json={"function": "drop_table", "params": {}, "version": ver})
        self.assertEqual(400, r.status_code)
        # string 枚举外值
        r = self.client.post(
            self.FQUERY, headers=self.auth_h,
            json={"function": "quarter_end_integer_deposits",
                  "params": {"cash_summary_tokens": "转账"}, "version": ver})
        self.assertEqual(400, r.status_code)
        self.assertIn("enum", r.json()["error"]["message"])
        # integer 收字符串
        r = self.client.post(
            self.FQUERY, headers=self.auth_h,
            json={"function": "integer_transfer_aggregates",
                  "params": {"round_unit": "1万"}, "version": ver})
        self.assertEqual(400, r.status_code)
        # 未声明参数
        r = self.client.post(
            self.FQUERY, headers=self.auth_h,
            json={"function": "integer_transfer_aggregates",
                  "params": {"bogus": 1}, "version": ver})
        self.assertEqual(400, r.status_code)
        # 源节点不存在 404（校验在开库前）
        r = self.client.post(
            self.FQUERY, headers=self.auth_h,
            json={"function": "integer_transfer_aggregates", "params": {},
                  "source_node_id": "rule:ghost", "version": ver})
        self.assertEqual(404, r.status_code)

    def test_function_query_datasource_unavailable_no_empty_node(self):
        base = self._open("r6")
        ver = base["version"]
        r = self.client.post(
            self.FQUERY, headers=self.auth_h,
            json={"function": "integer_transfer_aggregates", "params": {},
                  "version": ver})
        self.assertEqual(200, r.status_code, r.text)
        d = r.json()["data"]
        self.assertFalse(d["executed"])
        self.assertEqual("DATASOURCE_UNAVAILABLE", d["code"])
        self.assertEqual("integer_transfer_aggregates", d["function"])
        # 无节点产生、无新版本
        cur = self._open("r6")
        self.assertEqual(ver, cur["version"])
        self.assertFalse(any(n["kind"] == "function_result"
                            for n in cur["doc"]["nodes"]))

    def _make_version_db(self) -> Path:
        import duckdb
        path = self.factory.case_dir("c1") / "v1.duckdb"
        con = duckdb.connect(str(path))
        try:
            con.execute("CREATE TABLE rc204_probe(k INTEGER)")
            con.execute("INSERT INTO rc204_probe VALUES (1)")
        finally:
            con.close()
        return path

    def test_function_query_success_audit_readonly_and_trace(self):
        from server.app.routers import canvas as canvas_router

        base = self._open("r6")
        ver = base["version"]
        rule_id = self._rule_node(base)["id"]
        path = self._make_version_db()
        digest_before = hashlib.sha1(path.read_bytes()).hexdigest()

        class FakeExecutor:
            last = None

            def __init__(self, store, pack="default", access=None,
                         base_dir=None):
                # 只读护栏：经 read 模式版本库进入
                self.assertEqual("read", store.mode)
                self.store = store

            def assertEqual(self, a, b):
                assert a == b

            def invoke(self, name, params):
                FakeExecutor.last = {"name": name, "params": dict(params)}
                return {"function": name, "output_type": "rows",
                        "readonly": True, "params_used": dict(params),
                        "rows": [{"from_raw": "甲", "to_raw": "乙",
                                    "total": 300000}]}

        with patch.object(canvas_router, "FunctionExecutor", FakeExecutor):
            r = self.client.post(
                self.FQUERY, headers=self.auth_h,
                json={"function": "integer_transfer_aggregates", "params": {},
                      "source_node_id": rule_id, "version": ver})
        self.assertEqual(200, r.status_code, r.text)
        d = r.json()["data"]
        self.assertTrue(d["executed"])
        node = d["node"]
        self.assertEqual("function_result", node["kind"])
        self.assertEqual("integer_transfer_aggregates",
                         node["props"]["function"])
        self.assertEqual({"round_unit": 10000}, node["props"]["params"])
        self.assertEqual("王检察官", node["props"]["executed_by"])
        self.assertEqual(1, node["props"]["row_count"])
        self.assertNotIn("sql", node["props"])
        self.assertEqual("查询自", d["edge"]["rel"])
        self.assertEqual(rule_id, d["edge"]["source"])
        self.assertEqual(node["id"], d["edge"]["target"])
        self.assertEqual(ver + 1, d["version"])
        self.assertEqual(
            {"name": "integer_transfer_aggregates",
             "params": {"round_unit": 10000}}, FakeExecutor.last)

        # AC-204-3 只读：版本库文件字节未变，探针行仍在
        self.assertEqual(digest_before,
                         hashlib.sha1(path.read_bytes()).hexdigest())
        import duckdb
        ro = duckdb.connect(str(path), read_only=True)
        try:
            self.assertEqual(
                1, ro.execute("SELECT count(*) FROM rc204_probe")
                .fetchone()[0])
        finally:
            ro.close()

        # 审计链可查
        st = self._state()
        try:
            self.assertTrue(st.chain_verify())
            rows = st.conn.execute(
                "SELECT after_state FROM audit_chain WHERE "
                "json_extract(after_state,'$.action')='canvas.function_query'"
            ).fetchall()
        finally:
            st.close()
        self.assertEqual(1, len(rows))

        # AC-204-4 结果节点可继续 RC-103 溯源（回查询源 + 输入表快照）
        r = self.client.post(f"{self.R6}/expand", headers=self.auth_h,
                             json={"node_id": node["id"], "version": ver + 1})
        self.assertEqual(200, r.status_code, r.text)
        detail = r.json()["data"]["details"][node["id"]]
        self.assertEqual("function_result", detail["kind"])
        self.assertEqual([rule_id], detail["source_node_ids"])
        self.assertEqual(["obj_transaction"], detail["input_tables"])
        self.assertEqual({"round_unit": 10000}, detail["params"])
        # 溯源不落新版本（无新节点）
        self.assertEqual(ver + 1, r.json()["data"]["version"])

    def test_function_query_degraded_and_permission(self):
        from server.app.routers import canvas as canvas_router

        base = self._open("r6")
        ver = base["version"]
        self._make_version_db()

        class DegradedExecutor:
            def __init__(self, *a, **k):
                pass

            def invoke(self, name, params):
                return {"function": name, "degraded": True,
                        "degraded_reason": "缺 obj_transaction",
                        "params_used": params}

        class ForbiddenExecutor:
            def __init__(self, *a, **k):
                pass

            def invoke(self, name, params):
                raise PermissionError("间类策略拒绝")

        with patch.object(canvas_router, "FunctionExecutor", DegradedExecutor):
            r = self.client.post(
                self.FQUERY, headers=self.auth_h,
                json={"function": "integer_transfer_aggregates", "params": {},
                      "version": ver})
        self.assertEqual(200, r.status_code)
        d = r.json()["data"]
        self.assertFalse(d["executed"])
        self.assertEqual("DEGRADED", d["code"])
        self.assertEqual(ver, self._open("r6")["version"])  # 不落节点

        with patch.object(canvas_router, "FunctionExecutor", ForbiddenExecutor):
            r = self.client.post(
                self.FQUERY, headers=self.auth_h,
                json={"function": "integer_transfer_aggregates", "params": {},
                      "version": ver})
        self.assertEqual(403, r.status_code)


if __name__ == "__main__":
    unittest.main()
