"""
tests/test_vlm_pack.py
P8 多模态图像研判测试（全部离线：fake VLM 注入，不依赖真实 API/网络）。

覆盖 v3 验收：
  1. 模型直入生产=0：VLM 成功只产 image_draft 提案（status=draft），
     runtime 对象/链接零创建；
  2. 断网/超时不影响确定性结果、降级有痕（llm_call_log）、TTL stale；
  3. 证据核验（人）后对象/链接落表（DuckDB 轨道）/ state（Web 轨道），
     graph_view 合成自动入图；
  4. VLM 读不到未脱敏 PII：EXIF 纯字节剥离、face/id_scan 阻断、
     指令文本 PII 复扫拦截。
"""
from __future__ import annotations

import copy
import json
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store
from core.access import AccessContext
from core.llm import image_guard
from core.llm.draft_image import (
    draft_image_inspect,
    is_draft_stale,
    list_image_drafts,
)
from core.llm.image_guard import ImageBlocked
from core.llm.llm_client import LLMClient
from core.ontology import build_ontology
from core.pack_loader import discover as discover_packs
from core.proposal import ProposalStore

ONTO_ROOT = ROOT / "ontology"


# ----------------------------------------------------------------------
# 最小图像字节夹具
# ----------------------------------------------------------------------
def _seg(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg_with_exif() -> bytes:
    """SOI + APP0(JFIF) + APP1(EXIF) + COM + SOS + 熵数据 + EOI。"""
    app0 = _seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    app1 = _seg(0xE1, b"Exif\x00\x00MM\x00*\x00\x00\x00\x08gps-bytes")
    com = _seg(0xFE, b"hello-comment-13800001111")
    sos = _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    return b"\xff\xd8" + app0 + app1 + com + sos + b"\x00\x10\x20\x30\xff\xd9"


def _chunk(typ: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def png_with_text() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    text = b"Comment\x00note-13800001111"
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return (sig + _chunk(b"IHDR", ihdr) + _chunk(b"tEXt", text)
            + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def vlm_client(findings: list[dict], model: str = "qwen-vl-max") -> LLMClient:
    parsed = {"findings": findings, "summary": "..."}
    content = "```json\n" + json.dumps(parsed, ensure_ascii=False) + "\n```"
    return LLMClient(model=model,
                     fake_invoke=lambda **kw: {"content": content})


def setup_db() -> Store:
    """最小语义层（沿用 test_llm_draft 夹具：原始表 + build_ontology）。"""
    s = Store(db_path=":memory:")
    c = s.conn
    c.execute("CREATE TABLE 银行流水 (主体 VARCHAR, 对方 VARCHAR, 金额 DOUBLE, 日期 VARCHAR)")
    c.execute("INSERT INTO 银行流水 VALUES ('张三','李四',10000,'2024-01-01')")
    c.execute("CREATE TABLE 通话记录 (主体 VARCHAR, 对端 VARCHAR, 日期 VARCHAR, 次数 BIGINT)")
    c.execute("INSERT INTO 通话记录 VALUES ('张三','李四','2024-01-01',5)")
    c.execute("CREATE TABLE 工商信息 (主体 VARCHAR, 法人 VARCHAR, 状态 VARCHAR, 关联 VARCHAR)")
    c.execute("INSERT INTO 工商信息 VALUES ('张三','张三','存续','')")
    c.execute("CREATE TABLE 轨迹出行 (日期 VARCHAR, 主体 VARCHAR, 地点 VARCHAR)")
    c.execute("INSERT INTO 轨迹出行 VALUES ('2024-01-01','张三','北京')")
    c.execute("CREATE TABLE 招投标档案 (项目 VARCHAR, 中标方 VARCHAR, 中标公示日 VARCHAR, 分管领导 VARCHAR)")
    c.execute("INSERT INTO 招投标档案 VALUES ('项目A','宏业建设','2024-01-01','张三')")
    c.execute("CREATE TABLE 公开OSINT (主体 VARCHAR, 公开信息 VARCHAR, 发布日期 DATE, 来源 VARCHAR)")
    c.execute("INSERT INTO 公开OSINT VALUES ('张三','公开信息','2024-01-01','来源')")
    c.execute("CREATE TABLE 举报材料 (举报日期 DATE, 分类 VARCHAR, 被举报人 VARCHAR, 举报人 VARCHAR, 内容 VARCHAR)")
    c.execute("INSERT INTO 举报材料 VALUES ('2024-01-01','资金','李四','王五','举报内容')")
    build_ontology(c)
    return s


def web_ctx(network: str = "web") -> AccessContext:
    return AccessContext(operator="test_analyst", role="主办", clearance=2,
                         case_id="C001", network=network)


# ======================================================================
# 1) 纯字节图像闸门（EXIF 剥离 + 敏感类别 block）
# ======================================================================
class TestImageGuard(unittest.TestCase):

    def test_jpeg_strip_removes_exif_com_keeps_jfif(self):
        raw = jpeg_with_exif()
        out = image_guard.strip_metadata(raw)
        self.assertTrue(out.startswith(b"\xff\xd8"))
        self.assertIn(b"JFIF", out)          # APP0 保留
        self.assertNotIn(b"Exif", out)       # APP1 删除
        self.assertNotIn(b"hello-comment", out)
        self.assertTrue(out.endswith(b"\xff\xd9"))
        self.assertFalse(image_guard.has_exif(out))

    def test_png_strip_removes_text_keeps_ihdr_idat(self):
        raw = png_with_text()
        out = image_guard.strip_metadata(raw)
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", out)
        self.assertIn(b"IDAT", out)
        self.assertIn(b"IEND", out)
        self.assertNotIn(b"note-13800001111", out)
        self.assertFalse(image_guard.has_exif(out))

    def test_unknown_format_blocked(self):
        with self.assertRaises(ImageBlocked):
            image_guard.strip_metadata(b"GIF89a\x01\x00rest")
        self.assertTrue(image_guard.has_exif(b"GIF89a"))  # 未知=不可信

    def test_face_id_scan_blocked(self):
        pol = {"face": "block", "id_document": "block"}
        for cls in ("face", "id_scan"):
            with self.assertRaises(ImageBlocked):
                image_guard.assert_sendable({"content_class": cls}, pol)

    def test_mask_treated_as_block(self):
        with self.assertRaises(ImageBlocked):
            image_guard.assert_sendable(
                {"content_class": "face"}, {"face": "mask"})

    def test_undeclared_class_blocked_fail_closed(self):
        with self.assertRaises(ImageBlocked):
            image_guard.assert_sendable({"content_class": ""},
                                        {"face": "block"})
        with self.assertRaises(ImageBlocked):
            image_guard.assert_sendable({"content_class": "weird"}, {})

    def test_safe_classes_pass(self):
        for cls in ("invoice", "receipt", "document", "other"):
            image_guard.assert_sendable({"content_class": cls}, {})


# ======================================================================
# 2) VLM 草案通道（模型直入生产=0 / 降级有痕 / TTL stale）
# ======================================================================
class TestDraftImage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        discover_packs()  # 全局 registry 挂载 packs/vlm（幂等刷新）

    def setUp(self):
        self.store = setup_db()
        self.images = {"evidence/ev_1/a.jpg": jpeg_with_exif()}
        self.loader = lambda uri: self.images[uri]

    def tearDown(self):
        self.store.close()

    def _inspect(self, *, client, content_class="invoice",
                 instruction="", ctx=None, policy=None,
                 subject_type="person", subject_id="p1"):
        return draft_image_inspect(
            self.store.conn, ctx or web_ctx(),
            case_id="C001", image_uri="evidence/ev_1/a.jpg",
            content_class=content_class, instruction=instruction,
            subject_type=subject_type, subject_id=subject_id,
            pack="default", base_dir=ONTO_ROOT,
            llm_client=client, image_loader=self.loader, policy=policy)

    def test_success_only_creates_draft_proposals(self):
        findings = [
            {"title": "发票抬头", "detail": "抬头为宏业建设",
             "severity": "info", "score": 0.9},
            {"title": "票据金额", "detail": "金额 10000 元",
             "severity": "warn", "score": 0.8},
            {"title": "非法条目", "detail": "x", "severity": "critical"},
        ]
        r = self._inspect(client=vlm_client(findings))
        self.assertTrue(r["ok"], f"expected ok, got: {r}")
        self.assertEqual(len(r["proposals"]), 2)
        self.assertEqual(len(r["dropped"]), 1)  # severity 非法
        # 提案落 draft，永不自动生效
        ps = ProposalStore(self.store.conn)
        for p in r["proposals"]:
            rec = ps.get(p["proposal_id"])
            self.assertEqual(rec["status"], "draft")
            self.assertTrue(rec["author"].startswith("model:"))
        # 生产面零写入：runtime 对象/链接表存在（编译期建空表，同 obj_decision）
        # 但零行——草案通道永不产生证据行
        n_obj = self.store.conn.execute(
            "SELECT COUNT(*) FROM obj_image_evidence").fetchone()[0]
        n_lnk = self.store.conn.execute(
            "SELECT COUNT(*) FROM lnk_image_for_person").fetchone()[0]
        self.assertEqual((n_obj, n_lnk), (0, 0))

    def test_llm_disabled_degraded(self):
        pol = copy.deepcopy(_load_real_policy())
        pol["llm_enabled"] = False
        r = self._inspect(client=vlm_client([]), policy=pol)
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["proposals"], [])

    def test_local_network_vision_whitelist_empty_degraded(self):
        r = self._inspect(client=vlm_client([]), ctx=web_ctx("local"))
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])  # local 档无视觉模型

    def test_cloud_vision_whitelist_empty_degraded(self):
        pol = copy.deepcopy(_load_real_policy())
        pol["deployments"]["cloud"]["allowed_vision_models"] = []
        r = self._inspect(client=vlm_client([]), policy=pol)
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])

    def test_timeout_fails_with_trace_no_semantic_change(self):
        def _boom(**kw):
            raise TimeoutError("upstream timeout after 30s")
        client = LLMClient(model="qwen-vl-max", fake_invoke=_boom)
        before = _snapshot_semantic(self.store.conn)
        r = self._inspect(client=client)
        self.assertFalse(r["ok"])
        self.assertIn("timeout", r["error"])
        # 确定性结果不受影响：语义层表行零变化（仅日志/提案允许变化）
        self.assertEqual(before, _snapshot_semantic(self.store.conn))
        self.assertEqual(r["proposals"], [])

    def test_default_client_uses_gate_selected_vision_model(self):
        """未注入 client 时自动构造的 LLMClient 必须携带闸门选出的视觉模型。

        回归：曾写成 LLMClient()，实际调用默认文本模型 qwen-plus——它收到
        image_url 部件不报错但看不见图，回 findings 恒空的合法 JSON，线上
        草案队列永远为 0，且回包 model 标签被外层贴成 qwen-vl-max。
        """
        import core.llm.draft_image as mod

        captured = {}

        class _FakeClient:
            def __init__(self, model=None, **kwargs):
                captured["model"] = model

            def chat_json(self, messages, *, repair_retry=True, **kwargs):
                return {"ok": True, "model": captured["model"], "result": {
                    "content": "...",
                    "parsed": {"findings": [
                        {"title": "t", "detail": "d",
                         "severity": "info"}],
                        "summary": "x"}}}

        orig = mod.LLMClient
        mod.LLMClient = _FakeClient
        try:
            r = self._inspect(client=None)
        finally:
            mod.LLMClient = orig
        self.assertTrue(r["ok"], r)
        self.assertEqual(captured["model"], "qwen-vl-max")
        self.assertEqual(len(r["proposals"]), 1)

    def test_call_failure_reports_real_cause_not_json_parse(self):
        """chat_json 返回 ok=False（缺 API key / HTTP 错误等被 client 捕获
        的调用层失败）时，错误文案必须透传真实原因，不得伪装成 JSON 解析
        失败——否则环境问题会被误诊为模型输出问题。"""

        class _CallFailClient(LLMClient):
            def chat_json(self, messages, *, repair_retry=True, **kwargs):
                return {"ok": False, "model": self.model,
                        "error": "DASHSCOPE_API_KEY 未设置"}

        r = self._inspect(client=_CallFailClient(model="qwen-vl-max"))
        self.assertFalse(r["ok"])
        self.assertFalse(r.get("blocked"))
        self.assertIn("VLM 调用失败", r["error"])
        self.assertIn("DASHSCOPE_API_KEY 未设置", r["error"])
        self.assertNotIn("无法解析", r["error"])
        self.assertEqual(r["proposals"], [])

    def test_instruction_pii_blocked(self):
        r = self._inspect(client=vlm_client([]),
                          instruction="请核对 13800001111 的发票")
        self.assertFalse(r["ok"])
        self.assertTrue(r["blocked"])

    def test_face_class_blocked_before_send(self):
        r = self._inspect(client=vlm_client([]), content_class="face")
        self.assertFalse(r["ok"])
        self.assertTrue(r["blocked"])

    def test_image_load_failure_blocked_with_trace(self):
        r = draft_image_inspect(
            self.store.conn, web_ctx(), case_id="C001",
            image_uri="evidence/missing/x.jpg", content_class="invoice",
            pack="default", base_dir=ONTO_ROOT,
            llm_client=vlm_client([]),
            image_loader=lambda uri: (_ for _ in ()).throw(
                FileNotFoundError("nope")))
        self.assertFalse(r["ok"])
        self.assertIn("图像读取失败", r["error"])

    def test_ttl_stale_unit(self):
        payload = {"input": {"created_epoch": time.time() - 100000,
                             "ttl_s": 86400}}
        self.assertTrue(is_draft_stale(payload))
        fresh = {"input": {"created_epoch": time.time(), "ttl_s": 86400}}
        self.assertFalse(is_draft_stale(fresh))
        self.assertTrue(is_draft_stale({"input": {}}))  # 缺字段 fail-closed

    def test_list_drafts_stale_flag(self):
        findings = [{"title": "t", "detail": "d", "severity": "info"}]
        r = self._inspect(client=vlm_client(findings))
        self.assertTrue(r["ok"])
        now = time.time()
        self.assertFalse(list_image_drafts(
            self.store.conn, case_id="C001", now_epoch=now)[0]["stale"])
        self.assertTrue(list_image_drafts(
            self.store.conn, case_id="C001",
            now_epoch=now + 100000)[0]["stale"])


# ======================================================================
# 3) 人验后落表（DuckDB 轨道：MCP/CLI）
# ======================================================================
class TestPersistImageEvidence(unittest.TestCase):

    def setUp(self):
        self.store = setup_db()

    def tearDown(self):
        self.store.close()

    def _persist(self, subject_type="person", subject_id="p1",
                 model_score=0.9, **kw):
        from core.action_executor import persist_image_evidence
        return persist_image_evidence(
            self.store.conn, "default",
            image_uri="evidence/ev_1/a.jpg", model="qwen-vl-max",
            prompt_version="vlm-invoice-v1", model_score=model_score,
            verifier="test_analyst", verify_conclusion="与原件一致",
            subject_type=subject_type, subject_id=subject_id,
            draft_id="pp-abc", clue_id="L1", **kw)

    def test_person_object_and_link(self):
        r = self._persist()
        self.assertTrue(r["persisted"])
        n_obj = self.store.conn.execute(
            "SELECT COUNT(*) FROM obj_image_evidence").fetchone()[0]
        n_lnk = self.store.conn.execute(
            "SELECT COUNT(*) FROM lnk_image_for_person").fetchone()[0]
        self.assertEqual((n_obj, n_lnk), (1, 1))
        row = self.store.conn.execute(
            "SELECT model, verifier, model_score FROM obj_image_evidence"
        ).fetchone()
        self.assertEqual(row[0], "qwen-vl-max")
        self.assertEqual(row[1], "test_analyst")
        self.assertAlmostEqual(row[2], 0.9)

    def test_org_and_project_links(self):
        from core.action_executor import persist_image_evidence
        self._persist("org", "o1")
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM lnk_image_for_org").fetchone()[0], 1)
        persist_image_evidence(
            self.store.conn, "default", image_uri="u", model="m",
            prompt_version="p", model_score=None, verifier="v",
            verify_conclusion="c", subject_type="bid_project",
            subject_id="prj1", draft_id="pp-xyz")
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM lnk_image_for_project").fetchone()[0], 1)

    def test_idempotent_draft_id(self):
        self._persist()
        r2 = self._persist()
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM obj_image_evidence").fetchone()[0], 1)

    def test_bad_subject_and_score(self):
        with self.assertRaises(ValueError):
            self._persist(subject_type="vehicle", subject_id="x1")
        with self.assertRaises(ValueError):
            self._persist(subject_id="")
        with self.assertRaises(ValueError):
            self._persist(model_score="not-a-number")

    def test_candidate_copy_fields_persisted(self):
        """AI 草案 finding 文案（title/detail/severity）随人验落语义表。"""
        self._persist(title="异常缴款", detail="备注含现金字样", severity="warn")
        row = self.store.conn.execute(
            "SELECT title, detail, severity FROM obj_image_evidence"
        ).fetchone()
        self.assertEqual(tuple(row), ("异常缴款", "备注含现金字样", "warn"))

    def test_legacy_table_columns_added(self):
        """旧库 obj_image_evidence 缺文案三列 → persist 幂等补列后可写。"""
        self.store.conn.execute(
            "CREATE TABLE obj_image_evidence (image_evidence_id VARCHAR, "
            "image_uri VARCHAR, model VARCHAR, prompt_version VARCHAR, "
            "model_score DOUBLE, verifier VARCHAR, verify_conclusion VARCHAR, "
            "subject_type VARCHAR, draft_id VARCHAR, created_at VARCHAR, "
            "source_rows VARCHAR)")
        r = self._persist(title="t", detail="d", severity="warn")
        self.assertTrue(r["persisted"])
        row = self.store.conn.execute(
            "SELECT title, detail, severity FROM obj_image_evidence"
        ).fetchone()
        self.assertEqual(tuple(row), ("t", "d", "warn"))


# ======================================================================
# 4) Web state 轨道 + graph_view 自动入图
# ======================================================================
class TestStateImageEvidence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        case_dir = Path(self.tmp.name)
        self.state = StateStore_connect(case_dir)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_insert_list_and_idempotent(self):
        row = self.state.insert_image_evidence(
            case_id="C001", clue_id="L1", image_uri="evidence/ev_1/a.jpg",
            model="qwen-vl-max", prompt_version="vlm-invoice-v1",
            model_score=0.9, verifier="test_analyst",
            verify_conclusion="与原件一致", subject_type="person",
            subject_id="p1", draft_id="pp-abc")
        self.assertTrue(row["image_evidence_id"].startswith("imgev_"))
        again = self.state.insert_image_evidence(
            case_id="C001", image_uri="x", model="m", prompt_version="p",
            verifier="v", verify_conclusion="c", subject_type="person",
            subject_id="p1", draft_id="pp-abc")
        self.assertEqual(again["image_evidence_id"],
                         row["image_evidence_id"])
        items = self.state.list_image_evidence(case_id="C001")
        self.assertEqual(len(items), 1)
        self.assertEqual(self.state.list_image_evidence(
            case_id="C001", clue_id="L9"), [])

    def test_bad_subject_rejected(self):
        with self.assertRaises(ValueError):
            self.state.insert_image_evidence(
                case_id="C001", image_uri="u", model="m",
                prompt_version="p", verifier="v", verify_conclusion="c",
                subject_type="vehicle", subject_id="x1")
        with self.assertRaises(ValueError):
            self.state.insert_image_evidence(
                case_id="C001", image_uri="u", model="m",
                prompt_version="p", verifier="v", verify_conclusion="c",
                subject_type="person", subject_id="")

    def test_candidate_copy_fields_persisted(self):
        """state.image_evidence 文案三列随核验写入（candidate 同构）。"""
        row = self.state.insert_image_evidence(
            case_id="C001", clue_id="L1", image_uri="evidence/ev_1/a.jpg",
            model="qwen-vl-max", prompt_version="vlm-invoice-v1",
            model_score=0.8, title="异常缴款", detail="备注含现金字样",
            severity="warn", verifier="test_analyst",
            verify_conclusion="与原件一致", subject_type="person",
            subject_id="p1", draft_id="pp-t1")
        self.assertEqual((row["title"], row["detail"], row["severity"]),
                         ("异常缴款", "备注含现金字样", "warn"))

    def test_legacy_db_missing_columns_migrated(self):
        """旧 state.sqlite 缺文案三列 → 重开即幂等补列，可带文案写入。"""
        self.state.close()
        import sqlite3 as _sqlite3
        db = Path(self.tmp.name) / "state.sqlite"
        conn = _sqlite3.connect(str(db))
        conn.execute("DROP TABLE image_evidence")
        conn.execute(
            "CREATE TABLE image_evidence (image_evidence_id TEXT PRIMARY KEY,"
            " case_id TEXT NOT NULL, clue_id TEXT NOT NULL DEFAULT '',"
            " image_uri TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',"
            " prompt_version TEXT NOT NULL DEFAULT '', model_score REAL,"
            " verifier TEXT NOT NULL DEFAULT '',"
            " verify_conclusion TEXT NOT NULL DEFAULT '',"
            " subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,"
            " draft_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)")
        conn.commit()
        conn.close()
        state2 = StateStore_connect(Path(self.tmp.name))
        try:
            row = state2.insert_image_evidence(
                case_id="C001", clue_id="L1", image_uri="evidence/ev_1/a.jpg",
                model="m", prompt_version="p", title="t", detail="d",
                severity="warn", verifier="v", verify_conclusion="c",
                subject_type="person", subject_id="p1", draft_id="pp-t2")
            self.assertEqual((row["title"], row["detail"], row["severity"]),
                             ("t", "d", "warn"))
        finally:
            state2.close()

    def test_graph_view_merges_image_nodes_edges(self):
        from server.app import graph_view
        store = setup_db()
        try:
            rows = [{
                "image_evidence_id": "imgev_test01",
                "subject_type": "person", "subject_id": "p1",
                "image_uri": "evidence/ev_1/a.jpg"}]
            g = graph_view.assemble_graph(
                conn=store.conn, pack="default", base_dir=ONTO_ROOT,
                image_evidence_rows=rows)
            node_ids = {n["id"] for n in g["nodes"]}
            self.assertIn("image_evidence:imgev_test01", node_ids)
            types = {e["type"] for e in g["edges"]}
            self.assertIn("image_for_person", types)
            # edge_kinds 白名单投影：不含图像链接时图像节点不合成
            g2 = graph_view.assemble_graph(
                conn=store.conn, pack="default", base_dir=ONTO_ROOT,
                edge_kinds=["transfers"], image_evidence_rows=rows)
            self.assertNotIn("image_evidence:imgev_test01",
                             {n["id"] for n in g2["nodes"]})
        finally:
            store.close()


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _load_real_policy() -> dict:
    from core.llm.redact import load_llm_policy
    return load_llm_policy("default", ONTO_ROOT)


def _snapshot_semantic(conn) -> dict:
    """语义层 obj_/lnk_ 表行计数（提案/日志/审计除外）。"""
    out: dict[str, int] = {}
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()
    for (t,) in rows:
        if (t.startswith(("obj_", "lnk_"))
                and t not in ("obj_decision",)
                and not t.startswith("lnk_decision")):
            out[t] = conn.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


# ======================================================================
# 5) 书证材料卡 findings 分组（GET /vlm/findings 数据源纯函数）
# ======================================================================
class TestGroupFindings(unittest.TestCase):

    MATERIALS = [{"material_id": "ev_1"}, {"material_id": "ev_2"}]

    @staticmethod
    def _draft(pid: str, mid: str, status: str = "draft",
               stale: bool = False) -> dict:
        return {
            "proposal_id": pid, "status": status, "stale": stale,
            "created_at": "2026-01-01 00:00:00",
            "payload": {
                "candidate": {"title": f"t-{pid}", "detail": f"d-{pid}",
                              "severity": "warn"},
                "input": {"image_uri": f"evidence/{mid}/a.jpg",
                          "model": "qwen-vl-max"},
                "_sort_hint": {"model_score": 0.9},
            },
        }

    @staticmethod
    def _verified(iid: str, mid: str) -> dict:
        return {
            "image_evidence_id": iid, "title": f"t-{iid}",
            "detail": f"d-{iid}", "severity": "info",
            "image_uri": f"evidence/{mid}/b.jpg", "model": "m",
            "model_score": 0.5, "verifier": "v", "verify_conclusion": "c",
            "subject_type": "person", "subject_id": "p1", "clue_id": "L1",
            "created_at": "2026-01-02 00:00:00",
        }

    def test_groups_pending_and_verified_by_material(self):
        from server.app.routers.vlm import _group_findings
        g = _group_findings(
            self.MATERIALS,
            [self._draft("pp-1", "ev_1")],
            [self._verified("imgev_1", "ev_2")])
        self.assertEqual(g["ev_1"]["pending"][0]["proposal_id"], "pp-1")
        self.assertEqual(g["ev_1"]["pending"][0]["title"], "t-pp-1")
        self.assertFalse(g["ev_1"]["pending"][0]["stale"])
        self.assertEqual(g["ev_1"]["verified"], [])
        self.assertEqual(g["ev_2"]["verified"][0]["image_evidence_id"],
                         "imgev_1")
        self.assertEqual(g["ev_2"]["pending"], [])

    def test_skips_non_draft_and_foreign_uri(self):
        """已裁决草案不入 pending；跨材料 uri 不串组；裸 material_id 也认。"""
        from server.app.routers.vlm import _group_findings
        drafts = [
            self._draft("pp-ok", "ev_1"),
            self._draft("pp-approved", "ev_1", status="approve"),
        ]
        drafts.append({
            "proposal_id": "pp-bare", "status": "draft", "stale": True,
            "created_at": "2026-01-01 00:00:00",
            "payload": {"candidate": {"title": "tb", "detail": "db",
                                      "severity": "info"},
                        "input": {"image_uri": "ev_2"}},
        })
        g = _group_findings(self.MATERIALS, drafts, [])
        self.assertEqual(
            [d["proposal_id"] for d in g["ev_1"]["pending"]], ["pp-ok"])
        self.assertEqual(g["ev_2"]["pending"][0]["proposal_id"], "pp-bare")
        self.assertTrue(g["ev_2"]["pending"][0]["stale"])
        # 未知材料 uri 既不新增组也不报错
        g2 = _group_findings(self.MATERIALS, [self._draft("pp-x", "ev_9")], [])
        self.assertEqual(g2["ev_1"]["pending"], [])
        self.assertEqual(g2["ev_2"]["pending"], [])

    def test_empty_materials_empty_group(self):
        from server.app.routers.vlm import _group_findings
        self.assertEqual(_group_findings([], [], []), {})


def StateStore_connect(case_dir: Path):
    from server.app.store.state_store import StateStore
    return StateStore("C001", case_dir / "state.sqlite")


if __name__ == "__main__":
    unittest.main()
