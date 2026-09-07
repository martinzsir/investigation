"""
tests/test_state_sink.py
M3 阶段 A：D1 写路径接线（决策 D-M3-1）——core ActionExecutor/DisposalBoard/
AuditChain 经 StateSink 窄协议写 per-case state.sqlite。

红线断言：
  - 五动作在 sqlite 后端跑通，审计链落 state.audit_chain 且签名逐条可验（AC-6）；
  - file 三条红线（human 角色 / legal_basis 必填 / 占位 operator）在 sqlite
    后端同等生效——校验逻辑单点在 core，Web 与 CLI 共用；
  - 状态机非法迁移拒绝；
  - 决策副作用落 state.review_decision（不写版本文件 obj_decision）；
  - save_statuses/load_statuses 在 sqlite 上方言兼容（UPSERT）；
  - sink=None 缺省 DuckDB 路径行为不变（锚）；
  - grep 门禁：core/ 不 import state_sink/state_store/server（依赖方向 server→core）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core import Store                                   # noqa: E402
from core.access import AccessContext                   # noqa: E402
from core.audit import AuditChain                       # noqa: E402
from core.disposal import DisposalBoard                 # noqa: E402
from core.registry import ClueStatus, LineageClue       # noqa: E402

from server.app.store.state_sink import StateSink       # noqa: E402
from server.app.store.state_store import StateStore     # noqa: E402


def _clue(cid: str, title: str = "测试线索") -> LineageClue:
    return LineageClue(clue_id=cid, skill_id="xu_shi", title=title,
                       source_rows=[{"file": "b.parquet", "row_id": 1}])


class TestStateSink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case_dir = Path(self.tmp.name) / "cases" / "c1"
        self.state = StateStore("c1", self.case_dir / "state.sqlite")
        self.sink = StateSink(self.state, ontology_version="v3")
        self.human = AccessContext(operator="王检察官", role="human",
                                   clearance=4, network="web",
                                   purpose="线索处置")
        self.soldier = AccessContext(operator="李侦查员", role="正兵",
                                     clearance=1, network="web")

    def tearDown(self):
        self.state.close()

    def _board(self, clues, access) -> DisposalBoard:
        return DisposalBoard(clues, store=self.sink, pack="default",
                             sink=self.sink, access=access)

    # ---- 审计链落 sqlite 且签名可验 ----
    def test_verify_writes_persistent_chain(self):
        board = self._board([_clue("c1")], self.human)
        board.verify("c1", operator="王检察官", note="接手核查")
        # AC-6：持久链有事件（非仅内存日志）
        self.assertEqual(self.state.event_count(), 1)
        self.assertTrue(self.state.chain_verify(),
                        "sqlite 审计链签名必须逐条可验（复用 core 签名算法）")
        chain = AuditChain.readonly(self.sink.conn, case_id="c1",
                                    backend="sqlite")
        self.assertTrue(chain.chain_verify())
        tl = chain.timeline(action="disposal")
        self.assertEqual(tl["total"], 1)
        ev = tl["items"][0]
        self.assertEqual(ev["operator"], "王检察官")
        self.assertEqual(ev["status_from"], ClueStatus.PENDING)
        self.assertEqual(ev["status_to"], ClueStatus.VERIFYING)
        self.assertEqual(ev["ontology_version"], "v3",
                         "sqlite 后端版本锚点取构造参数（state 无 meta 表）")

    # ---- 全生命周期到已立案 ----
    def test_full_lifecycle_to_file(self):
        board = self._board([_clue("c1", "大额取现")], self.human)
        board.verify("c1", operator="王检察官")
        board.confirm("c1", operator="王检察官", note="证据固定")
        board.file("c1", operator="王检察官", legal_basis="海州检刑立〔2026〕12号")
        board.persist()
        self.assertEqual(self.state.event_count(), 3)
        self.assertTrue(self.state.chain_verify())
        # 决策副作用落 state.review_decision（非版本文件 obj_decision）
        decisions = self.state.list_decisions(target_id="c1")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["verdict"], ClueStatus.FILED)
        self.assertEqual(decisions[0]["decided_by"], "王检察官")
        self.assertIn("海州检刑立", decisions[0]["payload"]["legal_basis"])
        # 处置状态落 state.clue_disposal_status
        row = self.sink.conn.execute(
            "SELECT status, operator FROM clue_disposal_status "
            "WHERE clue_id=?", ["c1"]).fetchone()
        self.assertEqual(row[0], ClueStatus.FILED)
        self.assertEqual(row[1], "王检察官")

    # ---- file 红线 1：正兵角色拒绝（human 专属终态）----
    def test_file_redline_soldier_denied(self):
        board = self._board([_clue("c1")], self.soldier)
        board.verify("c1", operator="李侦查员")
        board.confirm("c1", operator="李侦查员")
        with self.assertRaises(PermissionError):
            board.file("c1", operator="李侦查员",
                       legal_basis="某文号")
        # 被拒动作不得落链
        self.assertEqual(self.state.event_count(), 2)

    # ---- file 红线 2：缺 legal_basis 拒绝 ----
    def test_file_redline_missing_legal_basis(self):
        board = self._board([_clue("c1")], self.human)
        board.verify("c1", operator="王检察官")
        board.confirm("c1", operator="王检察官")
        with self.assertRaises(ValueError):
            board.executor.execute("file", board.get("c1"), "王检察官", {})

    # ---- file 红线 3：占位 operator 拒绝 ----
    def test_file_redline_placeholder_operator(self):
        board = self._board([_clue("c1")], self.human)
        board.verify("c1", operator="王检察官")
        board.confirm("c1", operator="王检察官")
        for bad in ("system", "ai", "assistant", "agent:x"):
            with self.assertRaises((ValueError, PermissionError)):
                board.executor.execute(
                    "file", board.get("c1"), bad,
                    {"legal_basis": "某文号"})

    # ---- 状态机：跳过中间态拒绝 ----
    def test_state_machine_skip_denied(self):
        board = self._board([_clue("c1")], self.human)
        with self.assertRaises(ValueError):
            board.file("c1", operator="王检察官",
                       legal_basis="某文号")  # 待查 → 已立案 非法

    # ---- exclude 必须带理由（params 校验在 core）----
    def test_exclude_requires_reason(self):
        board = self._board([_clue("c1")], self.human)
        with self.assertRaises(ValueError):
            board.exclude("c1", operator="王检察官", reason="")

    # ---- sqlite 状态持久化方言兼容（UPSERT + 回灌）----
    def test_persist_and_restore_sqlite(self):
        board = self._board([_clue("c1"), _clue("c2")], self.human)
        board.verify("c1", operator="王检察官", note="核查中")
        board.exclude("c2", operator="王检察官", reason="经查不实")
        board.persist()
        # 新看板从 state 回灌
        fresh = self._board([_clue("c1"), _clue("c2")], self.human)
        n = fresh.restore()
        self.assertEqual(n, 2)
        self.assertEqual(fresh.get("c1").status, ClueStatus.VERIFYING)
        self.assertEqual(fresh.get("c2").status, ClueStatus.EXCLUDED)
        # 幂等重跑 persist 不报错、行数不变
        self.assertEqual(board.persist(), 2)

    # ---- sink=None 缺省 DuckDB 路径零变化（锚）----
    def test_default_duckdb_path_unchanged(self):
        store = Store(db_path=":memory:")
        try:
            board = DisposalBoard([_clue("d1")], store=store, pack="default")
            self.assertIsNone(board.executor.sink)
            board.verify("d1", operator="王检察官")
            board.persist()
            chain = AuditChain(store.conn)
            self.assertEqual(chain.count(), 1)
            self.assertTrue(chain.chain_verify())
            # DuckDB 后端无版本锚点 → unknown 回落（既有语义）
            self.assertEqual(chain.current_ontology_version(), "unknown")
        finally:
            store.close()

    # ---- 双后端签名算法等价（同输入同签名）----
    def test_backend_signature_equivalence(self):
        from core.audit import _compute_signature, _GENESIS_HASH
        args = dict(
            event_id="evt_test_1", case_id="c1", ontology_version="v3",
            rule_version=None, function_version=None, params_hash=None,
            source_row_ids=["r1"], operator="王检察官",
            before={"status": ClueStatus.PENDING},
            after={"status": ClueStatus.VERIFYING, "note": "n"},
            prev_hash=_GENESIS_HASH)
        sig = _compute_signature(**args)
        # sqlite 落库后用 core 算法重算一致（chain_verify 已证）；
        # 此处直接断言迁移演练链路：duckdb 写 → import 到 state → root 一致
        store = Store(db_path=":memory:")
        try:
            dchain = AuditChain(store.conn, case_id="c1",
                                ontology_version="v3")
            dchain.append(operator="王检察官",
                          before={"status": ClueStatus.PENDING},
                          after={"status": ClueStatus.VERIFYING},
                          source_row_ids=["r1"], ontology_version="v3")
            r = self.state.import_from_duckdb(store)
            self.assertTrue(r["root_match"])
            self.assertTrue(self.state.chain_verify())
            self.assertEqual(self.state.root_hash(), dchain.root_hash())
        finally:
            store.close()
        self.assertEqual(len(sig), 64)

    # ---- grep 门禁：core/ 不得依赖 server 实现 ----
    def test_core_does_not_import_server(self):
        core_dir = ROOT / "core"
        violations = []
        for py in core_dir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if ("state_sink" in line or "state_store" in line
                        or "from server" in line or "import server" in line):
                    violations.append(f"{py.name}:{i}: {stripped}")
        self.assertEqual(violations, [],
                         f"core/ 不得 import server 状态层（依赖方向 server→core）："
                         f"{violations}")


if __name__ == "__main__":
    unittest.main()
