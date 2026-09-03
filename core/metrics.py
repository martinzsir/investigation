"""
core/metrics.py
REQ-030 规则运行时度量。

表结构 rule_run_metric（编译器不会 DROP，类似 ensure_runtime_tables）：
  run_id VARCHAR PK   —— 同一"次运行"的标识（sha1(pack + date + causal_id)）
  rule_id VARCHAR     —— 规则 ID
  rule_version VARCHAR—— hash(rule_id + rule_text + sorted params)，AC4 按版本分层
  evaluated BIGINT    —— 当次跑了 N 行样本/次
  hit BIGINT          —— 命中 N 行（≤ evaluated）
  later_verified BIGINT —— 后验：被人工"已固证/保留"
  later_excluded BIGINT —— 后验：被人工"已排除"
  override_count BIGINT —— 分析师 override（跳过/改阈值）次数
  recorded_at TIMESTAMP

对外函数：
  ensure_rule_run_metric(conn)              — 建表（幂等）
  record_run(conn, run_id, rule_metrics)    — 写入每规则一行（AC1：每规则一行）
  verdict_backfill(conn, run_id, rule_id, verdict, override)  — AC2 回流
  hit_rate(row) / precision_estimate(row) / override_rate(row) — AC3 三个聚合
  rule_version(rule_spec)                   — 规则版本号计算
  list_metrics(conn, rule_id, access)       — AC5 权限控制（低权限敏感规则被遮蔽）
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

TABLE = "rule_run_metric"


def ensure_rule_run_metric(conn) -> None:
    """幂等建表；不会被 ontology 编译器 DROP（走 DuckDB CREATE IF NOT EXISTS + 表名不在 obj_/lnk_）。

    rule_version 进 PK，保证同一规则改参数后新旧版本指标不混（AC4 分层）。
    """
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            run_id          VARCHAR   NOT NULL,
            rule_id         VARCHAR   NOT NULL,
            rule_version    VARCHAR   NOT NULL,
            evaluated       BIGINT    NOT NULL DEFAULT 0,
            hit             BIGINT    NOT NULL DEFAULT 0,
            later_verified  BIGINT    NOT NULL DEFAULT 0,
            later_excluded  BIGINT    NOT NULL DEFAULT 0,
            override_count  BIGINT    NOT NULL DEFAULT 0,
            recorded_at     TIMESTAMP NOT NULL,
            PRIMARY KEY (run_id, rule_id, rule_version)
        )
    """)
    # 旧库迁移：若表已存在但 PK 不含 rule_version（legacy schema），加列到末尾（DuckDB ALTER 不支持改 PK，只能提醒重建）
    try:
        rows = conn.execute("PRAGMA main.table_info('rule_run_metric')").fetchall()
    except Exception:
        rows = []
    if rows:
        cols = [r[1] for r in rows] if len(rows[0]) >= 2 else []
        if cols and "rule_version" not in cols:
            raise RuntimeError(
                "rule_run_metric 表结构过旧（缺 rule_version 列/PK 不含），"
                "请删除 investigation.duckdb 或手工重建 rule_run_metric（REQ-030 AC4）。")


def rule_version(rule_spec: Any) -> str:
    """hash(rule_id + rule_text + sorted(params)) —— AC4 版本分层。"""
    d = {
        "id": getattr(rule_spec, "id", None),
        "rule_text": getattr(rule_spec, "rule_text", None),
        "params": dict(getattr(rule_spec, "params", {}) or {}),
    }
    s = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def record_run(conn, run_id: str, per_rule: list[dict]) -> None:
    """批量写入；per_rule = [{rule_id, rule_version, evaluated, hit}]。

    AC1：每条规则一行（len(per_rule) 应等于规则总数，如 6 条）。
    幂等：若 PK(run_id, rule_id) 存在则跳过（不覆盖历史）。
    """
    ensure_rule_run_metric(conn)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    for m in per_rule:
        conn.execute(f"""
            INSERT OR IGNORE INTO {TABLE}
                (run_id, rule_id, rule_version, evaluated, hit,
                 later_verified, later_excluded, override_count, recorded_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?)
        """, [
            run_id,
            str(m["rule_id"]),
            str(m.get("rule_version") or ""),
            int(m.get("evaluated", 0)),
            int(m.get("hit", 0)),
            now,
        ])


def verdict_backfill(conn, run_id: str, rule_id: str,
                     verdict: str, override: bool = False,
                     rule_version: str | None = None) -> None:
    """AC2：人工 verdict 回流。verdict ∈ {verified, excluded}。

    verified → later_verified += 1；excluded → later_excluded += 1；
    override=True → override_count += 1。rule_version 指定时按版本定位，
    否则所有同 run_id+rule_id 行一并更新（兼容未传版本的人工反馈）。
    """
    if verdict not in ("verified", "excluded"):
        raise ValueError(f"verdict 仅允许 verified/excluded，实际 {verdict!r}")
    ensure_rule_run_metric(conn)
    wh = "WHERE run_id = ? AND rule_id = ?"
    args: list = [run_id, rule_id]
    if rule_version is not None:
        wh += " AND rule_version = ?"
        args.append(rule_version)
    ov = 1 if override else 0
    if verdict == "verified":
        conn.execute(f"""
            UPDATE {TABLE} SET later_verified = later_verified + 1,
                               override_count   = override_count + ?
            {wh}
        """, [ov] + args)
    else:
        conn.execute(f"""
            UPDATE {TABLE} SET later_excluded = later_excluded + 1,
                               override_count   = override_count + ?
            {wh}
        """, [ov] + args)


def hit_rate(row: dict) -> float | None:
    """AC3：命中率 = hit / evaluated。evaluated=0 时返回 None（无意义）。"""
    d = int(row.get("evaluated", 0) or 0)
    if d <= 0:
        return None
    return float(row.get("hit", 0)) / d


def precision_estimate(row: dict) -> float | None:
    """AC3：精确率估计 = later_verified / (later_verified + later_excluded)。分母 0 → None。"""
    v = int(row.get("later_verified", 0) or 0)
    e = int(row.get("later_excluded", 0) or 0)
    if v + e <= 0:
        return None
    return v / (v + e)


def override_rate(row: dict) -> float | None:
    """AC3：override_rate = override_count / evaluated。evaluated=0 → None。"""
    d = int(row.get("evaluated", 0) or 0)
    if d <= 0:
        return None
    return float(row.get("override_count", 0)) / d


# REQ-030 AC5：权限遮蔽规则 ID 集合（缺省假设 R4（同框轨迹）/ R3（通话频次）属敏感规则——
# 实际应与 PolicyEngine 对齐；此模块以 policy 对象级检查为准）
def _is_sensitive_rule(rule_id: str) -> bool:
    """轨迹/通话/法人亲属登记等敏感规则。"""
    return rule_id in {"R3", "R4"}


def list_metrics(conn, rule_id: str | None = None, access=None,
                 pack: str = "default") -> list[dict]:
    """查询指标并对 access 做策略遮蔽（AC5）。

    - access=None → system 全显；
    - access 非 system 且 role.rank < 偏将：敏感规则指标（R3/R4）只返回数量级摘要
      （evaluated/hit 取 10 的量级舍入 + _access_note），不改列名。
    """
    ensure_rule_run_metric(conn)
    from core.access import system_context, ROLE_RANK  # type: ignore
    ctx = access if access is not None else system_context()
    try:
        from core.policy import PolicyEngine
        policy = PolicyEngine(pack)
    except Exception:
        policy = None

    sql = f"SELECT * FROM {TABLE}"
    params = []
    if rule_id:
        sql += " WHERE rule_id = ?"
        params.append(rule_id)
    sql += " ORDER BY run_id, rule_id"
    rows = conn.execute(sql, params).fetchall()
    cols = [d[0] for d in conn.description] if rows else []
    out = [dict(zip(cols, r)) for r in rows]

    # 权限分级
    try:
        is_sys = bool(ctx.is_system)
    except Exception:
        is_sys = False
    is_low = (not is_sys) and (
        ROLE_RANK.get(getattr(ctx, "role", ""), 0) < ROLE_RANK.get("偏将", 100)
    )
    if not is_low:
        return out
    for row in out:
        rid = row.get("rule_id", "")
        # 用 PolicyEngine 查对象敏感策略（若有）；fallback：_is_sensitive_rule
        sens = False
        if policy:
            try:
                # 把 rule 当作"对象"名（rule:{rid}），按策略判定
                policy.check_object(ctx, f"rule_{rid}")
            except Exception as e:
                if "无权" in str(e) or "denied" in str(e).lower():
                    sens = True
        if not sens:
            sens = _is_sensitive_rule(rid)
        if sens:
            # 遮蔽：数值舍入到 10 进制量级（0→0，1-9→10，10+ → round(n/10)*10），
            # 敏感列 later_verified/later_excluded 置空
            for col in ("evaluated", "hit"):
                v = int(row.get(col) or 0)
                if v == 0:
                    row[col] = 0
                elif v < 10:
                    row[col] = 10
                else:
                    row[col] = (v // 10) * 10
            for col in ("later_verified", "later_excluded", "override_count"):
                row[col] = None
            row["_access_note"] = (
                f"operator={ctx.operator} role={ctx.role}：规则 {rid} 指标被策略遮蔽，"
                "仅返回数值量级摘要（REQ-030 AC5）。"
            )
    return out
