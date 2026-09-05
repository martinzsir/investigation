"""
core/parameters.py
REQ-032 参数治理：阈值/参数集版本化、提案审批、影子比对、回滚、provenance。

两表（不带 obj_/lnk_ 前缀，编译器不 DROP；CREATE IF NOT EXISTS 幂等）：
  parameter_set(set_id, scope=rule_id, version_seq, values, provenance,
                status draft/shadow/production/retired, approved_by, valid_from)
  parameter_proposal(proposal_id, set_id, evidence, risk, rollback_version,
                     status pending/approved/rejected, decided_by)

纪律：
  - AC1 变更恒新版本（version_seq 递增，永不覆盖旧值）；
  - AC2 未审批提案绝不生效：effective_values 只读 production 行；
  - AC3 shadow_compare：两组 values 各跑一次白名单只读 Function，比对 finding 差异；
  - AC4 rollback：当前 production → retired，目标版本 → production；
  - AC5 provenance（metrics_run_id/sample_size/basis）随版本落盘；
  - AC6 样本量 < MIN_SAMPLE_SIZE(20) 的提案拒绝进入审批。
本模块不改检测器接线（run_rules 仍读 rules.json）；参数上线由人工审批后另行接线。
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid

MIN_SAMPLE_SIZE = 20
SET_STATUSES = ("draft", "shadow", "production", "retired")
PROPOSAL_STATUSES = ("pending", "approved", "rejected")

_DDL_SET = """
CREATE TABLE IF NOT EXISTS parameter_set (
    set_id        VARCHAR PRIMARY KEY,
    scope         VARCHAR NOT NULL,
    version_seq   BIGINT  NOT NULL,
    case_id       VARCHAR NOT NULL DEFAULT 'default',
    values_json   VARCHAR NOT NULL,
    provenance    VARCHAR,
    status        VARCHAR NOT NULL DEFAULT 'draft',
    created_by    VARCHAR NOT NULL,
    created_at    TIMESTAMP NOT NULL,
    approved_by   VARCHAR,
    valid_from    TIMESTAMP
)
"""
_DDL_PROP = """
CREATE TABLE IF NOT EXISTS parameter_proposal (
    proposal_id       VARCHAR PRIMARY KEY,
    set_id            VARCHAR NOT NULL,
    scope             VARCHAR NOT NULL,
    evidence          VARCHAR,
    risk              VARCHAR,
    rollback_version  BIGINT,
    sample_size       BIGINT,
    status            VARCHAR NOT NULL DEFAULT 'pending',
    created_by        VARCHAR NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    decided_by        VARCHAR,
    decided_at        TIMESTAMP,
    decision_reason   VARCHAR,
    audit_event_id    VARCHAR
)
"""


class ParameterGovernanceError(ValueError):
    """参数治理校验/状态机错误。"""


def ensure_tables(conn) -> None:
    conn.execute(_DDL_SET)
    conn.execute(_DDL_PROP)


# ----------------------------------------------------------------------
# 参数集版本
# ----------------------------------------------------------------------
def draft_set(conn, *, scope: str, values: dict, provenance: dict | None = None,
              operator: str, case_id: str = "default") -> dict:
    """起草新参数版本：同一 scope 恒新 version_seq（AC1 永不覆盖）。"""
    ensure_tables(conn)
    if not isinstance(values, dict) or not values:
        raise ParameterGovernanceError("values 必须是非空调用参数对象")
    row = conn.execute(
        "SELECT COALESCE(MAX(version_seq), 0) FROM parameter_set WHERE scope = ?",
        [scope]).fetchone()
    seq = int(row[0]) + 1
    set_id = f"ps-{scope}-v{seq}"
    conn.execute(
        """INSERT INTO parameter_set
           (set_id, scope, version_seq, case_id, values_json, provenance,
            status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
        [set_id, scope, seq, case_id,
         json.dumps(values, ensure_ascii=False, default=str),
         json.dumps(provenance or {}, ensure_ascii=False, default=str),
         operator, _dt.datetime.now().isoformat(timespec="seconds")])
    return {"set_id": set_id, "scope": scope, "version_seq": seq,
            "status": "draft", "values": dict(values)}


def get_set(conn, set_id: str) -> dict | None:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT set_id, scope, version_seq, case_id, values_json, provenance, "
        "status, created_by, created_at, approved_by, valid_from "
        "FROM parameter_set WHERE set_id = ?", [set_id]).fetchone()
    if not row:
        return None
    d = dict(zip(["set_id", "scope", "version_seq", "case_id", "values_json",
                  "provenance", "status", "created_by", "created_at",
                  "approved_by", "valid_from"], row))
    d["values"] = json.loads(d.pop("values_json"))
    d["provenance"] = json.loads(d["provenance"]) if d["provenance"] else {}
    return d


def list_sets(conn, scope: str | None = None) -> list[dict]:
    ensure_tables(conn)
    sql = ("SELECT set_id, scope, version_seq, case_id, values_json, provenance, "
           "status, created_by, created_at, approved_by, valid_from FROM parameter_set")
    args = []
    if scope:
        sql += " WHERE scope = ?"; args.append(scope)
    sql += " ORDER BY scope, version_seq"
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(zip(["set_id", "scope", "version_seq", "case_id", "values_json",
                      "provenance", "status", "created_by", "created_at",
                      "approved_by", "valid_from"], r))
        d["values"] = json.loads(d.pop("values_json"))
        d["provenance"] = json.loads(d["provenance"]) if d["provenance"] else {}
        out.append(d)
    return out


def effective_values(conn, scope: str) -> dict:
    """AC2：只读当前 production 版本 values；无 production → {}（调用方回落规则默认值）。"""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT values_json FROM parameter_set "
        "WHERE scope = ? AND status = 'production' "
        "ORDER BY valid_from DESC, version_seq DESC LIMIT 1", [scope]).fetchone()
    return json.loads(row[0]) if row else {}


def effective_set_id(conn, scope: str) -> str | None:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT set_id FROM parameter_set "
        "WHERE scope = ? AND status = 'production' "
        "ORDER BY valid_from DESC, version_seq DESC LIMIT 1", [scope]).fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------------
# 参数提案
# ----------------------------------------------------------------------
def propose(conn, *, set_id: str, evidence: dict, risk: str,
            operator: str, sample_size: int,
            rollback_version: int | None = None) -> str:
    """创建参数变更提案（pending）。AC6：sample_size < 20 或缺失 → 拒入审批。"""
    ensure_tables(conn)
    s = get_set(conn, set_id)
    if s is None:
        raise ParameterGovernanceError(f"参数集不存在：{set_id}")
    if s["status"] not in ("draft", "shadow"):
        raise ParameterGovernanceError(
            f"参数集 {set_id} 状态 {s['status']}：只有 draft/shadow 可提案")
    if not isinstance(sample_size, int) or isinstance(sample_size, bool):
        raise ParameterGovernanceError(
            "propose 必须提供整数 sample_size（样本量证据缺失，fail-closed）")
    if int(sample_size) < MIN_SAMPLE_SIZE:
        raise ParameterGovernanceError(
            f"样本量 {sample_size} < {MIN_SAMPLE_SIZE}：证据不足，拒绝进入审批"
            f"（与阈值策略 min_samples 对齐，REQ-032 AC6）")
    if rollback_version is None:
        row = conn.execute(
            "SELECT version_seq FROM parameter_set "
            "WHERE scope = ? AND status = 'production' "
            "ORDER BY version_seq DESC LIMIT 1", [s["scope"]]).fetchone()
        rollback_version = int(row[0]) if row else None
    pid = "pp-" + uuid.uuid4().hex[:12]
    conn.execute(
        """INSERT INTO parameter_proposal
           (proposal_id, set_id, scope, evidence, risk, rollback_version,
            sample_size, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        [pid, set_id, s["scope"],
         json.dumps(evidence or {}, ensure_ascii=False, default=str),
         risk, rollback_version,
         int(sample_size) if sample_size is not None else None,
         operator, _dt.datetime.now().isoformat(timespec="seconds")])
    return pid


def _audit(conn, operator, before, after, source_ids, ontology_version="default"):
    from core.audit import AuditChain
    chain = AuditChain(conn)
    return chain.append(
        operator=operator, before=before, after=after,
        source_row_ids=source_ids,
        ontology_version=chain.current_ontology_version())


def approve(conn, proposal_id: str, operator: str,
            mode: str = "shadow", reason: str = "") -> dict:
    """审批提案：mode=shadow → 参数集进 shadow（不生效）；mode=production → 上线。

    上线时同 scope 旧 production 自动 retired；提案与参数集落审计。
    """
    ensure_tables(conn)
    if mode not in ("shadow", "production"):
        raise ParameterGovernanceError("mode 仅允许 shadow/production")
    prop = conn.execute(
        "SELECT proposal_id, set_id, scope, status, rollback_version "
        "FROM parameter_proposal WHERE proposal_id = ?", [proposal_id]).fetchone()
    if not prop:
        raise ParameterGovernanceError(f"参数提案不存在：{proposal_id}")
    pid, set_id, scope, pstatus, rollback_ver = prop
    if pstatus != "pending":
        raise ParameterGovernanceError(
            f"提案 {pid} 状态 {pstatus}：pending 提案才可审批（状态机单向）")
    s = get_set(conn, set_id)
    if s is None:
        raise ParameterGovernanceError(f"参数集不存在：{set_id}")

    new_status = "production" if mode == "production" else "shadow"
    now = _dt.datetime.now().isoformat(timespec="seconds")
    retired = []
    if mode == "production":
        old = conn.execute(
            "SELECT set_id FROM parameter_set "
            "WHERE scope = ? AND status = 'production'", [scope]).fetchall()
        for (old_id,) in old:
            conn.execute(
                "UPDATE parameter_set SET status = 'retired' WHERE set_id = ?",
                [old_id])
            retired.append(old_id)
    conn.execute(
        "UPDATE parameter_set SET status = ?, approved_by = ?, valid_from = ? "
        "WHERE set_id = ?", [new_status, operator,
                             now if mode == "production" else None, set_id])
    event_id = _audit(
        conn, operator,
        before={"set_id": set_id, "status": s["status"], "proposal": "pending"},
        after={"set_id": set_id, "status": new_status, "mode": mode,
               "retired": retired, "reason": reason,
               "rollback_version": rollback_ver},
        source_ids=[pid, set_id] + retired)
    conn.execute(
        """UPDATE parameter_proposal
           SET status = 'approved', decided_by = ?, decided_at = ?,
               decision_reason = ?, audit_event_id = ?
           WHERE proposal_id = ?""",
        [operator, now, reason, event_id, pid])
    return {"proposal_id": pid, "set_id": set_id, "status": new_status,
            "retired": retired, "audit_event_id": event_id}


def reject(conn, proposal_id: str, operator: str, reason: str = "") -> None:
    ensure_tables(conn)
    prop = conn.execute(
        "SELECT status FROM parameter_proposal WHERE proposal_id = ?",
        [proposal_id]).fetchone()
    if not prop:
        raise ParameterGovernanceError(f"参数提案不存在：{proposal_id}")
    if prop[0] != "pending":
        raise ParameterGovernanceError(f"提案 {proposal_id} 状态 {prop[0]}，不可重复审批")
    now = _dt.datetime.now().isoformat(timespec="seconds")
    event_id = _audit(conn, operator,
                      before={"proposal": proposal_id, "status": "pending"},
                      after={"proposal": proposal_id, "status": "rejected",
                             "reason": reason},
                      source_ids=[proposal_id])
    conn.execute(
        """UPDATE parameter_proposal
           SET status = 'rejected', decided_by = ?, decided_at = ?,
               decision_reason = ?, audit_event_id = ?
           WHERE proposal_id = ?""",
        [operator, now, reason, event_id, proposal_id])


def rollback(conn, *, scope: str, rollback_version: int,
             operator: str, reason: str = "") -> dict:
    """AC4：当前 production → retired，目标 version → production。"""
    ensure_tables(conn)
    target = conn.execute(
        "SELECT set_id, version_seq, status FROM parameter_set "
        "WHERE scope = ? AND version_seq = ?", [scope, rollback_version]).fetchone()
    if not target:
        raise ParameterGovernanceError(
            f"回滚目标不存在：scope={scope} version={rollback_version}")
    target_id, target_seq, target_status = target
    if target_status == "retired":
        # retired 可被回滚重新激活（版本不删除原则）
        pass
    current = conn.execute(
        "SELECT set_id FROM parameter_set "
        "WHERE scope = ? AND status = 'production'", [scope]).fetchall()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    retired = []
    for (cur_id,) in current:
        if cur_id == target_id:
            continue
        conn.execute("UPDATE parameter_set SET status = 'retired' WHERE set_id = ?",
                     [cur_id])
        retired.append(cur_id)
    conn.execute(
        "UPDATE parameter_set SET status = 'production', approved_by = ?, "
        "valid_from = ? WHERE set_id = ?", [operator, now, target_id])
    event_id = _audit(
        conn, operator,
        before={"scope": scope, "production": [r[0] for r in current]},
        after={"scope": scope, "production": [target_id], "retired": retired,
               "rollback_to_version": target_seq, "reason": reason},
        source_ids=[target_id] + retired)
    return {"scope": scope, "production_set": target_id,
            "version_seq": target_seq, "retired": retired,
            "audit_event_id": event_id}


# ----------------------------------------------------------------------
# 影子比对（AC3）：两组 values 各跑一次白名单只读 Function
# ----------------------------------------------------------------------
def _row_sig(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def shadow_compare(conn, old_set_id: str, new_set_id: str, store,
                   rule_id: str, pack: str = "default") -> dict:
    """用两组参数值各执行一次规则挂钩的只读 Function，比对结果行差异。

    返回 {rule_id, function, old_count, new_count, added[], removed[], stable}。
    全程只读（FunctionExecutor 白名单函数），不写任何业务表。
    """
    from core.functions import FunctionExecutor
    from core.ontology_loader import load_pack
    spec = load_pack(pack)
    if rule_id not in spec.rules:
        raise ParameterGovernanceError(f"rule_id {rule_id!r} 不在规则手册")
    rule = spec.rules[rule_id]
    old_set, new_set = get_set(conn, old_set_id), get_set(conn, new_set_id)
    if old_set is None or new_set is None:
        raise ParameterGovernanceError("shadow_compare：参数集不存在")
    base = dict(rule.params or {})
    fx = FunctionExecutor(store, pack)

    def _run(values: dict):
        merged = dict(base)
        merged.update(values or {})
        out = fx.invoke(rule.function, merged)
        rows = out.get("rows") or []
        return rows

    old_rows = _run(old_set["values"])
    new_rows = _run(new_set["values"])
    old_sigs = {_row_sig(r) for r in old_rows}
    new_sigs = {_row_sig(r) for r in new_rows}
    added = [r for r in new_rows if _row_sig(r) in (new_sigs - old_sigs)]
    removed = [r for r in old_rows if _row_sig(r) in (old_sigs - new_sigs)]
    return {
        "rule_id": rule_id,
        "function": rule.function,
        "old_set": old_set_id,
        "new_set": new_set_id,
        "old_count": len(old_rows),
        "new_count": len(new_rows),
        "added": added,
        "removed": removed,
        "stable": not added and not removed,
    }
