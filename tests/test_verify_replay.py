"""
tests/test_verify_replay.py
REQ-V-016 核查方向 → 只读 Function 映射（playbook 唯一数据源 + 关键词兜底）
+ REQ-V-017 一键复跑回填（Worker op=replay / replay_json 列迁移 / 审计留痕）。

断言（实施方案 §4 P3 REQ-V-016 / REQ-V-017）：
  AC1 兜底表装载期校验：FALLBACK_RULES 引用的 function 全部存在于
      default 包 functions.json（load_pack 唯一权威面），py 实现额外
      ∈ core.functions.FUNCTION_IMPLS——对齐 ontology loader
      "缺失硬失败"纪律，由本组测试锁定（spec：兜底表由本需求测试校验）；
  AC2 主路径（playbook 唯一数据源）：suggested/ai_draft 项
      channel=function 且 ref_function 非空 → 直接映射 ref_function，
      fallback_function 反查手册（r6_fund_tw_rerun →
      integer_transfer_aggregates；未声明 = 空串）；
  AC3 兜底路径：manual/auto 项（ref_function 为空）按维度关键词映射
      （通讯/通话+频次 → call_frequency_spike、轨迹+同框 →
      co_located_pairs），表序即优先级；无命中 → None
      （REQ-V-017 端点据此回 NO_REPLAY_MAPPING，demo 口径：人工调取类
      文本不映射）；
  AC4 fail-closed：channel=external 不映射；ref_function 不在案件包
      functions.json（快照换包/函数下线/脏 ref_function）→ None；
      fallback_function 失效 → 降级空串不废主映射。

REQ-V-017 验收：
  V17-1 复跑后 status/conclusion 不变，replay_json 含溯源 source_row_ids；
  V17-2 Function 报错 → 任务 FAILED 不写任何字段；
  V17-3 无映射的核查项 → Worker NO_REPLAY_MAPPING（API 层 400 同码）；
  V17-4 复跑结论永不参与固证门禁判定（replay 列独立于 status，
        verify_progress 不受影响——由"裁决四列不动"断言覆盖）。
  另含：replay_json 旧库幂等补列、脏 JSON 投影 None 不抛。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.functions import FUNCTION_IMPLS
from core.ontology_loader import load_pack

from server.app.verify_functions_map import (
    FALLBACK_RULES,
    ReplayMapping,
    resolve_replay_mapping,
)

PACK_ROOT = ROOT / "ontology"  # 内核 default 包（与 test_verify_suggest 同例）


def _resolve(item: dict):
    return resolve_replay_mapping(item, pack_id="default", base_dir=PACK_ROOT)


# ----------------------------------------------------------------------
# AC1：兜底表装载期校验（缺失 = 测试硬失败）
# ----------------------------------------------------------------------
class FallbackTableAuditTest(unittest.TestCase):
    def setUp(self):
        self.specs = load_pack("default", base_dir=PACK_ROOT).functions

    def test_fallback_functions_declared_in_pack(self):
        for keywords, fn in FALLBACK_RULES:
            self.assertTrue(keywords, f"兜底表 {fn} 关键词组不能为空")
            self.assertIn(fn, self.specs,
                          f"兜底表 function 未在 default 包 functions.json "
                          f"声明：{fn}")

    def test_py_impls_registered(self):
        for _, fn in FALLBACK_RULES:
            spec = self.specs[fn]
            if spec.impl == "py":
                self.assertIn(fn, FUNCTION_IMPLS,
                              f"py Function 未在 FUNCTION_IMPLS 注册：{fn}")


# ----------------------------------------------------------------------
# AC2：主路径——playbook 唯一数据源（建议项/ai_draft 项同构）
# ----------------------------------------------------------------------
class PlaybookPathTest(unittest.TestCase):
    def test_suggested_function_item_maps_ref(self):
        m = _resolve({"channel": "function",
                      "ref_function": "time_window_collision",
                      "text": "复跑 张卫国 的整数资金时间窗核查"})
        self.assertIsInstance(m, ReplayMapping)
        self.assertEqual(m.function, "time_window_collision")
        # 反查 r6_fund_tw_rerun 声明的 fallback
        self.assertEqual(m.fallback_function, "integer_transfer_aggregates")
        self.assertEqual(m.source, "playbook")

    def test_ai_draft_item_same_path(self):
        m = _resolve({"origin": "ai_draft", "channel": "function",
                      "ref_function": "call_frequency_spike",
                      "text": "补查 通话频次 突增"})
        self.assertEqual(m.function, "call_frequency_spike")
        self.assertEqual(m.source, "playbook")
        # r6_call_window 未声明 fallback_function → 空串
        self.assertEqual(m.fallback_function, "")

    def test_external_item_no_mapping(self):
        self.assertIsNone(_resolve({"channel": "external", "ref_function": "",
                                    "text": "向住建局招标办调取招投标底档"}))


# ----------------------------------------------------------------------
# AC3：兜底路径——manual/auto 项维度关键词映射
# ----------------------------------------------------------------------
class KeywordFallbackTest(unittest.TestCase):
    def test_comm_freq_keyword(self):
        m = _resolve({"channel": "", "ref_function": "",
                      "text": "补查 张卫国 与中标方联系人的通话频次是否突增"})
        self.assertEqual(m.function, "call_frequency_spike")
        self.assertEqual(m.source, "fallback")
        self.assertEqual(m.fallback_function, "")

    def test_track_colocated_keyword(self):
        m = _resolve({"channel": "", "ref_function": "",
                      "text": "核查 张卫国 与 李志强 在中标窗口期是否轨迹同框"})
        self.assertEqual(m.function, "co_located_pairs")

    def test_first_rule_wins_on_multi_match(self):
        m = _resolve({"channel": "", "ref_function": "",
                      "text": "通讯频次与轨迹同框一并核查"})
        self.assertEqual(m.function, "call_frequency_spike")

    def test_no_match_returns_none(self):
        # demo 口径：人工调取类文本无库内可复跑方向 → NO_REPLAY_MAPPING
        self.assertIsNone(_resolve({"channel": "", "ref_function": "",
                                    "text": "调取 张卫国 账户流水并核对进账对手方"}))


# ----------------------------------------------------------------------
# AC4：fail-closed——不在案件包 functions.json 的引用不带病映射
# ----------------------------------------------------------------------
class FailClosedTest(unittest.TestCase):
    def test_unknown_ref_function_returns_none(self):
        self.assertIsNone(_resolve({"channel": "function",
                                    "ref_function": "ghost_fn", "text": "x"}))

    def test_stale_fallback_downgrades_to_empty(self):
        # fallback_function 引用失效（换包/函数下线）→ 降级空串，主映射保留
        pbs = [{"id": "p1", "channel": "function",
                "function": "time_window_collision",
                "fallback_function": "ghost_fn",
                "text": "复跑 {subject}（{project_count}）"}]
        with patch("server.app.verify_functions_map.load_verify_playbooks",
                   return_value=pbs):
            m = _resolve({"channel": "function",
                          "ref_function": "time_window_collision",
                          "text": "复跑 张卫国（8）"})
        self.assertEqual(m.function, "time_window_collision")
        self.assertEqual(m.fallback_function, "")

    def test_playbook_fallback_resolved_when_valid(self):
        pbs = [{"id": "p1", "channel": "function",
                "function": "time_window_collision",
                "fallback_function": "integer_transfer_aggregates",
                "text": "复跑 {subject}（{project_count}）"}]
        with patch("server.app.verify_functions_map.load_verify_playbooks",
                   return_value=pbs):
            m = _resolve({"channel": "function",
                          "ref_function": "time_window_collision",
                          "text": "复跑 张卫国（8）"})
        self.assertEqual(m.fallback_function, "integer_transfer_aggregates")


# ======================================================================
# REQ-V-017：一键复跑回填（StateStore replay_json 列 + Worker op=replay）
# ======================================================================

_REPLAY_SEED = {"kind": "suggested", "origin": "suggested", "status": "建议",
                "channel": "function",
                "ref_function": "integer_transfer_aggregates",
                "text": "复跑 张卫国 的整数转账聚合"}


class StateStoreReplayColumnTest(unittest.TestCase):
    """replay_json 列：回填往返 / 裁决四列不动 / 旧库幂等补列 / 脏 JSON。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "state.sqlite"
        from server.app.store.state_store import StateStore
        self.StateStore = StateStore
        self.state = StateStore("c1", self.path)
        self.state.upsert_verify_items("c1", "clue-1", [dict(_REPLAY_SEED)])
        self.item_id = f"vi_{self.state.verify_item_key('clue-1', 'suggested', _REPLAY_SEED['text'])}"

    def tearDown(self):
        self.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replay_roundtrip_keeps_verdict_columns(self):
        # 先走一次人工裁决（终态 + 署名），复跑不得触碰这四列
        self.state.transition_verify_item(
            self.item_id, status="已证实", conclusion="流水时间耦合",
            operator="王检察官", updated_at="2026-09-12 10:00:00")
        replay = {"function": "integer_transfer_aggregates",
                  "source_row_ids": [self.item_id], "degraded": True,
                  "result": None, "replayed_at": "2026-09-12 11:00:00"}
        updated = self.state.set_verify_item_replay(self.item_id, replay)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["replay"]["function"],
                         "integer_transfer_aggregates")
        self.assertEqual(updated["replay"]["source_row_ids"], [self.item_id])
        self.assertEqual(updated["replay"]["degraded"], True)
        # V17-1：裁决四列不动
        self.assertEqual(updated["status"], "已证实")
        self.assertEqual(updated["conclusion"], "流水时间耦合")
        self.assertEqual(updated["operator"], "王检察官")
        self.assertEqual(updated["updated_at"], "2026-09-12 10:00:00")
        # V17-4：复跑结论不进门禁——progress 仍按 status 统计
        self.assertEqual(self.state.verify_progress("clue-1")["concluded"], 1)

    def test_set_replay_missing_item_returns_none(self):
        self.assertIsNone(
            self.state.set_verify_item_replay("vi_ghost", {"x": 1}))

    def test_legacy_db_without_replay_column_migrates(self):
        """REQ-V-018 前旧库（无 replay_json 列）打开幂等补列、复跑可用。"""
        self.state.close()
        legacy = self.tmp / "legacy.sqlite"
        conn = sqlite3.connect(str(legacy))
        conn.executescript("""
            CREATE TABLE clue_verify_item (
                item_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                clue_id TEXT NOT NULL, item_key TEXT NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL DEFAULT '待核查',
                conclusion TEXT NOT NULL DEFAULT '',
                operator TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(clue_id, item_key));
            INSERT INTO clue_verify_item (item_id, case_id, clue_id, item_key,
                kind, text) VALUES ('vi_old', 'c1', 'clue-1', 'k1',
                'manual', '旧库存量核查项');
        """)
        conn.commit()
        conn.close()
        st = self.StateStore("c1", legacy)
        try:
            row = st.get_verify_item("vi_old")
            self.assertIsNotNone(row)
            self.assertIsNone(row["replay"])  # 存量项未复跑 → None
            updated = st.set_verify_item_replay(
                "vi_old", {"function": "call_frequency_spike"})
            self.assertEqual(updated["replay"]["function"],
                             "call_frequency_spike")
        finally:
            st.close()

    def test_corrupt_replay_json_projects_none(self):
        self.state.set_verify_item_replay(self.item_id, {"ok": True})
        self.state.close()
        # 直接把列写坏（模拟外部损坏），读面投影 None 不抛
        conn = sqlite3.connect(str(self.path))
        conn.execute("UPDATE clue_verify_item SET replay_json='{oops' "
                     "WHERE item_id=?", [self.item_id])
        conn.commit()
        conn.close()
        self.state = self.StateStore("c1", self.path)
        self.assertIsNone(self.state.get_verify_item(self.item_id)["replay"])


class WorkerReplayTest(unittest.TestCase):
    """op=replay 执行体：回填/降级/错误码/审计链（不产版本文件）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        from server.app.cases import CaseService
        from server.app.meta.repo_sqlite import SqliteMetaRepo
        from server.app.store import StoreFactory
        self.repo = SqliteMetaRepo(self.tmp / "meta.db")
        self.factory = StoreFactory(cases_root=self.tmp / "cases",
                                    meta=self.repo)
        self.svc = CaseService(self.repo, self.factory,
                               cases_root=self.tmp / "cases")
        self.svc.create_case(case_id="c1", name="复跑测试案",
                             tenant_id="t1", created_by="王检察官")
        self.repo.set_version("c1", 1, "test")
        # read 模式要求版本文件存在：write 打开一次创建空库（无 obj_* 表）。
        # CaseStore 连接是惰性打开的（首次触碰 read_conn/write_conn 才连接），
        # 仅 close() 不会落盘——须显式触碰 write_conn 触发建文件。
        with self.factory.for_case("c1", mode="write", version=1) as st:
            st.write_conn.execute("SELECT 1")
        self.state = None
        self._seed("clue-1", [dict(_REPLAY_SEED)])
        self.item_id = self._find_item("clue-1", _REPLAY_SEED["text"])

    def tearDown(self):
        if self.state is not None:
            self.state.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 辅助 ----
    def _seed(self, clue_id: str, items: list[dict]) -> None:
        self._open_state().upsert_verify_items("c1", clue_id, items)

    def _open_state(self):
        from server.app.store.state_store import StateStore
        if self.state is None:
            self.state = StateStore(
                "c1", self.factory.case_dir("c1") / "state.sqlite")
        return self.state

    def _find_item(self, clue_id: str, text: str) -> str:
        for row in self._open_state().list_verify_items(clue_id):
            if row["text"] == text:
                return row["item_id"]
        raise AssertionError(f"未播种到核查项：{text}")

    def _handle(self, item_id: str, *, clue_id: str = "clue-1",
                operator: str = "王检察官") -> dict:
        from server.app.meta.models import TaskRow
        from server.app.worker.verify import handle_verify
        task = TaskRow(id="t_replay", case_id="c1", task_type="VERIFY",
                       params={"op": "replay", "clue_id": clue_id,
                               "item_id": item_id, "operator": operator,
                               "role": "human", "clearance": 4},
                       idem_key="k1", created_by=operator)
        return handle_verify(task, repo=self.repo, factory=self.factory,
                             snapshot_base_for=self.svc.snapshot_ontology_root)

    def _item_row(self) -> dict:
        return self._open_state().get_verify_item(self.item_id)

    # ---- V17-1：回填 + 状态不动 + 溯源 + 审计链 ----
    def test_replay_backfills_and_keeps_status(self):
        before = self._item_row()
        out = self._handle(self.item_id)
        self.assertEqual(out["op"], "replay")
        self.assertEqual(out["version"], 1)
        replay = self._item_row()["replay"]
        self.assertIsNotNone(replay)
        # 空库（无 obj_transaction）→ SQL Function 结构降级，任务不失败
        self.assertTrue(replay["degraded"])
        self.assertEqual(replay["function"], "integer_transfer_aggregates")
        self.assertEqual(replay["mapping_source"], "playbook")
        self.assertEqual(replay["source_row_ids"], [self.item_id])
        self.assertEqual(replay["version"], "v1")
        self.assertEqual(replay["operator"], "王检察官")
        self.assertIn("replayed_at", replay)
        self.assertIn("params_used", replay)
        # 裁决四列不动
        after = self._item_row()
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["conclusion"], before["conclusion"])
        self.assertEqual(after["operator"], before["operator"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        # 审计链留痕且可整链校验
        self.assertTrue(self._open_state().chain_verify())
        self.assertEqual(self._open_state().event_count(), 1)

    def test_replay_is_repeatable(self):
        """复跑允许重复执行：再次覆写 replay_json，审计链追加不报幂等冲突。"""
        self._handle(self.item_id)
        first_at = self._item_row()["replay"]["replayed_at"]
        self._handle(self.item_id)
        self.assertEqual(self._open_state().event_count(), 2)
        self.assertIsInstance(first_at, str)

    # ---- V17-3：无映射 → NO_REPLAY_MAPPING，不写任何字段 ----
    def test_no_mapping_fails_without_write(self):
        self._seed("clue-1", [{"kind": "manual", "text":
                               "调取 张卫国 账户流水并核对进账对手方"}])
        manual_id = self._find_item("clue-1",
                                    "调取 张卫国 账户流水并核对进账对手方")
        from server.app.worker.tasks import TaskExecError
        with self.assertRaises(TaskExecError) as cm:
            self._handle(manual_id)
        self.assertEqual(cm.exception.code, "NO_REPLAY_MAPPING")
        self.assertIsNone(self._open_state().get_verify_item(manual_id)
                          ["replay"])

    # ---- 归属/存在性：跨线索与不存在一律 ITEM_NOT_FOUND ----
    def test_item_not_found(self):
        from server.app.worker.tasks import TaskExecError
        with self.assertRaises(TaskExecError) as cm:
            self._handle("vi_ghost")
        self.assertEqual(cm.exception.code, "ITEM_NOT_FOUND")

    def test_cross_clue_item_rejected(self):
        self._seed("clue-2", [dict(_REPLAY_SEED)])
        other = self._find_item("clue-2", _REPLAY_SEED["text"])
        from server.app.worker.tasks import TaskExecError
        with self.assertRaises(TaskExecError) as cm:
            self._handle(other)  # clue_id 缺省 clue-1，核查项在 clue-2
        self.assertEqual(cm.exception.code, "ITEM_NOT_FOUND")

    # ---- V17-2：Function 报错 → REPLAY_FAILED，不写任何字段 ----
    def test_function_error_fails_without_write(self):
        with patch("core.functions.FunctionExecutor") as fake_fx:
            fake_fx.return_value.invoke.side_effect = RuntimeError("boom")
            from server.app.worker.tasks import TaskExecError
            with self.assertRaises(TaskExecError) as cm:
                self._handle(self.item_id)
            self.assertEqual(cm.exception.code, "REPLAY_FAILED")
        self.assertIsNone(self._item_row()["replay"])
        self.assertEqual(self._open_state().event_count(), 0)

    # ---- 主跑失败 → 备选接管（fallback_used 留痕）----
    def test_primary_failure_falls_back(self):
        item = {"kind": "suggested", "origin": "suggested", "status": "建议",
                "channel": "function", "ref_function": "time_window_collision",
                "text": "复跑 张卫国 的时间窗碰撞"}
        self._seed("clue-1", [item])
        tw_id = self._find_item("clue-1", item["text"])
        ok_out = {"function": "integer_transfer_aggregates",
                  "output_type": "rows", "rows": [], "readonly": True,
                  "params_used": {"round_unit": 10000}}
        with patch("core.functions.FunctionExecutor") as fake_fx:
            fake_fx.return_value.invoke.side_effect = [
                RuntimeError("primary down"), ok_out]
            out = self._handle(tw_id)
        self.assertTrue(out["fallback_used"])
        self.assertEqual(out["function"], "integer_transfer_aggregates")
        replay = self._open_state().get_verify_item(tw_id)["replay"]
        self.assertTrue(replay["fallback_used"])
        self.assertEqual(replay["mapping_source"], "playbook")
        self.assertEqual(replay["params_used"], {"round_unit": 10000})


if __name__ == "__main__":
    unittest.main()
