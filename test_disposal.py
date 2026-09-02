"""
test_disposal.py —— 处置状态状态机 / 持久化 / 红线校验 测试。

运行：python test_disposal.py
"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.registry import ClueStatus, ClueStatusMachine, LineageClue
from core.lineage import save_statuses, load_statuses
from core.disposal import DisposalBoard
from core.store import Store


def assert_eq(a, b, msg):
    if a != b:
        raise AssertionError(f"{msg}: {a!r} != {b!r}")
    print(f"  ✓ {msg}")


def test_state_machine():
    print("[1] 状态机合法/非法迁移")
    m = ClueStatusMachine
    assert_eq(m.can_transition(ClueStatus.PENDING, ClueStatus.VERIFYING), True, "待查→查证中 允许")
    assert_eq(m.can_transition(ClueStatus.PENDING, ClueStatus.CONFIRMED), True, "待查→已固证 允许")
    assert_eq(m.can_transition(ClueStatus.PENDING, ClueStatus.EXCLUDED), True, "待查→已排除 允许")
    assert_eq(m.can_transition(ClueStatus.VERIFYING, ClueStatus.CONFIRMED), True, "查证中→已固证 允许")
    assert_eq(m.can_transition(ClueStatus.EXCLUDED, ClueStatus.PENDING), True, "已排除→待查 可重开")
    assert_eq(m.can_transition(ClueStatus.PENDING, ClueStatus.FILED), False, "待查→已立案 禁止(须经固证)")
    assert_eq(m.can_transition(ClueStatus.CONFIRMED, ClueStatus.PENDING), False, "已固证→待查 禁止")
    assert_eq(m.can_transition(ClueStatus.FILED, ClueStatus.PENDING), False, "已立案 为终态")


def test_clue_set_status_and_audit():
    print("[2] LineageClue 状态变更 + 审计链")
    c = LineageClue(title="测试线索")
    c.set_status(ClueStatus.VERIFYING, operator="正兵", note="开始核查")
    c.set_status(ClueStatus.CONFIRMED, operator="正兵", note="流水比对成立")
    assert_eq(c.status, ClueStatus.CONFIRMED, "状态=已固证")
    assert_eq(len(c.audit_log), 2, "审计链 2 条")
    assert_eq(c.audit_log[-1]["operator"], "正兵", "审计记录操作人")

    # 红线：禁止经 set_status 直接置 已立案
    try:
        c.set_status(ClueStatus.FILED, operator="某人")
    except ValueError:
        print("  ✓ set_status(FILED) 被拦截")
    else:
        raise AssertionError("应拦截 已立案 经 set_status 设置")


def test_filed_needs_confirmed():
    print("[3] 已立案 前置条件：须 已固证")
    c = LineageClue(title="x")
    try:
        c.set_filed(operator="王检察官", legal_basis="XX号")
    except ValueError as e:
        print(f"  ✓ 未固证禁止立案：{e}")
    else:
        raise AssertionError("应拒绝")

    c.set_status(ClueStatus.VERIFYING)
    c.set_status(ClueStatus.CONFIRMED)
    c.set_filed(operator="王检察官", legal_basis="杭检立〔2026〕XX号")
    assert_eq(c.status, ClueStatus.FILED, "已固证后可立案")
    assert_eq("法定程序完备" in c.note, True, "法定依据入审计链")


def test_persist_and_restore(tmp_db=":memory:"):
    print("[4] DuckDB 持久化 / 回灌")
    store = Store(db_path=tmp_db)
    c1 = LineageClue(title="a"); c1.set_status(ClueStatus.VERIFYING)
    c2 = LineageClue(title="b"); c2.set_status(ClueStatus.CONFIRMED)
    clues = [c1, c2]
    n = save_statuses(store.conn, clues)
    assert_eq(n, 2, "写入 2 行")

    # 模拟新会话：新对象，从 DB 回灌
    fresh = [LineageClue(clue_id=c1.clue_id, title="a"),
             LineageClue(clue_id=c2.clue_id, title="b")]
    restored = load_statuses(store.conn, fresh)
    assert_eq(restored, 2, "回灌 2 条")
    assert_eq(fresh[0].status, ClueStatus.VERIFYING, "c1 状态恢复为 查证中")
    assert_eq(fresh[1].status, ClueStatus.CONFIRMED, "c2 状态恢复为 已固证")
    store.close()


def test_disposal_board():
    print("[5] DisposalBoard 便捷 API + 排除须给理由")
    c = LineageClue(title="demo")
    board = DisposalBoard([c])
    board.verify(c.clue_id, note="核查")
    board.confirm(c.clue_id)
    try:
        board.exclude(c.clue_id, reason="")
    except ValueError as e:
        print(f"  ✓ 已排除 必须填理由：{e}")
    else:
        raise AssertionError("应拒绝空理由")
    board.exclude(c.clue_id, reason="正常业务往来")
    assert_eq(board.get(c.clue_id).status, ClueStatus.EXCLUDED, "已排除 生效")
    r = board.report()
    assert_eq(r["by_status"][ClueStatus.EXCLUDED], 1, "看板统计正确")


if __name__ == "__main__":
    test_state_machine()
    test_clue_set_status_and_audit()
    test_filed_needs_confirmed()
    test_persist_and_restore()
    test_disposal_board()
    print("\n✅ 全部测试通过")
