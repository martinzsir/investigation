"""
tests/test_state_store.py
M2 阶段 F：D1 state.sqlite 骨架（决策 D-M2-2：只交骨架+迁移方案，不接写路径）。

断言：
  1. WAL 模式 + busy_timeout=5000；
  2. 四表齐备（audit_chain / action_request / clue_disposal_status /
     review_decision），audit_chain 与 DuckDB 版逐列同构；
  3. import_from_duckdb 幂等迁移演练：行数 + root_hash 一致，
     迁移后 chain_verify 逐条可验（签名算法复用 core）；
  4. 篡改可检出（chain_verify=False）；
  5. grep 门禁：state_store 仅被 store/ 与 tests/ 引用（无写路径接线）；
     sqlite3.connect 仅出现在 repo_sqlite.py 与 state_store.py。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import duckdb

from core.audit import AuditChain

from server.app.store.backend import StoreFactory
from server.app.store.state_store import StateStore


def _append(chain: AuditChain, operator: str, after: dict) -> str:
    return chain.append(operator=operator, before=None, after=after,
                        source_row_ids=["row-1"], ontology_version="ont-1.0")


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _duckdb_chain(self, name: str, n_events: int = 3):
        path = self.tmp / name
        conn = duckdb.connect(str(path))
        chain = AuditChain(conn, "c1")
        for i in range(n_events):
            _append(chain, "王检", {"status": "查证中", "i": i})
        return conn, chain

    # ---- 1/2：WAL + schema ----
    def test_wal_and_busy_timeout(self):
        st = StateStore("c1", self.tmp / "cases" / "c1" / "state.sqlite")
        try:
            mode = st._conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            timeout = st._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(int(timeout), 5000)
        finally:
            st.close()

    def test_schema_four_tables_and_isomorphic_columns(self):
        st = StateStore("c1", self.tmp / "cases" / "c1" / "state.sqlite")
        try:
            tables = {r[0] for r in st._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for t in ("audit_chain", "action_request",
                      "clue_disposal_status", "review_decision"):
                self.assertIn(t, tables)
            # 与 DuckDB audit_chain 逐列同构（列名与顺序一致）
            conn, _ = self._duckdb_chain("_iso.duckdb", 0)
            try:
                duck_cols = [d[0] for d in conn.execute(
                    "SELECT * FROM audit_chain LIMIT 0").description]
            finally:
                conn.close()
            state_cols = [r[1] for r in st._conn.execute(
                "PRAGMA table_info(audit_chain)").fetchall()]
            self.assertEqual(state_cols, duck_cols)
        finally:
            st.close()

    # ---- 3：幂等迁移演练 ----
    def test_import_from_duckdb_idempotent(self):
        conn, chain = self._duckdb_chain("c1.duckdb", 3)
        try:
            st = StateStore("c1", self.tmp / "cases" / "c1" / "state.sqlite")
            try:
                r1 = st.import_from_duckdb(conn)
                self.assertTrue(r1["counts_match"])
                self.assertTrue(r1["root_match"])
                self.assertEqual(r1["duckdb_count"], 3)
                self.assertEqual(r1["state_count"], 3)
                self.assertEqual(r1["duckdb_root_hash"], chain.root_hash())
                self.assertEqual(r1["state_root_hash"], chain.root_hash())
                self.assertTrue(st.chain_verify(),
                                "迁移后签名应逐条可验（算法复用 core）")
                # 幂等：重跑行数不变、根哈希不变
                r2 = st.import_from_duckdb(conn)
                self.assertEqual(r2["state_count"], 3)
                self.assertEqual(r2["state_root_hash"], r1["state_root_hash"])
            finally:
                st.close()
        finally:
            conn.close()

    def test_import_from_casestore_and_empty_chain(self):
        # CaseStore 入参形态（生产路径传 store 对象）
        factory = StoreFactory(cases_root=self.tmp / "cases")
        store = factory.for_case("c1", mode="write", version=1)
        try:
            chain = AuditChain(store.write_conn, "c1")
            _append(chain, "李检", {"status": "已排除"})
        finally:
            store.close()
        st = StateStore("c1", self.tmp / "cases" / "c1" / "state.sqlite")
        try:
            r = st.import_from_duckdb(factory.for_case("c1", mode="read",
                                                       version=1))
            self.assertTrue(r["counts_match"] and r["root_match"])
            self.assertEqual(r["state_count"], 1)
            self.assertTrue(st.chain_verify())
        finally:
            st.close()

        # 空链迁移：0 行 → 根哈希 genesis、counts_match
        conn, _ = self._duckdb_chain("empty.duckdb", 0)
        try:
            st2 = StateStore("c2", self.tmp / "cases" / "c2" / "state.sqlite")
            try:
                r = st2.import_from_duckdb(conn)
                self.assertEqual(r["state_count"], 0)
                self.assertEqual(r["state_root_hash"], "0" * 64)
                self.assertTrue(r["counts_match"] and r["root_match"])
            finally:
                st2.close()
        finally:
            conn.close()

    # ---- 4：篡改检出 ----
    def test_tamper_detected(self):
        conn, _ = self._duckdb_chain("c1.duckdb", 2)
        st = StateStore("c1", self.tmp / "cases" / "c1" / "state.sqlite")
        try:
            st.import_from_duckdb(conn)
            self.assertTrue(st.chain_verify())
            st._conn.execute(
                "UPDATE audit_chain SET after_state=? WHERE seq=1",
                ('{"status": "已立案"}',))
            self.assertFalse(st.chain_verify(), "篡改必须被 chain_verify 检出")
        finally:
            st.close()
        conn.close()

    # ---- 5：grep 门禁（M3 起 state 接进 Web 读写面）----
    def test_grep_gates_no_write_path_wiring(self):
        # 只匹配真实 import 语句（文档字符串提及不算）
        pat = re.compile(
            r"(from\s+[\w.]*state_store\s+import|import\s+[\w.]*state_store)")
        # M3 边界：state_store 可被 Web 数据面（store/ 实现 + routers/ 读面 +
        # worker/ 写面）与 tests/ 消费；唯一硬不变量是 core/ 不得依赖
        # state_store（依赖方向 server→core，core 对 Web state 零感知）。
        allowed_parents = {
            (ROOT / "server" / "app" / "store").resolve(),
            (ROOT / "server" / "app" / "routers").resolve(),
            (ROOT / "server" / "app" / "worker").resolve(),
        }
        hits = [str(f) for f in (ROOT / "server").rglob("*.py")
                if f.resolve().parent not in allowed_parents
                and pat.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(
            hits, [],
            f"state_store 仅限 store/routers/worker 与 tests 引用：{hits}")
        # core/ 永远不得依赖 state_store（依赖方向 server→core）
        core_hits = [str(f) for f in (ROOT / "core").rglob("*.py")
                     if pat.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(core_hits, [],
                         f"core/ 不得 import state_store：{core_hits}")
        # sqlite3.connect 越界门禁（与 test_audit_view 双保险）
        pat_sq = re.compile(r"sqlite3\.connect\(")
        allowed = {"repo_sqlite.py", "state_store.py"}
        unexpected = [f.name for f in (ROOT / "server").rglob("*.py")
                      if f.name not in allowed
                      and pat_sq.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
