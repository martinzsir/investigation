"""
tests/test_verify_draft.py
REQ-V-019 LLM 核查方向草案（守门矩阵端到端）。

AC 对应（实施方案 REQ-V-019）：
  AC-1 能力总闸：llm_enabled=false / isolated 会话 / cloud 档未启用 →
       degraded（ok=false, mode=off），零模型调用，llm_call_log(allowed=false)
       落库 + 审计链（REQ-040 AC4 同款）；
  AC-2 装载 fail-closed：旧 llm_policy.json（无 deployments 段）装载后两档
       disabled；内置最严默认策略同样无可用档；
  AC-3 授权/端点闸：cloud 档 clearance 不足拒；非 HTTPS 拒；host 越白名单拒；
       local 档解析到公网 IP 拒（resolve 注入假解析，不依赖真实网络）；
       全部零模型调用；
  AC-4 脱敏分档：strict（cloud）人名 tokenize → 出网 payload 无真名、
       token map 仅请求内存 → 模型产出 rehydrate 回真名落提案；
  AC-5 幻觉护栏：非法 channel / 白名单外 function / 状态变更指令值 /
       external 缺字段候选整体丢弃（dropped 留因），不生成自由 SQL；
  AC-6 幂等：同 clue 同 content_sha1 不重复提交（in-batch + 跨请求）；
  AC-7 shadow 隔离：除 proposal/llm_call_log/audit_chain 外零表变化；
  AC-8 API 端点：POST .../verify-items/draft degraded 200 语义 + 线索 404 +
       fake 通道成功落提案（status=draft）；
  AC-9 审批桥接：origin='ai_draft' 提案审批通过带 channel/ref_function/
       external/falsification 落核查项（operator=审批人）；人工提案行为
       不变（origin=manual）；非法 origin Worker 拒绝。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
from fastapi.testclient import TestClient

from core.access import AccessContext
from core.audit import AuditChain
from core.llm.draft_verify import draft_verify_items, select_deployment
from core.llm.redact import load_llm_policy, tokenize_name
from core.proposal import ProposalStore

from server.app.cases import CaseService
from server.app.clues_artifact import save_case_clues
from server.app.deps import WebContext
from server.app.main import create_app
from server.app.meta.models import CASE_ACTIVE, CaseRecord
from server.app.meta.repo_sqlite import SqliteMetaRepo
from server.app.security import hash_password
from server.app.store import StoreFactory
from server.app.store.state_store import StateStore
from server.app.worker.pool import WorkerPool
from server.app.worker.tasks import (
    TASK_BUILD,
    TASK_VERIFY,
    TaskExecError,
    enqueue_task,
    handle_build,
)
from server.app.worker.verify import handle_verify
from core.registry import LineageClue

PACK = "default"
BASE_DIR = ROOT / "ontology"
CLUE = "clue-1"


def _clue(cid: str = CLUE, title: str = "张三资金链异常") -> LineageClue:
    return LineageClue(clue_id=cid, skill_id="xu_shi", title=title,
                       jian_types=["生间"],
                       source_rows=[{"file": "b.parquet", "row_id": 1}])


# ----------------------------------------------------------------------
# core 层：draft_verify_items 守门矩阵
# ----------------------------------------------------------------------
class _FakeLLM:
    """注入 fake：chat_json 形状对齐 LLMClient（ok 剥壳由 draft_verify 做）。"""

    def __init__(self, parsed, *, fail=False):
        self.parsed = parsed
        self.fail = fail
        self.calls = 0

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        if self.fail:
            return {"ok": False, "model": "fake-model",
                    "error": "模拟网络失败"}
        return {"ok": True, "model": "fake-model",
                "result": {"content": "```json```", "raw": {},
                           "parsed": self.parsed}}


def _ctx(network="web", clearance=4, operator="王检察官"):
    return AccessContext(operator=operator, role="human",
                         clearance=clearance, case_id="c1",
                         purpose="draft_verify", network=network)


def _policy(*, cloud_enabled=True, strict=True, min_clearance=2,
            llm_enabled=True, base_url="https://api.fake.example.com/v1",
            whitelist=("api.fake.example.com",)):
    return {
        "llm_enabled": llm_enabled,
        "network": "isolated",
        "allowed_models": ["fake-model"],
        "mode": "shadow",
        "pii_redaction": {"id_card": "redact", "phone": "redact",
                          "bank_card": "redact", "precise_track": "drop",
                          "call_content": "drop", "name": "tokenize"},
        "retention": {"prompt_days": 0, "raw_context": "never_store"},
        "fallback": "deterministic_only",
        "deployments": {
            "local": {"enabled": False},
            "cloud": {
                "enabled": cloud_enabled,
                "base_url": base_url,
                "allowed_models": ["fake-model"],
                "redaction": "strict" if strict else "relaxed",
                "require_clearance": True,
                "min_clearance": min_clearance,
                "endpoints": {"https_only": True,
                              "host_whitelist": list(whitelist)},
            },
        },
    }


_CONTEXT = {
    "线索": {"clue_id": CLUE, "标题": "张三与李四资金链异常",
             "依据": "整数进账 7 笔", "维度": "资金"},
    # 结构化主体（_NAME_KEYS 命中键名）→ strict 档 build_token_map 收集人名
    "主体": [{"姓名": "张三", "角色": "账户持有人"},
             {"姓名": "李四", "角色": "对手方"}],
    "已有核查项": [],
}


def _llm_log(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS llm_call_log (log_id TEXT)")
    return conn.execute("SELECT COUNT(*) FROM llm_call_log").fetchone()[0]


class DraftVerifyCoreTest(unittest.TestCase):
    """draft_verify_items：能力闸/选档/端点闸/护栏/幂等/shadow。"""

    def setUp(self):
        self.conn = duckdb.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    # ---- AC-1 能力总闸：llm_enabled=false → degraded，零模型调用 ----
    def test_off_when_llm_disabled(self):
        fake = _FakeLLM({"items": []})
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR, llm_client=fake,
            policy=_policy(llm_enabled=False))
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(r["mode"], "off")
        self.assertEqual(r["proposals"], [])
        self.assertEqual(fake.calls, 0)  # 零模型调用
        self.assertGreaterEqual(_llm_log(self.conn), 1)  # 降级落日志

    # ---- AC-1 isolated 会话全拒（内核纯离线） ----
    def test_off_when_isolated_session(self):
        fake = _FakeLLM({"items": []})
        r = draft_verify_items(
            self.conn, _ctx(network="isolated"), CLUE, case_id="c1",
            context=dict(_CONTEXT), pack=PACK, base_dir=BASE_DIR,
            llm_client=fake, policy=_policy())
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertEqual(fake.calls, 0)

    # ---- AC-1/AC-2 cloud 档未启用 → off（会话∩策略交集为空） ----
    def test_off_when_cloud_disabled(self):
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR, llm_client=_FakeLLM({"items": []}),
            policy=_policy(cloud_enabled=False))
        self.assertFalse(r["ok"])
        self.assertTrue(r["degraded"])
        self.assertIn("cloud", r["reason"])

    # ---- AC-3 cloud 档 clearance 不足 → blocked，零模型调用 ----
    def test_cloud_blocked_low_clearance(self):
        fake = _FakeLLM({"items": []})
        r = draft_verify_items(
            self.conn, _ctx(clearance=1), CLUE, case_id="c1",
            context=dict(_CONTEXT), pack=PACK, base_dir=BASE_DIR,
            llm_client=fake, policy=_policy(min_clearance=2))
        self.assertFalse(r["ok"])
        self.assertFalse(r["degraded"])
        self.assertTrue(r.get("blocked"))
        self.assertIn("clearance", r["error"])
        self.assertEqual(fake.calls, 0)

    # ---- AC-3 端点闸：非 HTTPS 拒 ----
    def test_cloud_blocked_http_endpoint(self):
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR,
            llm_client=_FakeLLM({"items": []}),
            policy=_policy(base_url="http://api.fake.example.com/v1"))
        self.assertTrue(r.get("blocked"))
        self.assertIn("HTTPS", r["error"])

    # ---- AC-3 端点闸：host 越白名单拒 ----
    def test_cloud_blocked_host_not_whitelisted(self):
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR,
            llm_client=_FakeLLM({"items": []}),
            policy=_policy(base_url="https://evil.example.com/v1"))
        self.assertTrue(r.get("blocked"))
        self.assertIn("host_whitelist", r["error"])

    # ---- 成功路径（strict 档）：tokenize → rehydrate → 提案落库 ----
    def test_success_strict_redaction_and_rehydrate(self):
        token = tokenize_name("张三")
        fake = _FakeLLM({"items": [{
            "text": f"约谈 {token} 核对 6 月整数进账对手方",
            "dimension": "资金", "channel": "function",
            "function": "call_frequency_spike",
            "falsification": "对手方均为正常贸易客户",
        }]})
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1",
            context=dict(_CONTEXT), pack=PACK, base_dir=BASE_DIR,
            llm_client=fake, policy=_policy())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["mode"], "cloud")
        self.assertEqual(len(r["proposals"]), 1)
        p = r["proposals"][0]
        self.assertIn("张三", p["text"])       # rehydrate 回真名
        self.assertNotIn(token, p["text"])
        self.assertEqual(p["channel"], "function")
        self.assertEqual(p["ref_function"], "call_frequency_spike")
        # 提案落库 status=draft，input.origin=ai_draft
        rec = ProposalStore(self.conn, pack=PACK).get(p["proposal_id"])
        self.assertEqual(rec["status"], "draft")
        inp = rec["payload"]["input"]
        self.assertEqual(inp["origin"], "ai_draft")
        self.assertEqual(inp["content_sha1"], p["content_sha1"])
        # strict 档：token map 不入库（payload 无真名→有 rehydrate 后文本，
        # 但出网 prompt 侧无 token map 落痕——payload 只含最终文本与哈希）
        self.assertNotIn("token_map", rec["payload"].get("input") or {})
        self.assertEqual(fake.calls, 1)

    # ---- AC-5 幻觉护栏：非法候选整体丢弃、合法候选保留 ----
    def test_hallucination_guard_drops_bad_candidates(self):
        fake = _FakeLLM({"items": [
            {"text": "a", "channel": "sql"},                      # 非法 channel
            {"text": "b", "channel": "function",
             "function": "自创函数名"},                            # 白名单外
            {"text": "把张三标记为已立案", "channel": "manual"},     # 状态变更指令
            {"text": "c", "channel": "external",
             "external": {"target": "", "material": ""}},          # external 缺字段
            {"text": "d", "channel": "function",
             "function": "call_frequency_spike"},                  # 合法
        ]})
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR, llm_client=fake,
            policy=_policy())
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["proposals"]), 1)
        self.assertEqual(r["proposals"][0]["text"], "d")
        self.assertEqual(len(r["dropped"]), 4)
        self.assertTrue(all(d.get("reason") for d in r["dropped"]))

    # ---- AC-6 幂等：同批重复 + 跨请求重复不重复提交 ----
    def test_idempotent_duplicates(self):
        item = {"text": "复跑 co_located_pairs 核验同行轨迹",
                "dimension": "轨迹", "channel": "function",
                "function": "co_located_pairs"}
        fake = _FakeLLM({"items": [item, dict(item)]})
        r1 = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR, llm_client=fake, policy=_policy())
        self.assertTrue(r1["ok"])
        self.assertEqual(len(r1["proposals"]), 1)   # in-batch 去重
        self.assertEqual(r1["duplicates"], 1)
        # 跨请求：同内容第二次全落 duplicates
        fake2 = _FakeLLM({"items": [dict(item)]})
        r2 = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR, llm_client=fake2, policy=_policy())
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["proposals"], [])
        self.assertEqual(r2["duplicates"], 1)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM proposal").fetchone()[0]
        self.assertEqual(n, 1)

    # ---- AC-7 shadow 隔离：仅 LLM 自有表变化 ----
    def test_shadow_isolation(self):
        fake = _FakeLLM({"items": [{
            "text": "核对轨迹同行", "dimension": "轨迹",
            "channel": "function", "function": "co_located_pairs"}]})
        r = draft_verify_items(
            self.conn, _ctx(), CLUE, case_id="c1", context=dict(_CONTEXT),
            pack=PACK, base_dir=BASE_DIR, llm_client=fake, policy=_policy())
        self.assertTrue(r["ok"])
        tables = {row[0] for row in self.conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'").fetchall()}
        # 纯 LLM 连接上只允许出现 LLM 自有三表 + meta_ontology_state；
        # 语义层/业务表绝不落痕。meta_ontology_state 是 AuditChain 版本锚点
        # 读路径的结构性建表（CREATE IF NOT EXISTS 空表，非业务写入），
        # shadow_diff 只比行数（空表 delta=0）不算越界，这里显式断言 0 行。
        self.assertTrue(
            tables <= {"proposal", "llm_call_log", "audit_chain",
                       "meta_ontology_state"},
            tables)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM meta_ontology_state").fetchone()[0], 0)
        self.assertFalse([t for t in tables if t.startswith(("obj_", "lnk_"))])
        # 调用日志 + 审计链留痕
        self.assertEqual(fake.calls, 1)
        chain = AuditChain(self.conn)
        self.assertTrue(chain.chain_verify())


class PolicyLoadDeploymentsTest(unittest.TestCase):
    """AC-2 装载 fail-closed：旧文件/缺段/段非法 → 两档 disabled。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data: dict):
        import json
        d = self.tmp / PACK
        d.mkdir(parents=True, exist_ok=True)
        (d / "llm_policy.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_legacy_file_without_deployments_disabled(self):
        self._write({"network": "isolated", "allowed_models": [],
                     "llm_enabled": True})
        pol = load_llm_policy(PACK, self.tmp)
        deps = pol["deployments"]
        self.assertFalse(deps["local"].get("enabled"))
        self.assertFalse(deps["cloud"].get("enabled"))
        # 会话面投影：web 会话 → off
        mode, dep, reason = select_deployment(_ctx(), pol)
        self.assertEqual(mode, "off")
        self.assertIn("cloud", reason)

    def test_malformed_deployments_section_disabled(self):
        self._write({"network": "web", "allowed_models": [],
                     "llm_enabled": True, "deployments": "oops"})
        pol = load_llm_policy(PACK, self.tmp)
        self.assertFalse(pol["deployments"]["local"].get("enabled"))
        self.assertFalse(pol["deployments"]["cloud"].get("enabled"))

    def test_partial_section_keeps_valid_slot(self):
        self._write({"network": "web", "allowed_models": [],
                     "llm_enabled": True,
                     "deployments": {"cloud": {"enabled": True,
                                               "allowed_models": ["m1"]}}})
        pol = load_llm_policy(PACK, self.tmp)
        self.assertTrue(pol["deployments"]["cloud"].get("enabled"))
        self.assertFalse(pol["deployments"]["local"].get("enabled"))

    def test_fail_closed_default_has_no_deployment(self):
        # 内置最严默认策略（文件缺失时回落）无任何可用档
        pol = load_llm_policy("no_such_pack", self.tmp)
        mode, _, _ = select_deployment(_ctx(), pol)
        self.assertEqual(mode, "off")


# ----------------------------------------------------------------------
# AC-9 审批桥接：ai_draft 提案 → 审批 → 结构化核查项
# ----------------------------------------------------------------------
class DraftBridgeTest(unittest.TestCase):
    OP = "王检察官"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1,
                              proposals_db=str(self.tmp / "proposals.duckdb"))
        self.client = TestClient(create_app(self.ctx))
        salt, h = hash_password("pw")
        from server.app.meta.models import User
        self.repo.create_user(User(operator=self.OP, password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        self.pool = WorkerPool(
            self.repo,
            {"factory": self.factory,
             "snapshot_base_for": self.svc.snapshot_ontology_root},
            max_workers=1, poll_interval=0.02, backoff_base=0.02)
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": self.OP, "password": "pw"})
        self.auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        self.repo.create_case(CaseRecord(id="c1", tenant_id="t1", name="草案案"))
        self.repo.transition_case("c1", CASE_ACTIVE, by="u")
        t = enqueue_task(self.repo, case_id="c1", task_type=TASK_BUILD,
                         created_by="u")
        handle_build(t, repo=self.repo, factory=self.factory,
                     snapshot_base_for=lambda cid: ROOT / "ontology",
                     builder=_stub_builder, auto_quality_after_build=False)

    def tearDown(self):
        self.pool.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _drain(self):
        self.pool.run_until_drained(max_idle_rounds=5)

    def _state(self):
        return StateStore("c1", self.factory.case_dir("c1") / "state.sqlite")

    def _submit_ai_draft(self, text: str) -> str:
        conn = duckdb.connect(self.ctx.proposals_db)
        try:
            return ProposalStore(conn).submit({
                "proposal_id": "pp-" + uuid.uuid4().hex[:12],
                "kind": "verify_item",
                "case_id": "c1",
                "author": "model:fake-model",
                "candidate": {"text": text, "clue_id": CLUE},
                "constraints": {},
                "input": {
                    "origin": "ai_draft", "purpose": "draft_verify",
                    "mode": "cloud", "model": "fake-model",
                    "channel": "external",
                    "ref_function": "",
                    "external": {"target": "海州市监", "material": "工商内档"},
                    "falsification": "登记信息一致",
                    "content_sha1": "deadbeef",
                },
            }, actor="agent:sunzi")
        finally:
            conn.close()

    def test_ai_draft_proposal_bridges_structured_fields(self):
        text = "外部调取张三名下工商内档核对股权代持"
        pid = self._submit_ai_draft(text)
        r = self.client.post(
            f"/api/v1/cases/c1/proposals/{pid}/decide",
            headers=self.auth, json={"decision": "approve", "reason": "采纳"})
        self.assertEqual(r.status_code, 200, r.text)
        task = self.repo.get_task(r.json()["data"]["task"]["id"])
        self.assertEqual(task.params["op"], "add_manual")
        self.assertEqual(task.params["operator"], self.OP)  # 审批人，非模型
        self.assertEqual(task.params["origin"], "ai_draft")
        self.assertEqual(task.params["channel"], "external")
        self.assertEqual(task.params["external"]["target"], "海州市监")
        self._drain()
        with self._state() as st:
            row = st.get_verify_item(
                f"vi_{StateStore.verify_item_key(CLUE, 'manual', text)}")
        self.assertIsNotNone(row)
        self.assertEqual(row["origin"], "ai_draft")
        self.assertEqual(row["channel"], "external")
        self.assertEqual(row["falsification"], "登记信息一致")
        self.assertEqual(row["external"]["target"], "海州市监")

    def test_manual_proposal_behavior_unchanged(self):
        text = "纯人工方向：走访同行核实报价"
        conn = duckdb.connect(self.ctx.proposals_db)
        try:
            pid = ProposalStore(conn).submit({
                "proposal_id": "pp-" + uuid.uuid4().hex[:12],
                "kind": "verify_item", "case_id": "c1",
                "author": "agent:sunzi",
                "candidate": {"text": text, "clue_id": CLUE},
            }, actor="agent:sunzi")
        finally:
            conn.close()
        r = self.client.post(
            f"/api/v1/cases/c1/proposals/{pid}/decide",
            headers=self.auth, json={"decision": "approve"})
        self.assertEqual(r.status_code, 200, r.text)
        task = self.repo.get_task(r.json()["data"]["task"]["id"])
        self.assertNotIn("origin", task.params)     # 桥接不注入 → Worker 落 manual
        self.assertNotIn("channel", task.params)
        self._drain()
        with self._state() as st:
            row = st.get_verify_item(
                f"vi_{StateStore.verify_item_key(CLUE, 'manual', text)}")
        self.assertEqual(row["origin"], "manual")
        self.assertEqual(row["channel"], "")

    def test_worker_rejects_illegal_origin(self):
        row = enqueue_task(
            self.repo, case_id="c1", task_type=TASK_VERIFY,
            params={"op": "add_manual", "clue_id": CLUE, "text": "x",
                    "operator": self.OP, "origin": "立案"},
            created_by=self.OP)
        with self.assertRaises(TaskExecError) as cm:
            handle_verify(row, repo=self.repo, factory=self.factory)
        self.assertEqual(cm.exception.code, "VERIFY_REJECTED")


def _stub_builder(conn, *, pack: str, base_dir: Path, progress) -> dict:
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    progress(90.0, "compile", "完成", "stub")
    return {"objects": {}, "links": {}, "skipped": []}


# ----------------------------------------------------------------------
# AC-8 API 端点：degraded 200 / 404 / fake 通道成功
# ----------------------------------------------------------------------
class DraftEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.ctx = WebContext(repo=self.repo, factory=self.factory,
                              cases=self.svc, session_ttl_hours=1,
                              proposals_db=str(self.tmp / "proposals.duckdb"))
        self.app = create_app(self.ctx)
        self.client = TestClient(self.app)
        salt, h = hash_password("pw")
        from server.app.meta.models import User
        self.repo.create_user(User(operator="王检察官", password_hash=h,
                                   salt=salt, role="human", clearance=4,
                                   tenant_id="t1"))
        r = self.client.post("/api/v1/auth/login",
                             json={"operator": "王检察官", "password": "pw"})
        self.auth = {"Authorization": f"Bearer {r.json()['data']['token']}"}
        # 建案 + 线索产物（assemble_detail 读产物；无需 state）
        rr = self.client.post("/api/v1/cases", headers=self.auth,
                              json={"case_id": "c1", "name": "草案案"})
        self.assertEqual(rr.status_code, 200, rr.text)
        self.repo.set_version("c1", 1, "test")
        save_case_clues(self.factory.case_dir("c1"), 1, [_clue()])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, clue_id: str = CLUE):
        return self.client.post(
            f"/api/v1/cases/c1/clues/{clue_id}/verify-items/draft",
            headers=self.auth)

    def test_degraded_returns_200_with_off_signal(self):
        # 显式注入 llm_enabled=false 策略：off 语义由策略注入决定，
        # 不依赖仓库内 default 包策略文件的业务开关（cloud 档可被环境启用）
        with patch("core.llm.draft_verify.load_llm_policy",
                   return_value=_policy(llm_enabled=False)):
            r = self._post()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertFalse(d["ok"])
        self.assertTrue(d["degraded"])
        self.assertEqual(d["mode"], "off")
        self.assertEqual(d["proposals"], [])
        # 零模型调用降级留痕（proposals 库 llm_call_log）
        conn = duckdb.connect(self.ctx.proposals_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM llm_call_log "
                "WHERE allowed = false").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(n, 1)

    def test_unknown_clue_404(self):
        r = self._post("clue-ghost")
        self.assertEqual(r.status_code, 404, r.text)

    def test_success_falls_into_proposal_queue(self):
        # 端点 context（_draft_context）不含 name 键（标题为自由文本，且
        # source_rows 明细不进 prompt）→ token_map 为空，token→真名 rehydrate
        # 往返由 core 层 test_success_strict_redaction_and_rehydrate 覆盖；
        # 此处断言端到端明文落提案队列 + shadow（不建 state.sqlite）
        fake = _FakeLLM({"items": [{
            "text": "约谈 当事人 核对进账对手方", "dimension": "资金",
            "channel": "function", "function": "call_frequency_spike"}]})
        # 端点内部缺省 LLMClient()/load_llm_policy()；patch 成 fake 通道 +
        # cloud 档可用 policy（生产接线同位：仅换客户端与策略注入源）
        with patch("core.llm.draft_verify.load_llm_policy",
                   return_value=_policy()), \
             patch("core.llm.draft_verify.LLMClient", return_value=fake):
            r = self._post()
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()["data"]
        self.assertTrue(d["ok"], d)
        self.assertEqual(len(d["proposals"]), 1)
        self.assertEqual(d["proposals"][0]["text"],
                         "约谈 当事人 核对进账对手方")
        # 提案落库 status=draft；state.sqlite 核查项不变（shadow）
        conn = duckdb.connect(self.ctx.proposals_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM proposal WHERE status = 'draft'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)
        # 影子隔离：LLM 草案零写穿核查项（state.sqlite 可能由详情读路径的
        # REQ-V-002 惰性供给确定性创建，与 LLM 无关；断言口径是核查项不变：
        # 无本草案文本、无 ai_draft 来源行）
        st_path = self.factory.case_dir("c1") / "state.sqlite"
        if st_path.exists():
            st = StateStore("c1", st_path)
            try:
                rows = st.list_verify_items(CLUE)
            finally:
                st.close()
            texts = {r.get("text") for r in rows}
            self.assertNotIn("约谈 当事人 核对进账对手方", texts)
            self.assertFalse(
                [r for r in rows if r.get("origin") == "ai_draft"])


if __name__ == "__main__":
    unittest.main()
