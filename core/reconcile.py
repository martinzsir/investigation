"""
core/reconcile.py
回写对账 / 重试 / 死信（REQ-014）。

规则：
  - failed 记录按指数退避重试（1m→5m→30m→2h→8h），attempts 达 5 转 dead_letter
    并发 writeback.dead_letter 告警事件；
  - 409 冲突**不重试**，进人工队列（manual_409）；
  - pending_receipt（200 无业务号）超时后幂等重投（同幂等键，外部不会重复建单）；
  - 对账差异（本地 confirmed 但外部查无此单）**只报告、不自动覆盖**——
    防止外部系统短暂故障导致本地状态被错误回滚。

手动触发：Reconciler(conn, adapter).run_reconcile() → 返回结构化差异/处置报告。
"""
from __future__ import annotations

from datetime import datetime, timedelta

_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, _FMT)
    except ValueError:
        return None


class Reconciler:
    MAX_ATTEMPTS = 5
    BACKOFF_SECONDS = [60, 300, 1800, 7200, 28800]   # 指数退避
    PENDING_TIMEOUT_SEC = 3600                        # 待回执 1h 超时

    def __init__(self, conn, adapter):
        from core.outbox import Outbox
        from core.writeback import WritebackDispatcher
        self._conn = conn
        self.outbox = Outbox(conn)
        self.adapter = adapter
        self._disp = WritebackDispatcher(conn, adapter)

    # ---- 主入口 ----
    def run_reconcile(self, *, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        report = {"retried": [], "dead_lettered": [], "manual_409": [],
                  "reconfirmed": [], "skipped_backoff": [],
                  "pending_waiting": [], "discrepancies": []}

        # 1) 失败重试 / 死信 / 409 转人工
        for row in self.outbox.list_by_status("failed"):
            err = row.get("last_error") or ""
            ref = {"outbox_id": row["outbox_id"], "action_id": row["action_id"],
                   "attempts": row["attempts"], "last_error": err}
            if "409" in err:
                report["manual_409"].append(ref)                    # 不重试
                continue
            if row["attempts"] >= self.MAX_ATTEMPTS:
                self._dead_letter(row, err)
                report["dead_lettered"].append(ref)
                continue
            if not self._backoff_due(row, now):
                report["skipped_backoff"].append(ref)
                continue
            result = self._disp._send_one(row)
            report["retried"].append({**ref, "result": result})

        # 2) pending_receipt 超时（200 无业务号）→ 幂等重投
        for row in self.outbox.list_by_status("sent"):
            ref = {"outbox_id": row["outbox_id"], "action_id": row["action_id"]}
            last = _parse_ts(row.get("last_attempt_at") or row.get("sent_at"))
            if last and (now - last).total_seconds() < self.PENDING_TIMEOUT_SEC:
                report["pending_waiting"].append(ref)
                continue
            result = self._disp._send_one(row)
            report["retried"].append({**ref, "result": result,
                                      "attempts": row["attempts"]})

        # 3) 差异对账：本地 confirmed vs 外部台账（只报告，不覆盖）
        for row in self.outbox.list_by_status("confirmed"):
            ext_id = row.get("external_id")
            if not ext_id:
                continue
            st = self.adapter.fetch_status(ext_id)
            if st.state != "confirmed":
                report["discrepancies"].append({
                    "outbox_id": row["outbox_id"], "action_id": row["action_id"],
                    "external_id": ext_id, "external_state": st.state,
                    "local_state": "confirmed",
                    "note": "本地已确认但外部查无此单，只报告不自动回滚"})
        return report

    # ---- 内部 ----
    def _backoff_due(self, row: dict, now: datetime) -> bool:
        last = _parse_ts(row.get("last_attempt_at"))
        if last is None:
            return True
        idx = min(max(row["attempts"] - 1, 0), len(self.BACKOFF_SECONDS) - 1)
        return now >= last + timedelta(seconds=self.BACKOFF_SECONDS[idx])

    def _dead_letter(self, row: dict, error: str) -> None:
        self.outbox.mark_dead_letter(row["outbox_id"],
                                     f"重试 {self.MAX_ATTEMPTS} 次仍失败：{error}")
        self._disp._update_action_request(
            "SET status='dead_letter', writeback_status='dead_letter' "
            "WHERE action_id=?", [row["action_id"]])
        try:
            from core.event_bus import EventBus
            EventBus(self._conn).publish(
                "writeback.dead_letter",
                {"action_id": row["action_id"], "outbox_id": row["outbox_id"],
                 "attempts": row["attempts"], "last_error": error},
                actor="reconcile")
        except Exception:
            pass

    @staticmethod
    def render_report(report: dict) -> str:
        """渲染人工可读的差异/处置报告。"""
        lines = ["=== 回写对账报告 ===",
                 f"重试投递：{len(report['retried'])} 条",
                 f"转死信：{len(report['dead_lettered'])} 条",
                 f"409 转人工：{len(report['manual_409'])} 条",
                 f"退避等待中：{len(report['skipped_backoff'])} 条",
                 f"待回执等待中：{len(report['pending_waiting'])} 条",
                 f"对账差异：{len(report['discrepancies'])} 条（只报告不覆盖）"]
        for d in report["discrepancies"]:
            lines.append(f"  [差异] action={d['action_id']} "
                         f"external_id={d['external_id']} 外部状态={d['external_state']}")
        for m in report["manual_409"]:
            lines.append(f"  [人工] action={m['action_id']} 409 冲突需人工核查")
        for d in report["dead_lettered"]:
            lines.append(f"  [死信] action={d['action_id']} 重试{d['attempts']}次：{d['last_error']}")
        return "\n".join(lines)
