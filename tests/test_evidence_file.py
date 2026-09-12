"""
tests/test_evidence_file.py
核查工作区 REQ-V-009/010：证据材料存储、元数据与上传/下载 API（evidencefile 组）。

REQ-V-009 断言（实施方案验收 1~5 + 方法层补充）：
  1. 上传 ../evil.txt → 存储名不含路径穿越成分，orig_name 保留原样；
  2. 同文件两次上传 sha256 相同、material_id 不同（允许重复留证）；
  3. >20MB → EvidenceTooLargeError（API 层映射 413），不留文件不留目录；
  4. list_evidence 按 uploaded_at 降序（同刻 rowid 稳定）；
  5. link/unlink：挂接回填 item_id，解除后行仍在（REQ-V-011 方法层前提）；
  6. 旧案件 state.sqlite（无 clue_evidence 表）打开即建表可用；
  7. BUILD 不读 evidence/ 目录——evidence/ 在 ingest 白名单外（验收 5，
     由现有 etl/ingest 组回归保障，本文件不重复断言）。

REQ-V-010 断言（EvidenceApiTest，TestClient 造案模式对齐 test_verify_api）：
  AC-1 上传 200 且 audit_chain 追加 evidence_upload、chain_verify 通过；
  AC-2 下载流为原文件字节、Content-Disposition 含 orig_name；
  AC-3 跨租户 404；下载记平台审计一条；
  AC-4 operator=agent:* 上传 → 403（书证是人的行为）；
  另含：清单元数据投影（内部列不外泄）、空文件/非法类型 400、
  跨线索下载 404、>20MB 413。
"""
from __future__ import annotations

import hashlib
import io
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
from server.app.evidence_store import (
    MAX_EVIDENCE_SIZE,
    EvidenceTooLargeError,
    sanitize_filename,
    save_evidence_file,
)
from server.app.main import create_app
from server.app.meta.models import User
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore

CASE = "demoF"
CLUE = "clue_9446b1bd"
ITEM = "vi_d0577e14036a1c2e"


def _fake_upload(name: str, data: bytes):
    """Starlette UploadFile 最小替身（.filename/.file）。"""
    return type("U", (), {"filename": name, "file": io.BytesIO(data)})()


class SanitizeFilenameTest(unittest.TestCase):
    def test_traversal_both_separators(self):
        self.assertEqual(sanitize_filename("../evil.txt"), "evil.txt")
        self.assertEqual(sanitize_filename("..\\..\\evil.txt"), "evil.txt")
        self.assertEqual(sanitize_filename("a/b/c.pdf"), "c.pdf")

    def test_control_chars_and_edges(self):
        self.assertEqual(sanitize_filename("回执\x00\x1f.pdf"), "回执.pdf")
        self.assertEqual(sanitize_filename("  .hidden . "), "hidden")
        self.assertEqual(sanitize_filename("..."), "evidence")
        self.assertEqual(sanitize_filename(""), "evidence")

    def test_normal_name_kept(self):
        self.assertEqual(sanitize_filename("招行流水回执.pdf"),
                         "招行流水回执.pdf")


class SaveEvidenceFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.case_dir = self.tmp / "cases" / CASE

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_writes_sanitized_path_and_hashes(self):
        data = b"bank statement bytes"
        r = save_evidence_file(self.case_dir, ("../evil.txt", data))
        self.assertTrue(r["material_id"].startswith("ev_"))
        self.assertEqual(r["filename"], "evil.txt")
        self.assertEqual(r["orig_name"], "../evil.txt")  # 原名保留
        self.assertEqual(r["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(r["size"], len(data))
        # 验收 1：存储路径不含穿越成分，文件真实落盘
        self.assertFalse(any(p == ".." for p in r["path"].parts))
        self.assertEqual(r["path"].read_bytes(), data)
        self.assertIn("evidence", r["path"].parts)

    def test_duplicate_upload_same_sha_diff_material(self):
        data = b"same content"
        a = save_evidence_file(self.case_dir, ("回执.pdf", data))
        b = save_evidence_file(self.case_dir, ("回执.pdf", data))
        self.assertEqual(a["sha256"], b["sha256"])  # 验收 2
        self.assertNotEqual(a["material_id"], b["material_id"])
        self.assertTrue(a["path"].exists() and b["path"].exists())
        self.assertNotEqual(a["path"].parent, b["path"].parent)

    def test_upload_file_like_object(self):
        r = save_evidence_file(
            self.case_dir, _fake_upload("监控截图.png", b"\x89PNG"))
        self.assertEqual(r["filename"], "监控截图.png")
        self.assertEqual(r["size"], 4)

    def test_too_large_rejected_no_side_effect(self):
        big = b"x" * (MAX_EVIDENCE_SIZE + 1)
        with self.assertRaises(EvidenceTooLargeError):
            save_evidence_file(self.case_dir, ("big.bin", big))
        # 不留目录不留文件
        self.assertFalse((self.case_dir / "evidence").exists())

    def test_exactly_at_limit_allowed(self):
        data = b"x" * MAX_EVIDENCE_SIZE
        r = save_evidence_file(self.case_dir, ("big.bin", data))
        self.assertEqual(r["size"], MAX_EVIDENCE_SIZE)


class EvidenceMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "cases" / CASE / "state.sqlite"
        self.st = StateStore(CASE, self.db)

    def tearDown(self):
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert(self, *, name="回执.pdf", at="2026-09-12 10:00:00",
                item_id=None, mid=None) -> dict:
        return self.st.insert_evidence(
            case_id=CASE, clue_id=CLUE, material_type="付款凭证",
            filename=name, orig_name=name, sha256=f"sha-{name}-{at}",
            size=100, uploaded_by="王峰", uploaded_at=at,
            item_id=item_id, material_id=mid)

    def test_insert_get_roundtrip(self):
        row = self._insert(mid="ev_fixed000001")
        self.assertEqual(row["material_id"], "ev_fixed000001")
        self.assertEqual(row["case_id"], CASE)
        self.assertEqual(row["clue_id"], CLUE)
        self.assertIsNone(row["item_id"])  # 线索级材料
        self.assertEqual(row["note"], "")
        got = self.st.get_evidence("ev_fixed000001")
        self.assertEqual(got["sha256"], "sha-回执.pdf-2026-09-12 10:00:00")
        self.assertIsNone(self.st.get_evidence("ev_missing"))

    def test_list_order_uploaded_at_desc(self):
        self._insert(name="a.pdf", at="2026-09-12 10:00:00")
        self._insert(name="b.pdf", at="2026-09-12 11:00:00")
        self._insert(name="c.pdf", at="2026-09-12 10:00:00")
        names = [r["filename"] for r in self.st.list_evidence(CLUE)]
        # 验收 4：uploaded_at 降序；同刻按 rowid 逆序（后插在前）
        self.assertEqual(names, ["b.pdf", "c.pdf", "a.pdf"])

    def test_list_filter_by_item(self):
        self._insert(name="linked.pdf", item_id=ITEM)
        self._insert(name="loose.pdf")
        by_item = [r["filename"] for r in self.st.list_evidence(CLUE, ITEM)]
        self.assertEqual(by_item, ["linked.pdf"])

    def test_link_unlink_keeps_row(self):
        row = self._insert(name="回执.pdf")
        mid = row["material_id"]
        linked = self.st.link_evidence(mid, ITEM)
        self.assertEqual(linked["item_id"], ITEM)
        self.assertEqual(
            self.st.list_evidence(CLUE, ITEM)[0]["material_id"], mid)
        un = self.st.unlink_evidence(mid)
        self.assertIsNone(un["item_id"])  # REQ-V-011 验收 2：行仍在
        self.assertIsNotNone(self.st.get_evidence(mid))
        # 材料不存在 → None（Worker 层映射 MATERIAL_NOT_FOUND）
        self.assertIsNone(self.st.link_evidence("ev_nope", ITEM))
        self.assertIsNone(self.st.unlink_evidence("ev_nope"))

    def test_legacy_db_without_evidence_table(self):
        self.st.close()
        # 清掉 setUp 建的新库，造真正旧库：只有 audit_chain，无 clue_evidence
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db) + suffix)
            if p.exists():
                p.unlink()
        legacy = sqlite3.connect(str(self.db))
        legacy.execute("CREATE TABLE audit_chain (seq INTEGER)")
        legacy.commit()
        legacy.close()
        st = StateStore(CASE, self.db)  # 打开即幂等建表
        try:
            row = st.insert_evidence(
                case_id=CASE, clue_id=CLUE, material_type="合同",
                filename="c.pdf", orig_name="c.pdf", sha256="s", size=1,
                uploaded_by="王峰", uploaded_at="t")
            self.assertTrue(row["material_id"].startswith("ev_"))
        finally:
            st.close()
        self.st = StateStore(CASE, self.db)  # 还原给 tearDown


class EvidenceApiTest(unittest.TestCase):
    """REQ-V-010 上传/下载/清单 API（TestClient 造案，无异步任务参与）。"""

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
        for operator, role in (("王检察官", "human"),
                               ("李侦查员", "正兵"),
                               ("agent:sunzi", "正兵")):
            salt, h = hash_password("pw")
            self.repo.create_user(User(operator=operator, password_hash=h,
                                       salt=salt, role=role, clearance=1,
                                       tenant_id="t1"))
        salt, h = hash_password("pw")
        self.repo.create_user(User(operator="李检", password_hash=h,
                                   salt=salt, role="正兵", clearance=1,
                                   tenant_id="t2"))
        self.auth_h = self._login("王检察官")
        self.auth_t2 = self._login("李检")
        self.auth_agent = self._login("agent:sunzi")
        r = self.client.post("/api/v1/cases", headers=self.auth_h,
                             json={"case_id": "c1", "name": "测试案"})
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 辅助 ----
    def _login(self, operator: str) -> dict:
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": operator, "password": "pw"})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['data']['token']}"}

    def _upload(self, headers: dict | None = None, case_id: str = "c1",
                clue_id: str = "clue-1", name: str = "receipt.pdf",
                content: bytes = b"bank statement bytes",
                material_type: str = "付款凭证", note: str = ""):
        data = {"material_type": material_type}
        if note:
            data["note"] = note
        return self.client.post(
            f"/api/v1/cases/{case_id}/clues/{clue_id}/evidence",
            headers=headers or self.auth_h,
            files={"file": (name, content)}, data=data)

    def _state(self, case_id: str = "c1") -> StateStore:
        return StateStore(case_id,
                          self.factory.case_dir(case_id) / "state.sqlite")

    # ---- AC-1：上传 200 + evidence_upload 落链 + chain_verify ----
    def test_upload_audit_chain(self):
        r = self._upload(name="../evil.txt", note="银行回执")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["material_id"].startswith("ev_"))
        self.assertEqual(d["filename"], "evil.txt")  # 存储名消毒
        self.assertEqual(d["orig_name"], "../evil.txt")  # 原名保留
        self.assertEqual(d["sha256"],
                         hashlib.sha256(b"bank statement bytes").hexdigest())
        self.assertEqual(d["material_type"], "付款凭证")
        # 元数据行落库；文件落 evidence/{mid}/ 且无穿越成分
        st = self._state()
        try:
            row = st.get_evidence(d["material_id"])
            self.assertEqual(row["clue_id"], "clue-1")
            self.assertEqual(row["uploaded_by"], "王检察官")
            self.assertTrue(st.chain_verify())
        finally:
            st.close()
        fpath = (self.factory.case_dir("c1") / "evidence"
                 / d["material_id"] / "evil.txt")
        self.assertEqual(fpath.read_bytes(), b"bank statement bytes")

    # ---- AC-2：下载流 + Content-Disposition ----
    def test_download_stream_and_platform_audit(self):
        mid = self._upload().json()["data"]["material_id"]
        r = self.client.get(
            f"/api/v1/cases/c1/clues/clue-1/evidence/{mid}/download",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, b"bank statement bytes")
        self.assertIn("receipt.pdf", r.headers.get("content-disposition", ""))
        # AC-3：下载记平台审计一条（evidence_download，operator/purpose）
        events = self.repo.list_platform_events(event="evidence_download")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["operator"], "王检察官")
        detail = json.loads(events[0]["detail"])
        self.assertEqual(detail["material_id"], mid)
        self.assertEqual(detail["purpose"], "证据外带下载")

    # ---- AC-3：跨租户 404；未登录 401 ----
    def test_cross_tenant_404_and_unauthorized_401(self):
        mid = self._upload().json()["data"]["material_id"]
        url = f"/api/v1/cases/c1/clues/clue-1/evidence/{mid}/download"
        self.assertEqual(self.client.get(url, headers=self.auth_t2).status_code,
                         404)
        self.assertEqual(self._upload(headers=self.auth_t2).status_code, 404)
        self.assertEqual(self.client.get(
            "/api/v1/cases/c1/clues/clue-1/evidence",
            headers=self.auth_t2).status_code, 404)
        noauth = self.client.get(
            "/api/v1/cases/c1/clues/clue-1/evidence")
        self.assertEqual(noauth.status_code, 401)

    # ---- AC-4：agent 上传 403（书证是人的行为）----
    def test_agent_upload_forbidden(self):
        r = self._upload(headers=self.auth_agent)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "FORBIDDEN")
        # 拒绝在前：无书证文件、无元数据行（state.sqlite 由建案审计链初始化，
        # 不能以「不存在」为断言）
        self.assertFalse((self.factory.case_dir("c1") / "evidence").exists())
        st = self._state()
        try:
            self.assertEqual(st.list_evidence("clue-1"), [])
        finally:
            st.close()

    # ---- 清单：元数据投影、降序、内部列不外泄 ----
    def test_list_metadata_projection(self):
        self._upload(name="a.pdf", content=b"A")
        self._upload(name="b.pdf", content=b"B", material_type="合同")
        r = self.client.get("/api/v1/cases/c1/clues/clue-1/evidence",
                            headers=self.auth_h)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["data"]["items"]
        self.assertEqual([it["orig_name"] for it in items],
                         ["b.pdf", "a.pdf"])  # uploaded_at 降序（同刻 rowid）
        self.assertTrue(all("case_id" not in it for it in items))
        self.assertTrue(all("content" not in it for it in items))

    # ---- 参数形状快速失败：空文件/非法类型 413/400 ----
    def test_upload_validation(self):
        self.assertEqual(self._upload(content=b"").status_code, 400)
        r = self._upload(material_type="不存在的类型")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "VALIDATION")
        big = b"x" * (MAX_EVIDENCE_SIZE + 1)
        self.assertEqual(self._upload(content=big).status_code, 413)
        # 413 拒绝后无残留
        r = self.client.get("/api/v1/cases/c1/clues/clue-1/evidence",
                            headers=self.auth_h)
        self.assertEqual(r.json()["data"]["items"], [])

    # ---- 跨线索/不存在材料下载 404（不枚举同态）----
    def test_download_wrong_clue_404(self):
        mid = self._upload(clue_id="clue-1").json()["data"]["material_id"]
        r = self.client.get(
            f"/api/v1/cases/c1/clues/clue-2/evidence/{mid}/download",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 404)
        r = self.client.get(
            f"/api/v1/cases/c1/clues/clue-1/evidence/ev_missing0000/download",
            headers=self.auth_h)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()

