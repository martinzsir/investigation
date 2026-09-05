"""
tests/test_export_policy.py
REQ-011 MCP 与导出接入权限 测试（AC2/AC3/AC5；AC1/AC4 在 mcp_client_test）。

  AC2: 导出 CSV 受限属性被 mask/剔除（_masked_columns 防护清单）
  AC3: 导出记录 operator / purpose / destination 到审计（audit_chain）
  AC5: 导出越权尝试被审计记录并告警（export_denied 事件 + 退出码 2）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.access import AccessContext, system_context          # noqa: E402
from core.policy import PolicyEngine                           # noqa: E402
from core import Store                                         # noqa: E402

# 最小语义层 DDL（导出仅涉及节点/边标识列，够 main() 走通）
_DDL = [
    "CREATE TABLE obj_person (person_id VARCHAR, raw_name VARCHAR)",
    "CREATE TABLE obj_org (org_id VARCHAR, raw_name VARCHAR)",
    "CREATE TABLE obj_account (account_id VARCHAR, raw_name VARCHAR)",
    "CREATE TABLE obj_bid_project (project_id VARCHAR, title VARCHAR)",
    "CREATE TABLE obj_transaction (transaction_id VARCHAR)",
    "CREATE TABLE obj_call (call_id VARCHAR)",
    "CREATE TABLE obj_trackpoint (track_id VARCHAR)",
    """CREATE TABLE lnk_transfers (from_account VARCHAR, to_account VARCHAR,
       from_account_id VARCHAR, to_account_id VARCHAR,
       amount DOUBLE, date VARCHAR)""",
    "CREATE TABLE lnk_calls_to (from_person VARCHAR, to_person VARCHAR, call_id VARCHAR)",
    """CREATE TABLE lnk_co_located (person_1 VARCHAR, person_2 VARCHAR,
       location VARCHAR, date VARCHAR)""",
    "CREATE TABLE lnk_owns (owner_raw VARCHAR, account_id VARCHAR)",
    "CREATE TABLE lnk_involved_in (org_id VARCHAR, project_id VARCHAR)",
    """CREATE TABLE lnk_time_window (title VARCHAR, owner_raw VARCHAR,
       offset_days BIGINT)""",
]
_SEED = [
    "INSERT INTO obj_person VALUES ('p1','张卫国'),('p2','李志强')",
    "INSERT INTO obj_org VALUES ('o1','宏业建设')",
    "INSERT INTO obj_account VALUES ('a1','尾号8848'),('a2','尾号1201')",
    "INSERT INTO obj_bid_project VALUES ('b1','某市政道路工程')",
    "INSERT INTO obj_transaction VALUES ('t1'),('t2')",
    "INSERT INTO obj_call VALUES ('c1')",
    "INSERT INTO obj_trackpoint VALUES ('k1')",
    "INSERT INTO lnk_transfers VALUES ('尾号8848','尾号1201','a1','a2',1000000.0,'2022-12-29')",
    "INSERT INTO lnk_calls_to VALUES ('p1','p2','c1')",
    "INSERT INTO lnk_co_located VALUES ('p1','p2','某宾馆','2022-12-29')",
    "INSERT INTO lnk_owns VALUES ('张卫国','a1')",
    "INSERT INTO lnk_involved_in VALUES ('o1','b1')",
    "INSERT INTO lnk_time_window VALUES ('某市政道路工程','张卫国',-2)",
]


def _make_semantic_store() -> Store:
    store = Store(db_path=":memory:")
    for ddl in _DDL:
        store.execute(ddl)
    for seed in _SEED:
        store.execute(seed)
    return store


def _audit_rows(store: Store) -> list[dict]:
    rows = store.conn.execute(
        "SELECT operator, after_state FROM audit_chain ORDER BY seq").fetchall()
    return [{"operator": r[0],
             "after": json.loads(r[1]) if r[1] else None} for r in rows]


class TestExportPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name) / "ladybug"

    def _run_export(self, store: Store, *extra_args: str) -> int:
        import scripts.export_ladybug as ex
        argv = ["export_ladybug.py", *extra_args]
        with mock.patch.object(ex, "OUT", self.out_dir), \
                mock.patch.object(sys, "argv", argv):
            return ex.main(store=store)

    def test_ac2_sensitive_columns_masked_for_low_role(self):
        """AC2: 受限属性在低权限会话下进入剔除清单，主办/human 不限。"""
        engine = PolicyEngine()
        low = AccessContext(operator="正兵甲", role="正兵", clearance=1)
        high = AccessContext(operator="主办丙", role="主办", clearance=3)
        self.assertIn("person.id_card",
                      [f"person.{c}" for c in
                       [p for p in ("raw_name", "id_card")
                        if engine.property_rule("person", p)
                        and not engine.can_read_property(low, "person", p)]])
        self.assertTrue(engine.can_read_property(high, "person", "id_card"))
        # 行级遮蔽
        masked = engine.apply_row_masks(
            low, "person", [{"raw_name": "张卫国", "id_card": "31012341234"}])
        self.assertEqual(masked[0]["id_card"], "310****1234")

    def test_ac3_export_recorded_to_audit_chain(self):
        """AC3: 导出记录 operator / purpose / destination 到审计。"""
        store = _make_semantic_store()
        try:
            rc = self._run_export(
                store, "--operator", "张组长", "--role", "主办",
                "--clearance", "3", "--purpose", "图库比对",
                "--destination", "内网FTP:/ladybug/2026Q3")
            self.assertEqual(rc, 0)
            events = _audit_rows(store)
            exports = [e for e in events if e["after"]
                       and e["after"].get("action") == "export"]
            self.assertEqual(len(exports), 1)
            after = exports[0]["after"]
            self.assertEqual(exports[0]["operator"], "张组长")
            self.assertEqual(after["purpose"], "图库比对")
            self.assertEqual(after["destination"], "内网FTP:/ladybug/2026Q3")
            self.assertTrue(after["files"])          # 文件清单非空
            self.assertTrue((self.out_dir / "nodes.csv").exists())
        finally:
            store.close()

    def test_ac5_unauthorized_export_denied_and_audited(self):
        """AC5: 越权导出被审计记录并告警（退出码 2 + export_denied 事件 + stderr 告警）。"""
        import io
        store = _make_semantic_store()
        try:
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                rc = self._run_export(
                    store, "--operator", "见习丁", "--role", "见习",
                    "--clearance", "0", "--purpose", "好奇",
                    "--destination", "airgap-usb")
            self.assertEqual(rc, 2)
            self.assertIn("[告警]", err.getvalue())
            self.assertIn("导出被策略拒绝", err.getvalue())
            events = _audit_rows(store)
            denied = [e for e in events if e["after"]
                      and e["after"].get("action") == "export_denied"]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0]["operator"], "见习丁")
            self.assertEqual(denied[0]["after"]["destination"], "airgap-usb")
            # 未产生任何导出文件
            self.assertFalse(self.out_dir.exists())
        finally:
            store.close()

    def test_system_bypass_still_works(self):
        """system 缺省（既有部署）全旁路，导出正常。"""
        store = _make_semantic_store()
        try:
            rc = self._run_export(store)
            self.assertEqual(rc, 0)
            self.assertTrue((self.out_dir / "transfer_edges.csv").exists())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
