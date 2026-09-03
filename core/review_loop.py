"""
core/review_loop.py
REQ-016：review.decided → 增量重建 → 只重算受影响规则 → finding.changed。

闭环（accept）：
  1. entity_mapping 落表（受保护：编译器只重建 obj_*/lnk_*，归并结果不被清除）；
  2. plan_from_review 计算一跳影响范围（受影响对象/链接/规则）；
  3. materialize_changed 增量重物化（行级 diff，不重写无关对象）；
  4. 只重算 affected_rules，前后 findings 做集合 diff；
  5. 差异产生 finding.changed 事件，变化的 finding 标记重入二次 review。

红线：
  - reject 不删证据、不重建，只写 review.decided（feedback）事件（AC3）；
  - 一次 accept 只重算一次：review_applied 幂等表拦截重复调用/事件重放（AC2）；
  - 重算只覆盖 affected_rules，无关规则结果不变（AC4）。
"""
from __future__ import annotations

import json
import time

_MAPPING_DDL = """
CREATE TABLE IF NOT EXISTS entity_mapping (
    variant VARCHAR PRIMARY KEY,
    canonical VARCHAR NOT NULL,
    entity_type VARCHAR,
    decided_by VARCHAR,
    decided_at VARCHAR,
    decision_id VARCHAR,
    pack VARCHAR DEFAULT 'default'
)
"""

_APPLIED_DDL = """
CREATE TABLE IF NOT EXISTS review_applied (
    decision_id VARCHAR PRIMARY KEY,
    applied_at VARCHAR,
    result_json VARCHAR
)
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(conn) -> None:
    conn.execute(_MAPPING_DDL)
    conn.execute(_APPLIED_DDL)


# ----------------------------------------------------------------------
# 落表
# ----------------------------------------------------------------------
def record_accept(conn, decision, *, pack: str = "default") -> int:
    """accept → entity_mapping（variant → canonical，受保护表）。返回落表条数。"""
    ensure_tables(conn)
    n = 0
    for variant in decision.variants:
        conn.execute(
            "INSERT OR REPLACE INTO entity_mapping "
            "(variant, canonical, entity_type, decided_by, decided_at, decision_id, pack) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [variant, decision.canonical, decision.entity_type,
             decision.operator, _now(), decision.candidate_id, pack])
        n += 1
    return n


def record_feedback(conn, decision) -> None:
    """reject/defer：只写 review.decided 事件（feedback），不碰证据与语义层（AC3）。"""
    try:
        from core.event_bus import EventBus
        EventBus(conn).publish(
            "review.decided",
            {"candidate_id": decision.candidate_id,
             "entity_type": decision.entity_type,
             "decision": decision.status,
             "operator": decision.operator,
             "note": decision.note,
             "rebuild_triggered": False},
            actor=decision.operator or "review")
    except Exception:
        pass


# ----------------------------------------------------------------------
# findings diff
# ----------------------------------------------------------------------
def _finding_sig(f: dict) -> str:
    return json.dumps(f.get("source_rows", []), ensure_ascii=False,
                      sort_keys=True, default=str)


def _diff_findings(pre: list[dict], post: list[dict]) -> dict:
    """按规则分组做证据集合 diff。"""
    def index(fs):
        out: dict[str, set] = {}
        for f in fs:
            out.setdefault(f["rule_id"], set()).add(_finding_sig(f))
        return out
    pi, qi = index(pre), index(post)
    new, disappeared, changed_rules = {}, {}, []
    for rid in sorted(set(pi) | set(qi)):
        a, b = pi.get(rid, set()), qi.get(rid, set())
        if a == b:
            continue
        changed_rules.append(rid)
        if b - a:
            new[rid] = len(b - a)
        if a - b:
            disappeared[rid] = len(a - b)
    return {"changed_rules": changed_rules, "new": new, "disappeared": disappeared}


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def apply_accept(store, decision, *, pack: str = "default") -> dict:
    """处理一条 accept 决策：落映射 → 增量重建 → 重算 → finding.changed。

    幂等：同一 decision_id 重复应用返回首次结果，不重复重算（AC2）。
    """
    from core.rebuild_planner import plan_from_review
    from core.ontology import materialize_changed
    from core.rules import run_rules
    from core.event_bus import EventBus

    conn = store.conn
    ensure_tables(conn)

    prior = conn.execute(
        "SELECT result_json FROM review_applied WHERE decision_id=?",
        [decision.candidate_id]).fetchone()
    if prior:
        return json.loads(prior[0])

    bus = EventBus(conn)
    bus.publish("review.decided",
                {"candidate_id": decision.candidate_id,
                 "entity_type": decision.entity_type,
                 "decision": decision.status,
                 "operator": decision.operator,
                 "canonical": decision.canonical,
                 "variants": list(decision.variants),
                 "rebuild_triggered": True},
                actor=decision.operator or "review")

    # 1) 影响范围（重建前的图：种子覆盖合并双方）
    plan = plan_from_review(conn, decision, pack=pack)

    # 2) 重算前快照（只跑受影响规则）
    pre = run_rules(store, stage=None, pack=pack, rule_ids=plan.affected_rules) \
        if plan.affected_rules else []

    # 3) 落归并映射（受保护表）→ 增量重物化
    n_map = record_accept(conn, decision, pack=pack)
    if plan.mode != "skip":
        materialize_changed(conn, plan, pack=pack, bus=bus,
                            actor=decision.operator or "review")

    # 4) 重算受影响规则 + diff
    post = run_rules(store, stage=None, pack=pack, rule_ids=plan.affected_rules) \
        if plan.affected_rules else []
    diff = _diff_findings(pre, post)

    # 5) 变化的 finding 发 finding.changed，标记重入二次 review（AC5）
    changed_findings = []
    for f in post:
        if f["rule_id"] in set(diff["changed_rules"]):
            f = dict(f)
            f["needs_review"] = True
            f["review_round"] = 2
            f["triggered_by_decision"] = decision.candidate_id
            changed_findings.append(f)
    for rid in diff["changed_rules"]:
        bus.publish("finding.changed",
                    {"rule_id": rid,
                     "decision_id": decision.candidate_id,
                     "new_count": diff["new"].get(rid, 0),
                     "disappeared_count": diff["disappeared"].get(rid, 0),
                     "needs_review": True},
                    actor=decision.operator or "review")

    result = {
        "decision_id": decision.candidate_id,
        "mappings_written": n_map,
        "plan": plan.to_dict(),
        "affected_rules": plan.affected_rules,
        "changed_rules": diff["changed_rules"],
        "new": diff["new"],
        "disappeared": diff["disappeared"],
        "changed_findings": changed_findings,
    }
    conn.execute(
        "INSERT OR REPLACE INTO review_applied (decision_id, applied_at, result_json) "
        "VALUES (?, ?, ?)",
        [decision.candidate_id, _now(),
         json.dumps(result, ensure_ascii=False, default=str)])
    return result


def apply_decisions(store, decisions, *, pack: str = "default") -> list[dict]:
    """批量处理队列中的已决策候选：accept 走重建闭环，其余只写 feedback。"""
    from core.review import Decision
    out = []
    for d in decisions:
        if d.status == Decision.ACCEPTED:
            out.append(apply_accept(store, d, pack=pack))
        else:
            record_feedback(store.conn, d)
            out.append({"decision_id": d.candidate_id,
                        "decision": d.status, "rebuild_triggered": False})
    return out
