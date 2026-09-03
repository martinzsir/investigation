"""
core/case_library.py
REQ-031 案例库：已核验线索沉淀为可检索案例片段（case_fragment）。

四质量门（settle_fragment，任一不过即拒，错误一次性收集）：
  1. 终态门：线索处置状态必须 ∈ {已固证→verified, 已排除→excluded}
     （clue_disposal_status 表；待查/查证中一律拒绝，未核验不得入库）；
  2. 脱敏门：pattern/evidence 经 PII 正则复扫零命中，且案件知识包
     case_knowledge.json 的 subject_aliases 真实姓名不得出现（必须用 当事人#token）；
  3. 适用条件门：pattern 非空、含 rule_id、达最小长度（可复用的适用条件描述）；
  4. legal_basis 非空；rule_version / ontology_version 齐全（AC5 溯源）。
检索 search(rule_id, keyword, outcome)；每次沉淀落 AuditChain。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid

from core.llm.redact import scan_pii

TERMINAL_MAP = {"已固证": "verified", "已排除": "excluded"}
OUTCOMES = ("verified", "excluded")

_DDL = """
CREATE TABLE IF NOT EXISTS case_fragment (
    fragment_id       VARCHAR PRIMARY KEY,
    rule_id           VARCHAR NOT NULL,
    rule_version      VARCHAR NOT NULL,
    case_id           VARCHAR NOT NULL,
    ontology_version  VARCHAR NOT NULL,
    pattern           VARCHAR NOT NULL,
    evidence          VARCHAR,
    outcome           VARCHAR NOT NULL,
    confidence        DOUBLE,
    legal_basis       VARCHAR NOT NULL,
    redaction_hash    VARCHAR NOT NULL,
    clue_id           VARCHAR NOT NULL,
    created_by        VARCHAR NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    audit_event_id    VARCHAR
)
"""


class CaseLibraryError(ValueError):
    """案例沉淀未过质量门。"""


def ensure_case_fragment(conn) -> None:
    conn.execute(_DDL)


def _real_names(pack: str) -> set[str]:
    """案件知识包中的真实姓名集合（subject_aliases 键 + 别名值）。"""
    from core.functions import load_case_knowledge
    kn = load_case_knowledge(pack)
    names: set[str] = set()
    for k, aliases in (kn.get("subject_aliases") or {}).items():
        names.add(k)
        names.update(aliases or [])
    names.discard("")
    return names


def _clue_status(conn, clue_id: str) -> str | None:
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'clue_disposal_status'").fetchone()[0]
    if not exists:
        return None
    row = conn.execute(
        "SELECT status FROM clue_disposal_status WHERE clue_id = ?",
        [clue_id]).fetchone()
    return row[0] if row else None


def settle_fragment(conn, *, clue_id: str, rule_id: str, outcome: str,
                    legal_basis: str, operator: str, pattern: str,
                    evidence: dict | None = None, confidence: float | None = None,
                    case_id: str = "default", pack: str = "default") -> str:
    """四质量门通过后沉淀案例片段，返回 fragment_id；不过抛 CaseLibraryError。"""
    ensure_case_fragment(conn)
    errors: list[str] = []

    # ---- 门 1：终态门 ----
    status = _clue_status(conn, clue_id)
    if status not in TERMINAL_MAP:
        errors.append(
            f"[终态门] 线索 {clue_id} 当前状态 {status!r}：仅 已固证/已排除 "
            f"可沉淀案例（待查/查证中未核验不得入库）")
    elif outcome not in OUTCOMES:
        errors.append(f"[终态门] outcome 仅允许 {OUTCOMES}，收到 {outcome!r}")
    elif TERMINAL_MAP[status] != outcome:
        errors.append(
            f"[终态门] 线索状态 {status!r} 对应 outcome={TERMINAL_MAP[status]}，"
            f"与申报 {outcome!r} 不一致")

    # ---- 门 3：适用条件门（先于脱敏，保证 pattern 是可检查文本）----
    if not isinstance(pattern, str) or not pattern.strip():
        errors.append("[适用条件门] pattern 必须是非空文本（适用条件描述）")
    else:
        if len(pattern.strip()) < 15:
            errors.append("[适用条件门] pattern 过短（<15 字）：须描述适用条件而非结论")
        if rule_id not in pattern:
            errors.append(f"[适用条件门] pattern 必须包含 rule_id {rule_id!r}（适用条件挂规则）")

    # ---- 门 4：legal_basis ----
    if not isinstance(legal_basis, str) or not legal_basis.strip():
        errors.append("[legal_basis] 法律依据非空（案例沉淀必须可援引）")

    # ---- 门 2：脱敏门 ----
    blob = json.dumps({"pattern": pattern, "evidence": evidence or {}},
                      ensure_ascii=False, default=str)
    pii = scan_pii(blob)
    if pii:
        errors.append(f"[脱敏门] pattern/evidence 检出 PII 形态 {pii}：禁止真名/证件号入库")
    leaked = sorted(n for n in _real_names(pack) if n and n in blob)
    if leaked:
        errors.append(f"[脱敏门] pattern/evidence 含案件真实姓名 {leaked}："
                      f"必须使用 当事人#token（tokenize）")
    if confidence is not None and not (
            isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0):
        errors.append(f"[字段] confidence 必须是 0~1 小数，收到 {confidence!r}")

    if errors:
        raise CaseLibraryError("案例沉淀被拒（{} 项）：\n  - {}".format(
            len(errors), "\n  - ".join(errors)))

    # ---- 版本溯源 ----
    from core.metrics import rule_version
    from core.ontology_loader import load_pack
    rules = load_pack(pack).rules
    if rule_id not in rules:
        raise CaseLibraryError(
            f"rule_id {rule_id!r} 不在规则手册 {sorted(rules)}")
    rv = rule_version(rules[rule_id])
    try:
        from core.ontology_version import current_version
        ov = current_version(conn, pack).ontology_version
    except Exception:
        ov = "unknown"

    fragment_id = "cf-" + uuid.uuid4().hex[:12]
    redaction_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO case_fragment
           (fragment_id, rule_id, rule_version, case_id, ontology_version,
            pattern, evidence, outcome, confidence, legal_basis, redaction_hash,
            clue_id, created_by, created_at, audit_event_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        [fragment_id, rule_id, rv, case_id, ov, pattern.strip(),
         json.dumps(evidence or {}, ensure_ascii=False, default=str),
         outcome, float(confidence) if confidence is not None else None,
         legal_basis.strip(), redaction_hash, clue_id, operator, now])

    # ---- 审计链 ----
    from core.audit import AuditChain
    chain = AuditChain(conn)
    event_id = chain.append(
        operator=operator,
        before=None,
        after={"action": "case_fragment_settled", "fragment_id": fragment_id,
               "rule_id": rule_id, "rule_version": rv, "outcome": outcome,
               "clue_id": clue_id},
        source_row_ids=[clue_id, fragment_id],
        ontology_version=ov,
        rule_version=rv)
    conn.execute("UPDATE case_fragment SET audit_event_id = ? WHERE fragment_id = ?",
                 [event_id, fragment_id])
    return fragment_id


def search(conn, *, rule_id: str | None = None, keyword: str | None = None,
           outcome: str | None = None, case_id: str = "default") -> list[dict]:
    """按 rule_id / outcome / pattern 关键词检索案例片段（keyword 走 LIKE）。"""
    ensure_case_fragment(conn)
    sql = ("SELECT fragment_id, rule_id, rule_version, case_id, ontology_version, "
           "pattern, evidence, outcome, confidence, legal_basis, redaction_hash, "
           "clue_id, created_by, created_at, audit_event_id "
           "FROM case_fragment WHERE case_id = ?")
    args: list = [case_id]
    if rule_id:
        sql += " AND rule_id = ?"; args.append(rule_id)
    if outcome:
        sql += " AND outcome = ?"; args.append(outcome)
    if keyword:
        sql += " AND pattern LIKE ?"; args.append(f"%{keyword}%")
    sql += " ORDER BY created_at DESC"
    cols = ["fragment_id", "rule_id", "rule_version", "case_id", "ontology_version",
            "pattern", "evidence", "outcome", "confidence", "legal_basis",
            "redaction_hash", "clue_id", "created_by", "created_at", "audit_event_id"]
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(zip(cols, r))
        d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else {}
        out.append(d)
    return out
