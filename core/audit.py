"""
core/audit.py
事件溯源审计 + SHA-256 哈希链（REQ-007）。

每条审计事件含 prev_hash + signature，形成不可篡改链。
篡改/删除/重放任一条均可被 chain_verify() 检测。

AuditChain 替换 LineageClue.audit_log 的简单 append，
set_status/set_filed 改为调 AuditChain.append（同时保留内存 audit_log 向后兼容）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


_GENESIS_HASH = "0" * 64

# REQ-G-025：线索处置事件识别——after 快照含线索状态机五态之一的 status，
# 且不含提案/参数集/导出等其他审计事件的特征键（这些事件也可能带 status 键）。
_DISPOSAL_STATUS = ("待查", "查证中", "已排除", "已固证", "已立案")
_DISPOSAL_EXCLUDE_KEYS = ("proposal_id", "proposal", "set_id", "action")


def _is_disposal_event(after: Any) -> bool:
    """after_state 是否为线索处置状态变更事件（verify/exclude/confirm/file）。"""
    if not isinstance(after, dict):
        return False
    if after.get("status") not in _DISPOSAL_STATUS:
        return False
    return not any(k in after for k in _DISPOSAL_EXCLUDE_KEYS)


@dataclass(frozen=True)
class AuditEvent:
    """不可变审计事件（哈希链节点）。"""
    event_id: str
    case_id: str
    ontology_version: str       # 来自 REQ-001 current_version
    rule_version: str | None    # rule_id + params_hash
    function_version: str | None
    params_hash: str | None
    source_row_ids: list[str]
    operator: str
    before: dict | None         # 状态变更前快照
    after: dict | None          # 状态变更后快照
    prev_hash: str              # 上一条 signature（链首为 "0"*64）
    signature: str              # sha256(本条所有字段 + prev_hash)
    occurred_at: str


_DDL = """
CREATE TABLE IF NOT EXISTS audit_chain (
    seq BIGINT NOT NULL,
    event_id VARCHAR PRIMARY KEY,
    case_id VARCHAR NOT NULL,
    ontology_version VARCHAR NOT NULL,
    rule_version VARCHAR,
    function_version VARCHAR,
    params_hash VARCHAR,
    source_row_ids VARCHAR,
    operator VARCHAR NOT NULL,
    before_state VARCHAR,
    after_state VARCHAR,
    prev_hash VARCHAR NOT NULL,
    signature VARCHAR NOT NULL,
    occurred_at VARCHAR NOT NULL
)
"""


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _compute_signature(event_id: str, case_id: str, ontology_version: str,
                       rule_version: str | None, function_version: str | None,
                       params_hash: str | None, source_row_ids: list[str],
                       operator: str, before: dict | None,
                       after: dict | None, prev_hash: str) -> str:
    """计算签名：所有字段 + prev_hash 的 sha256。"""
    payload = json.dumps({
        "event_id": event_id, "case_id": case_id,
        "ontology_version": ontology_version,
        "rule_version": rule_version, "function_version": function_version,
        "params_hash": params_hash,
        "source_row_ids": source_row_ids,
        "operator": operator,
        "before": before, "after": after,
        "prev_hash": prev_hash,
    }, ensure_ascii=False, sort_keys=True, default=str)
    return _sha256(payload)


class AuditChain:
    """持久化审计链（DuckDB 表 audit_chain）。"""

    def __init__(self, conn, case_id: str = "default", health=None):
        from core.run_health import get_health
        self._conn = conn
        self._case_id = case_id
        self.health = get_health(health)
        conn.execute(_DDL)
        self._seq = self._next_seq()

    def _next_seq(self) -> int:
        """获取下一个序列号（基于当前 max(seq)+1）。"""
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_chain").fetchone()
            return row[0]
        except Exception:
            return 1

    def current_ontology_version(self) -> str:
        """从 REQ-001 meta_ontology_state 取当前版本号。

        REQ-G-007：取不到版本锚点时回退 "unknown" 不崩，但落 version_anchor_missing
        诊断（warning），避免"审计链记录了 unknown 版本却无人知晓"。
        """
        try:
            from core.ontology_version import current_version
            ver = current_version(self._conn, "default")
            if ver and ver.ontology_version and ver.ontology_version != "unknown":
                return ver.ontology_version
        except Exception as e:
            self.health.record(
                "version_anchor_missing", "warning",
                source="audit:ontology_version",
                reason=f"审计链取 ontology 版本锚点失败：{str(e)[:80]}")
            return "unknown"
        self.health.record(
            "version_anchor_missing", "warning",
            source="audit:ontology_version",
            reason="审计链 ontology 版本锚点缺失（meta 无版本记录），写入 unknown")
        return "unknown"

    def append(self, *, operator: str, before: dict | None, after: dict | None,
               source_row_ids: list[str], ontology_version: str,
               rule_version: str | None = None,
               function_version: str | None = None,
               params_hash: str | None = None) -> str:
        """追加一条审计事件，返回 event_id。"""
        # 1. 取上一条 signature 作为 prev_hash
        row = self._conn.execute(
            "SELECT signature FROM audit_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row[0] if row else _GENESIS_HASH

        # 2. 生成 event_id
        event_id = uuid.uuid4().hex
        occurred_at = datetime.now().isoformat(timespec="seconds")

        # REQ-G-007：版本锚点为 unknown 时在 after_state 内打 anchor_status=missing
        # （不改表结构；签名基于标注后的 after 计算，保证链一致性）。
        if (not ontology_version or ontology_version == "unknown") and isinstance(after, dict):
            after = {**after, "anchor_status": "missing"}

        # 3. 计算签名
        signature = _compute_signature(
            event_id, self._case_id, ontology_version,
            rule_version, function_version, params_hash,
            source_row_ids, operator, before, after, prev_hash)

        # 4. 落盘（event_id 唯一，重复 INSERT 被忽略）
        seq = self._seq
        self._seq += 1
        self._conn.execute(
            """INSERT OR IGNORE INTO audit_chain
               (seq, event_id, case_id, ontology_version, rule_version, function_version,
                params_hash, source_row_ids, operator, before_state, after_state,
                prev_hash, signature, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [seq, event_id, self._case_id, ontology_version,
             rule_version, function_version, params_hash,
             json.dumps(source_row_ids, ensure_ascii=False),
             operator,
             json.dumps(before, ensure_ascii=False, default=str) if before else None,
             json.dumps(after, ensure_ascii=False, default=str) if after else None,
             prev_hash, signature, occurred_at])
        return event_id

    def chain_verify(self) -> bool:
        """校验整条链：每条 signature 重算一致 + prev_hash 衔接。"""
        rows = self._conn.execute(
            """SELECT event_id, case_id, ontology_version, rule_version,
                      function_version, params_hash, source_row_ids, operator,
                      before_state, after_state, prev_hash, signature
               FROM audit_chain ORDER BY seq"""
        ).fetchall()
        prev = _GENESIS_HASH
        for r in rows:
            (event_id, case_id, ontology_version, rule_version,
             function_version, params_hash, source_row_ids_json,
             operator, before_json, after_json,
             prev_hash, signature) = r
            # prev_hash 衔接检查
            if prev_hash != prev:
                return False
            # signature 重算
            try:
                source_row_ids = json.loads(source_row_ids_json) if source_row_ids_json else []
            except json.JSONDecodeError:
                source_row_ids = []
            before = json.loads(before_json) if before_json else None
            after = json.loads(after_json) if after_json else None
            recomputed = _compute_signature(
                event_id, case_id, ontology_version,
                rule_version, function_version, params_hash,
                source_row_ids, operator, before, after, prev_hash)
            if recomputed != signature:
                return False
            prev = signature
        return True

    def chain_integrity(self) -> dict:
        """REQ-G-018：审计链完备性明细（chain_verify 只回 bool，此处给可核对清单）。

        返回 {chain_ok, expected_count, actual_count, broken_links, missing_fields,
              disposal_events, disposal_activity}：
          - broken_links：prev_hash 断链 或 signature 重算不一致的 event_id（疑似篡改/丢链）；
          - missing_fields：operator / after_state 为空、或 ontology_version=unknown/n-a
            （REQ-G-007：签名通过 ≠ 锚定完好）；
          - expected/actual：按 max(seq) 与实际行数对出缺号；
          - disposal_events：链上线索处置事件数（after 含线索状态机五态 status）；
          - disposal_activity：库内"处置动作确有发生"的持久痕迹
            （persisted_non_pending=clue_disposal_status 非待查行；
             actions_applied=action_request 已本地提交请求）。
        REQ-G-025：有处置痕迹但链上零处置事件（接线缺失类静默失败）→ chain_ok=False
        并落 warning；链全空且确无处置（纯查询运行）→ 不报，保持 chain_ok=True。
        结论落 run_diagnostic（断链 critical，字段缺失/脱链 warning），并计入健康度。
        """
        rows = self._conn.execute(
            """SELECT seq, event_id, case_id, ontology_version, rule_version,
                      function_version, params_hash, source_row_ids, operator,
                      before_state, after_state, prev_hash, signature
               FROM audit_chain ORDER BY seq"""
        ).fetchall()
        prev = _GENESIS_HASH
        broken: list[str] = []
        missing: list[dict] = []
        disposal_events = 0
        _ANCHOR_BAD = ("", "unknown", "n/a", None)
        for r in rows:
            (seq, event_id, _case_id, ontology_version, rule_version,
             function_version, params_hash, source_row_ids_json,
             operator, before_json, after_json, prev_hash, signature) = r
            if prev_hash != prev:
                if event_id not in broken:
                    broken.append(event_id)
            try:
                source_row_ids = json.loads(source_row_ids_json) if source_row_ids_json else []
            except json.JSONDecodeError:
                source_row_ids = []
            before = json.loads(before_json) if before_json else None
            after = json.loads(after_json) if after_json else None
            if _is_disposal_event(after):
                disposal_events += 1
            recomputed = _compute_signature(
                event_id, _case_id, ontology_version,
                rule_version, function_version, params_hash,
                source_row_ids, operator, before, after, prev_hash)
            if recomputed != signature and event_id not in broken:
                broken.append(event_id)
            miss = []
            if not operator:
                miss.append("operator")
            if after_json is None or str(after_json).strip() in ("", "null"):
                miss.append("after_state")
            if (ontology_version or "").strip().lower() in _ANCHOR_BAD:
                miss.append("ontology_version")
            if miss:
                missing.append({"event_id": event_id, "fields": miss})
            prev = signature
        actual = len(rows)
        expected = max([int(r[0]) for r in rows], default=0)
        # REQ-G-025：处置动作有痕迹、链上零事件 = 接线缺失（空链不再判完整）
        activity = self._disposal_activity()
        activity_total = activity["persisted_non_pending"] + activity["actions_applied"]
        unwired = activity_total > 0 and disposal_events == 0
        chain_ok = ((not broken) and (not missing) and (expected == actual)
                    and (not unwired))
        if broken:
            self.health.record(
                "audit_integrity_gap", "critical",
                source="audit:chain",
                reason=f"审计链断链/签名不一致 {len(broken)} 条",
                broken_links=broken, expected_count=expected, actual_count=actual)
        elif unwired:
            self.health.record(
                "audit_integrity_gap", "warning",
                source="audit:chain",
                reason=f"处置动作未落审计链（疑似接线缺失）：持久化处置 "
                       f"{activity['persisted_non_pending']} 处、两阶段已提交动作 "
                       f"{activity['actions_applied']} 起，链上处置事件 0 条",
                disposal_activity=activity, disposal_events=0,
                expected_count=expected, actual_count=actual)
        elif missing or expected != actual:
            self.health.record(
                "audit_integrity_gap", "warning",
                source="audit:chain",
                reason=f"审计链完备性缺口：字段缺失 {len(missing)} 条"
                       f"{'，链序号缺号' if expected != actual else ''}",
                missing_fields=missing, expected_count=expected, actual_count=actual)
        return {"chain_ok": chain_ok, "expected_count": expected,
                "actual_count": actual, "broken_links": broken,
                "missing_fields": missing,
                "disposal_events": disposal_events,
                "disposal_activity": activity}

    def _disposal_activity(self) -> dict:
        """REQ-G-025：核对"处置动作确有发生"的持久痕迹（两独立证据源）。

        - persisted_non_pending：clue_disposal_status 中非「待查」行数
          （DisposalBoard.persist 落盘的处置结果快照，即时执行路径）；
        - actions_applied：action_request 中已过本地提交（_apply 已执行）的请求数
          （两阶段路径：dispatching/pending_receipt/confirmed/dispatch_failed）。
        表不存在或查询失败一律按 0 处理——纯查询运行合法，不制造噪声。
        """
        persisted = applied = 0
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM clue_disposal_status WHERE status <> '待查'"
            ).fetchone()
            persisted = int(row[0]) if row else 0
        except Exception:
            pass
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM action_request WHERE status IN "
                "('dispatching', 'pending_receipt', 'confirmed', 'dispatch_failed')"
            ).fetchone()
            applied = int(row[0]) if row else 0
        except Exception:
            pass
        return {"persisted_non_pending": persisted, "actions_applied": applied}

    def root_hash(self) -> str:
        """末条 signature 作为根哈希（同序列稳定复现）。"""
        row = self._conn.execute(
            "SELECT signature FROM audit_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else _GENESIS_HASH

    def count(self) -> int:
        """链中事件数。"""
        row = self._conn.execute("SELECT COUNT(*) FROM audit_chain").fetchone()
        return row[0] if row else 0
