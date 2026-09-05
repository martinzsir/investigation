"""
core/disposal.py
线索处置状态管理：供正兵操作台调用，配套状态机 + DuckDB 持久化。

设计原则（红线）：
  - AI / 自动化只能把线索推到 待查/查证中/已排除/已固证
  - 「已立案」为受控终态，仅正兵显式调用 set_filed() 且须提供法定依据
  - 所有状态变更走 LineageClue.set_status / set_filed，禁止直接改 .status
    （保证 audit_log 审计链完整）

典型用法：
    from core.disposal import DisposalBoard

    board = DisposalBoard(store=store, clues=clues)
    board.transition("clue_abc123", "查证中", operator="正兵", note="已调取流水核实")
    board.exclude("clue_def456", operator="正兵", reason="系正常业务往来")
    board.confirm("clue_abc123", operator="正兵")
    board.file("clue_abc123", operator="王检察官", legal_basis="杭检立〔2026〕XX号")
    board.persist()                  # 落 DuckDB
    board.report()                   # 打印处置看板
"""
from __future__ import annotations

from core.registry import ClueStatus, LineageClue
from core.lineage import save_statuses, load_statuses


class DisposalBoard:
    """正兵处置看板：内存线索 + DuckDB 持久化的统一封装。

    所有状态变更经 ActionExecutor（actions.json 声明的唯一写路径）：
    角色校验 / 必填参数 / 状态机校验 / 副作用（含 obj_decision 决策对象）。
    """

    def __init__(self, clues: list[LineageClue], store=None, pack: str = "default",
                 health=None):
        self.clues: dict[str, LineageClue] = {c.clue_id: c for c in clues}
        self.store = store  # Store 实例（含 .conn），可选
        from core.action_executor import ActionExecutor
        self.executor = ActionExecutor(store, pack=pack, health=health)

    # ---- 查询 ----
    def get(self, clue_id: str) -> LineageClue:
        if clue_id not in self.clues:
            raise KeyError(f"未知线索 {clue_id}，已有 {list(self.clues)}")
        return self.clues[clue_id]

    def active(self) -> list[LineageClue]:
        """仍需正兵跟进的线索（排除 已排除/已立案 终态）。"""
        return [c for c in self.clues.values() if c.is_active()]

    def by_status(self, status: str) -> list[LineageClue]:
        return [c for c in self.clues.values() if c.status == status]

    # ---- 状态迁移（统一走 Action 执行器，保证审计链 + 声明式校验）----
    def transition(self, clue_id: str, target: str, operator: str = "正兵",
                   note: str = "") -> LineageClue:
        clue = self.get(clue_id)
        spec = self.executor.action_for_status(target)
        # exclude 的必填参数名为 reason；其余动作备注走 note
        params = {"reason": note} if target == ClueStatus.EXCLUDED else {"note": note}
        self.executor.execute(spec.name, clue, operator, params)
        return clue

    def verify(self, clue_id: str, operator: str = "正兵", note: str = "") -> LineageClue:
        """标记为 查证中。"""
        clue = self.get(clue_id)
        self.executor.execute("verify", clue, operator, {"note": note})
        return clue

    def exclude(self, clue_id: str, operator: str = "正兵", reason: str = "") -> LineageClue:
        """标记为 已排除（必须给理由，写进审计链）。"""
        clue = self.get(clue_id)
        self.executor.execute("exclude", clue, operator, {"reason": reason})
        return clue

    def confirm(self, clue_id: str, operator: str = "正兵", note: str = "") -> LineageClue:
        """标记为 已固证。"""
        clue = self.get(clue_id)
        self.executor.execute("confirm", clue, operator, {"note": note})
        return clue

    def file(self, clue_id: str, operator: str, legal_basis: str) -> LineageClue:
        """
        受控置位 已立案：仅 已固证 可迁移，须提供法定依据（案号/审批文号）。
        operator 应为具名正兵（检察官/侦查员），禁止传 "system"/"AI"；
        副作用创建 obj_decision 决策对象（含 legal_basis/operator/时间戳/溯源）。
        """
        clue = self.get(clue_id)
        self.executor.execute("file", clue, operator, {"legal_basis": legal_basis})
        return clue

    # ---- 持久化 ----
    def persist(self) -> int:
        """把当前处置状态批量写入 DuckDB。无 store 时静默跳过。"""
        if self.store is None or not hasattr(self.store, "conn"):
            return 0
        return save_statuses(self.store.conn, list(self.clues.values()))

    def restore(self) -> int:
        """从 DuckDB 回灌上次会话的处置进度。"""
        if self.store is None or not hasattr(self.store, "conn"):
            return 0
        return load_statuses(self.store.conn, list(self.clues.values()))

    # ---- 看板 ----
    def report(self) -> dict:
        from collections import Counter
        counts = Counter(c.status for c in self.clues.values())
        ordered = {s: counts.get(s, 0) for s in
                   [ClueStatus.PENDING, ClueStatus.VERIFYING,
                    ClueStatus.EXCLUDED, ClueStatus.CONFIRMED, ClueStatus.FILED]}
        return {
            "total": len(self.clues),
            "active": len(self.active()),
            "by_status": ordered,
        }

    def print_report(self) -> None:
        r = self.report()
        print("=== 线索处置看板 ===")
        print(f"总计 {r['total']} 条，仍需跟进 {r['active']} 条")
        for status, cnt in r["by_status"].items():
            print(f"  {status}: {cnt}")
        print("--- 详情 ---")
        for c in self.clues.values():
            print(f"  {c.clue_id} [{c.status}] {c.title}")
            if c.audit_log:
                last = c.audit_log[-1]
                print(f"        ↳ 最近: {last['operator']} {last['from_status']}→{last['to_status']} {last.get('note','')}")


def _cmd(args) -> None:
    """命令行入口（python -m core.disposal <action> ...）。"""
    import json
    from pathlib import Path
    from core.store import Store
    from core.lineage import lineage_report

    store = Store()
    path = Path("output/lineage_clues.json")
    if not path.exists():
        raise SystemExit("未找到 output/lineage_clues.json，请先运行 run_with_invoker.py")
    report = json.loads(path.read_text(encoding="utf-8"))
    clues = [LineageClue(**c) for c in report.get("clues", [])]
    board = DisposalBoard(clues, store=store)
    board.restore()

    action = args.action
    if action == "report":
        board.print_report()
    elif action in ("verify", "exclude", "confirm", "file"):
        cid = args.clue_id
        if action == "verify":
            board.verify(cid, note=args.note or "")
        elif action == "exclude":
            board.exclude(cid, reason=args.note or "（未填写理由）")
        elif action == "confirm":
            board.confirm(cid, note=args.note or "")
        elif action == "file":
            board.file(cid, operator=args.operator or "正兵", legal_basis=args.legal_basis or "")
        board.persist()
        print(f"✅ {cid} → {board.get(cid).status}")
    else:
        raise SystemExit(f"未知动作 {action}，可用: report / verify / exclude / confirm / file")


if __name__ == "__main__":
    import argparse
    from core.registry import LineageClue  # 供 _cmd 延迟 import 用
    ap = argparse.ArgumentParser(description="线索处置状态管理")
    ap.add_argument("action", choices=["report", "verify", "exclude", "confirm", "file"])
    ap.add_argument("--clue-id", dest="clue_id", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--operator", default="")
    ap.add_argument("--legal-basis", dest="legal_basis", default="")
    _cmd(ap.parse_args())
