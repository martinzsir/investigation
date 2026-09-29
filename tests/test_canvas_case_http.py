"""案件级研判画布 HTTP 契约测试（GET/PATCH /cases/{cid}/case-canvas）。

核心守护的是**错误码**：409 必须用既有码 CONFLICT。
前端 client.ts 的 codeOf() 只认 7 个白名单码，用了别的码会被归成
INTERNAL —— 于是自动保存的 409 重试分支永不命中，正兵改坐标遇到他人
已更新时看到的不是"已按最新版本合并"而是"请求失败"，且持续报错。
这条不能只靠肉眼看代码，必须落在 HTTP 层断言。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server.app.cases import CaseService
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore

CC = "/api/v1/cases/c1/case-canvas"


def _doc(nodes=None, edges=None):
    return {"nodes": nodes or [], "edges": edges or []}


def _subject(nid: str, generated: bool = False):
    props = {"label": "张卫国", "person_pk": "person_abc"}
    if generated:
        # 镜头重建层标记：PATCH 必须把它剥掉，不落库
        props["generated_by"] = "lens_layer"
    return {"id": nid, "kind": "subject", "props": props, "x": 1.0, "y": 2.0}


class CaseCanvasHttpTest(unittest.TestCase):
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
        salt, h = hash_password("pw-pro")
        self.repo.create_user(User(operator="王检察官", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "王检察官", "password": "pw-pro"})
        self.assertEqual(r.status_code, 200, r.text)
        self.auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        r = self.client.post("/api/v1/cases", headers=self.auth,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)
        self.repo.set_version("c1", 1, "test")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self) -> StateStore:
        return StateStore("c1", self.factory.case_dir("c1") / "state.sqlite")

    def _get(self):
        return self.client.get(CC, headers=self.auth)

    def _patch(self, doc, expected_version):
        return self.client.patch(CC, headers=self.auth,
                                 json={"doc": doc,
                                       "expected_version": expected_version})

    # ------------------------------------------------------------------
    def test_first_get_returns_empty_doc(self):
        """首次 GET 惰性建空文档，不报错、version 为 1。"""
        r = self._get()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual("c1", d["case_id"])
        self.assertEqual(1, d["version"])
        self.assertEqual([], d["doc"]["nodes"])

    def test_patch_persists_and_returns_stripped(self):
        """PATCH 落库并返回 version；stripped 反映剥离数量。"""
        self._get()  # 惰性建画布（version=1），否则 PATCH 会走"不存在即建"
        r = self._patch(_doc([_subject("n1"), _subject("n2", generated=True)]), 1)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertEqual(2, d["version"])
        self.assertEqual(1, d["stripped"]["nodes"])  # 只剥掉带标记的 n2

        # 回读：人工节点在、镜头层节点不在
        g = self._get()
        self.assertEqual(200, g.status_code, g.text)
        ids = [n["id"] for n in g.json()["data"]["doc"]["nodes"]]
        self.assertIn("n1", ids)
        self.assertNotIn("n2", ids)

    def test_patch_returns_doc_absent(self):
        """案件级 PATCH **不返回 doc**——调用方不得用它覆盖本地文档。

        返回空 doc 却拿去覆盖，会把整张图抹成空。这条断言守住契约。
        """
        r = self._patch(_doc([_subject("n1")]), 1)
        self.assertEqual(200, r.status_code, r.text)
        self.assertNotIn("doc", r.json()["data"])

    def test_stale_version_conflict_uses_known_code(self):
        """版本基准过期 → 409 且 error.code == CONFLICT（前端白名单内）。"""
        self._get()
        self._patch(_doc([_subject("n1")]), 1)
        r = self._patch(_doc([_subject("n1")]), 1)  # 仍用旧基准
        self.assertEqual(409, r.status_code, r.text)
        code = (r.json().get("error") or {}).get("code")
        self.assertEqual("CONFLICT", code)

    def test_conflict_code_matches_frontend_whitelist(self):
        """CONFLICT 必须在前端 client.ts 的 BACKEND_CODES 白名单里。

        后端一旦改成自定义码（如 CANVAS_VERSION_CONFLICT），本断言会失败——
        它比"看一眼代码"可靠，因为前端白名单在另一个文件里。
        """
        src = (Path(__file__).resolve().parents[1]
               / "frontend" / "src" / "api" / "client.ts").read_text(
            encoding="utf-8")
        self.assertIn("'CONFLICT'", src)

    def test_cross_tenant_404(self):
        """越权统一 404（与既有读面同口径 fail-closed）。"""
        salt, h = hash_password("pw-other")
        self.repo.create_user(User(operator="他人", password_hash=h, salt=salt,
                                   role="human", clearance=4, tenant_id="t2"))
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "他人", "password": "pw-other"})
        auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        r2 = self.client.get(CC, headers=auth)
        self.assertEqual(404, r2.status_code, r2.text)

    # ------------------------------------------------------------------
    # 渐进式生成：GET 按揭示集过滤，PATCH meta 驱动揭示
    # ------------------------------------------------------------------
    TARGET = "case#c1:subject:person_x"

    def _write_observations(self, observations: list[dict]) -> None:
        from server.app.clues_artifact import directed_observations_path
        path = directed_observations_path(self.factory.case_dir("c1"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"observations": observations}, ensure_ascii=False),
            encoding="utf-8")

    def _observation(self, oid: str, *, clue_id: str | None = None) -> dict:
        origin: dict = {"node_id": self.TARGET}
        if clue_id is not None:
            origin["clue_id"] = clue_id
        return {
            "observation_id": oid,
            # 平台真实注册镜头：catalog 自解析 overpass_two_hop → R2 → H4，
            # 不靠调用方注入 function_id（守住真实读侧链路）。
            "skill_id": "fund_overpass_two_hop",
            "lens_name": "两跳过桥路径镜头",
            "title": "两跳过桥",
            "origin": origin,
            "facts": [{"at": "2020-03-10 14:35:00", "type": "transaction"}],
        }

    def test_get_hides_growth_until_revealed(self):
        """未揭示：图上零镜头节点，清单仍有这组且 revealed=False。"""
        self._get()  # 惰性建画布（v1）
        self._write_observations([self._observation("o1")])
        d = self._get().json()["data"]
        kinds = [n["kind"] for n in d["doc"]["nodes"]]
        self.assertNotIn("analysis_result", kinds)
        groups = d["lens_layer"]["groups"]
        self.assertEqual(1, len(groups))
        self.assertFalse(groups[0]["revealed"])
        self.assertEqual(0, d["lens_layer"]["revealed_count"])

    def test_patch_revealed_groups_then_get_grows_only_that_group(self):
        """PATCH 揭示 → 重取：结论与假设长出来，清单 revealed=True。"""
        self._get()
        self._write_observations([self._observation("o1")])
        doc = {"nodes": [], "edges": [], "meta": {"revealed_groups": [
            {"target": self.TARGET, "lens": "fund_overpass_two_hop"}]}}
        pr = self._patch(doc, 1)
        self.assertEqual(200, pr.status_code, pr.text)
        d = self._get().json()["data"]
        kinds = [n["kind"] for n in d["doc"]["nodes"]]
        self.assertIn("analysis_result", kinds)
        self.assertIn("hypothesis", kinds, "首个支撑结论揭示后假设必须出现")
        groups = d["lens_layer"]["groups"]
        self.assertTrue(groups[0]["revealed"])
        self.assertEqual(1, d["lens_layer"]["revealed_count"])

    def test_reveal_only_does_not_pull_other_clue(self):
        """线索域：clue_id 过滤由 lens-results 承担（此处守 GET 不受 clue 影响，
        案件画布始终看全量；线索过滤单测在 test_canvas_growth）。"""
        self._get()
        self._write_observations([
            self._observation("o1", clue_id="clue_a"),
            self._observation("o2")])  # 案件级发起
        d = self._get().json()["data"]
        # 未揭示前两组同镜头同靶心聚合为一组，计数 2（案件级视角不分线索）
        groups = d["lens_layer"]["groups"]
        self.assertEqual(1, len(groups))
        self.assertEqual(2, groups[0]["observation_count"])

    def test_dangling_attach_edge_dropped_when_target_absent(self):
        """靶心主体从未提升：挂接边悬空必须剔除，结论节点保留，计数下发。"""
        self._get()
        self._write_observations([self._observation("o1")])
        doc = {"nodes": [], "edges": [], "meta": {"revealed_groups": [
            {"target": self.TARGET, "lens": "fund_overpass_two_hop"}]}}
        self._patch(doc, 1)
        d = self._get().json()["data"]
        # 没有边的端点会是不存在的 TARGET 主体
        for e in d["doc"]["edges"]:
            self.assertNotEqual(self.TARGET, e["source"])
        self.assertGreaterEqual(d["lens_layer"]["edges_dropped"], 1)


if __name__ == "__main__":
    unittest.main()
