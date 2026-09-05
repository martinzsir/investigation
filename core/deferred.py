"""
core/deferred.py
defer 回捞机制（REQ-017）：暂缓候选带着 wake_conditions 沉底，条件满足自动唤醒重入 review。

三类唤醒条件（OR 语义，满足其一即唤醒）：
  {"on_dataset": "通话记录"}        新数据集/分区到达（source.partition.arrived 事件）
  {"after": "2026-10-01"}           TTL：到期唤醒（scan_due 时间扫描，或事件时间晚于该日）
  {"evidence_count_gte": 3}         新证据计数达到阈值（事件 payload evidence_count）

红线：
  - defer 必须带 wake_conditions，否则 ValueError（AC1）——没条件的暂缓等于丢线索；
  - 已唤醒任务不重复唤醒（AC4）：status=waiting 才参与匹配；
  - 唤醒只改任务状态并发 review.deferred 事件，证据/原决策不删不改；
  - 回捞率 = 唤醒后最终 decided / 总 deferred（AC5）。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

_DDL = """
CREATE TABLE IF NOT EXISTS deferred_task (
    task_id VARCHAR PRIMARY KEY,
    candidate_id VARCHAR NOT NULL,
    entity_type VARCHAR,
    canonical VARCHAR,
    wake_conditions_json VARCHAR NOT NULL,
    scheduled_at VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'waiting',
    created_by VARCHAR,
    created_at VARCHAR,
    woken_at VARCHAR,
    wake_reason VARCHAR,
    decided_at VARCHAR
)
"""

_WAITING = "waiting"
_WOKEN = "woken"
_EXPIRED = "expired"
# REQ-G-005：唤醒条件存在但无法解析（如 evidence_count 非数值）——区别于沉睡 waiting，
# 转 condition_error 进人审，避免"条件不满足"假象导致任务永久沉睡。
_CONDITION_ERROR = "condition_error"

_VALID_COND_KEYS = {"on_dataset", "after", "evidence_count_gte"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class DeferredTask:
    task_id: str
    candidate_id: str
    wake_conditions: dict
    scheduled_at: str
    status: str = _WAITING
    entity_type: str = ""
    canonical: str = ""
    created_by: str = ""
    woken_at: str | None = None
    wake_reason: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "DeferredTask":
        return cls(
            task_id=row["task_id"], candidate_id=row["candidate_id"],
            entity_type=row.get("entity_type") or "",
            canonical=row.get("canonical") or "",
            wake_conditions=json.loads(row["wake_conditions_json"]),
            scheduled_at=row["scheduled_at"], status=row["status"],
            created_by=row.get("created_by") or "",
            woken_at=row.get("woken_at"), wake_reason=row.get("wake_reason"))


def validate_wake_conditions(conds: dict) -> None:
    """wake_conditions 必须非空且仅含受支持条件键。"""
    if not conds:
        raise ValueError(
            "defer 必须提供 wake_conditions（on_dataset / after / evidence_count_gte），"
            "无条件暂缓等于线索丢池（REQ-017 AC1）")
    unknown = set(conds) - _VALID_COND_KEYS
    if unknown:
        raise ValueError(f"不支持的唤醒条件：{sorted(unknown)}")
    if "after" in conds:
        try:
            datetime.strptime(str(conds["after"])[:10], "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"after 条件须为 YYYY-MM-DD：{conds['after']!r}")
    # REQ-G-005：登记时就把阈值类型校验掉（畸形条件不入池）
    if "evidence_count_gte" in conds:
        try:
            int(conds["evidence_count_gte"])
        except (TypeError, ValueError):
            raise ValueError(
                f"evidence_count_gte 须为整数：{conds['evidence_count_gte']!r}")


def match_wake(task: DeferredTask, event) -> bool:
    """任务条件是否被事件满足（不修改任务状态）。

    event：core.event_bus.Event 或 dict（需含 type/payload/occurred_at 之一）。
    payload 约定：dataset（数据集名）、evidence_count（新证据条数）。
    """
    if task.status != _WAITING:
        return False
    conds = task.wake_conditions
    payload = getattr(event, "payload", None) or (event.get("payload") if isinstance(event, dict) else {}) or {}
    etype = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else "")
    occurred = getattr(event, "occurred_at", None) or (event.get("occurred_at") if isinstance(event, dict) else None)

    if "on_dataset" in conds:
        ds = payload.get("dataset") or payload.get("data_source")
        if ds and conds["on_dataset"] in str(ds):
            return True
    if "evidence_count_gte" in conds:
        n = payload.get("evidence_count")
        if n is None:
            n = payload.get("rows")
        try:
            # n 为 None（事件与证据计数无关）时按 0 处理，不唤醒也不算错
            if int(n if n is not None else 0) >= int(conds["evidence_count_gte"]):
                return True
        except (TypeError, ValueError):
            # 无法解析不等于"条件不满足"：交由 condition_parse_error 显式化（REQ-G-005）
            return False
    if "after" in conds:
        ref = str(occurred or _now())[:10]
        if ref >= str(conds["after"])[:10]:
            return True
    return False


def condition_parse_error(task: DeferredTask, event) -> str | None:
    """REQ-G-005：检测唤醒条件是否"存在但无法解析"（区别于正常的未满足）。

    仅当事件确实携带了 evidence_count/rows 字段、但其值无法转整数时，判定为
    条件解析错误（返回人读原因）；事件不带该字段时返回 None（与本条件无关）。
    """
    if task.status != _WAITING:
        return None
    conds = task.wake_conditions
    if "evidence_count_gte" not in conds:
        return None
    payload = getattr(event, "payload", None) \
        or (event.get("payload") if isinstance(event, dict) else {}) or {}
    has_field = ("evidence_count" in payload) or ("rows" in payload)
    if not has_field:
        return None
    n = payload.get("evidence_count")
    if n is None:
        n = payload.get("rows")
    try:
        int(n)
    except (TypeError, ValueError):
        return (f"任务 {task.task_id} 的 evidence_count_gte 条件无法解析："
                f"事件 evidence_count={n!r} 非整数（阈值 "
                f"{conds['evidence_count_gte']!r}）")
    return None


class DeferredBoard:
    """deferred_task 表的唯一读写入口。"""

    def __init__(self, conn, health=None):
        self._conn = conn
        conn.execute(_DDL)
        from core.run_health import get_health
        self.health = get_health(health)

    def _publish(self, etype: str, payload: dict, actor: str, where: str) -> None:
        """发事件；落盘失败不再静默吞（REQ-G-004），留痕但不阻断主流程。"""
        try:
            from core.event_bus import EventBus
            EventBus(self._conn).publish(etype, payload, actor=actor)
        except Exception as e:
            self.health.record(
                "event_publish_failed", "warning",
                source=f"deferred:{where}",
                reason=f"事件 {etype} 发布失败：{str(e)[:120]}",
                event_type=etype)

    # ---- 登记 ----
    def defer(self, *, candidate_id: str, wake_conditions: dict,
              entity_type: str = "", canonical: str = "",
              operator: str = "system", scheduled_at: str | None = None) -> DeferredTask:
        validate_wake_conditions(wake_conditions)
        task = DeferredTask(
            task_id=f"def_{uuid.uuid4().hex[:12]}",
            candidate_id=candidate_id, entity_type=entity_type,
            canonical=canonical, wake_conditions=wake_conditions,
            scheduled_at=scheduled_at or _now(), created_by=operator)
        self._conn.execute(
            "INSERT INTO deferred_task (task_id, candidate_id, entity_type, canonical, "
            "wake_conditions_json, scheduled_at, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [task.task_id, task.candidate_id, task.entity_type, task.canonical,
             json.dumps(task.wake_conditions, ensure_ascii=False),
             task.scheduled_at, _WAITING, operator, _now()])
        self._publish(
            "review.deferred",
            {"candidate_id": candidate_id, "wake_conditions": wake_conditions},
            actor=operator, where="defer")
        return task

    # ---- 查询 ----
    def list_all(self, status: str | None = None) -> list[DeferredTask]:
        sql = ("SELECT task_id, candidate_id, entity_type, canonical, "
               "wake_conditions_json, scheduled_at, status, created_by, "
               "created_at, woken_at, wake_reason, decided_at FROM deferred_task")
        if status:
            rows = self._conn.execute(sql + " WHERE status=?", [status]).fetchall()
        else:
            rows = self._conn.execute(sql).fetchall()
        cols = ["task_id", "candidate_id", "entity_type", "canonical",
                "wake_conditions_json", "scheduled_at", "status", "created_by",
                "created_at", "woken_at", "wake_reason", "decided_at"]
        return [DeferredTask.from_row(dict(zip(cols, r))) for r in rows]

    # ---- 唤醒 ----
    def wake(self, task: DeferredTask, reason: str) -> DeferredTask | None:
        """置 woken（幂等：已唤醒返回 None，AC4）。"""
        if task.status != _WAITING:
            return None
        self._conn.execute(
            "UPDATE deferred_task SET status=?, woken_at=?, wake_reason=? WHERE task_id=?",
            [_WOKEN, _now(), reason, task.task_id])
        self._publish(
            "review.decided",
            {"candidate_id": task.candidate_id, "decision": "deferred_woken",
             "wake_reason": reason, "rebuild_triggered": False,
             "needs_review": True},
            actor="deferred", where="wake")
        return DeferredTask(task_id=task.task_id, candidate_id=task.candidate_id,
                            entity_type=task.entity_type, canonical=task.canonical,
                            wake_conditions=task.wake_conditions,
                            scheduled_at=task.scheduled_at, status=_WOKEN,
                            created_by=task.created_by, woken_at=_now(),
                            wake_reason=reason)

    def mark_condition_error(self, task: DeferredTask, reason: str) -> DeferredTask:
        """REQ-G-005：唤醒条件无法解析 → condition_error（区别于沉睡），critical 留痕。"""
        self._conn.execute(
            "UPDATE deferred_task SET status=?, wake_reason=? WHERE task_id=?",
            [_CONDITION_ERROR, reason[:400], task.task_id])
        self.health.record(
            "wake_condition_unparseable", "critical",
            source=f"deferred:{task.task_id}", reason=reason,
            candidate_id=task.candidate_id)
        return DeferredTask(task_id=task.task_id, candidate_id=task.candidate_id,
                            entity_type=task.entity_type, canonical=task.canonical,
                            wake_conditions=task.wake_conditions,
                            scheduled_at=task.scheduled_at, status=_CONDITION_ERROR,
                            created_by=task.created_by, wake_reason=reason)

    def on_event(self, event) -> list[DeferredTask]:
        """事件驱动唤醒：on_dataset / evidence_count_gte / after（按事件时间）。

        REQ-G-005：条件存在但无法解析（evidence_count 非数值）时，任务转
        condition_error 而非静默留在 waiting（永久沉睡）。
        """
        etype = getattr(event, "type", None)
        if etype is None and isinstance(event, dict):
            etype = event.get("type", "?")
        woken = []
        for t in self.list_all(_WAITING):
            err = condition_parse_error(t, event)
            if err:
                self.mark_condition_error(t, err)
                continue
            if match_wake(t, event):
                w = self.wake(t, reason=f"event:{etype or '?'}")
                if w:
                    woken.append(w)
        return woken

    def scan_due(self, *, now: datetime | None = None) -> list[DeferredTask]:
        """时间扫描：TTL（after）到期任务唤醒（AC3）。"""
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        woken = []
        for t in self.list_all(_WAITING):
            after = t.wake_conditions.get("after")
            if after and today >= str(after)[:10]:
                w = self.wake(t, reason=f"ttl:{after}")
                if w:
                    woken.append(w)
        return woken

    def woken_candidate_ids(self) -> list[str]:
        """已唤醒待重入 review 的候选 id。"""
        return [t.candidate_id for t in self.list_all(_WOKEN)]

    def reenter_review_queue(self, queue) -> int:
        """把已唤醒候选在 ReviewQueue 中重置为待确认（重入二次 review）。

        queue 鸭子类型：需有 get(candidate_id)；候选不在队列（已重建）则跳过。
        返回重置条数。
        """
        from core.review import Decision
        n = 0
        for cid in self.woken_candidate_ids():
            try:
                d = queue.get(cid)
            except KeyError:
                continue
            d.status = Decision.PENDING
            d.timestamp = _now()
            n += 1
        return n

    # ---- 回捞统计 ----
    def mark_decided(self, candidate_id: str) -> None:
        """候选唤醒后已被正兵二次决策（accept/reject），记 decided_at。"""
        self._conn.execute(
            "UPDATE deferred_task SET decided_at=? WHERE candidate_id=? AND status=?",
            [_now(), candidate_id, _WOKEN])

    def recall_stats(self) -> dict:
        """回捞率（AC5）：decided / total deferred；woken 为已唤醒待决策。"""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM deferred_task GROUP BY status").fetchall()
        counts = dict(rows)
        total = sum(counts.values())
        woken = counts.get(_WOKEN, 0)
        decided = self._conn.execute(
            "SELECT COUNT(*) FROM deferred_task WHERE decided_at IS NOT NULL").fetchone()[0]
        return {"total_deferred": total, "waiting": counts.get(_WAITING, 0),
                "woken": woken, "decided_after_wake": decided,
                "condition_error": counts.get(_CONDITION_ERROR, 0),
                "recall_rate": round(decided / total, 4) if total else 0.0}
