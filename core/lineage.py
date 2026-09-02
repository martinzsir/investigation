"""
core/lineage.py
基于血缘的去重与合并（证据链 dedupe + merge）。

问题背景：
  多个技能（虚实/Q2过桥/Q3通话...）可能从不同角度产出指向同一事实的线索，
  若全部堆给正兵，会信息过载。需要根据「血缘相似性」去重合并。

去重依据（血缘三要素）：
  1. 数据溯源重叠度(source_rows)：两条线索引用 >50% 相同原始行 → 同源
  2. 间类归属(jian_types)：生间+反间 同框 → 可合并升格
  3. 假设链(assumption_chain)：同假设推导 → 属同一逻辑链

合并策略：
  - 同源线索 → 合并为一条，取并集(jian_types / source_rows / assumption_chain)
  - 合并后 jian_types 增多 → 可能触发用间交叉升格（单源→双源→三源）
  - 合并记录保留 merge_log，可审计「由哪些原始线索合成」
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from core.registry import LineageClue


# ----------------------------------------------------------------------
# 相似度
# ----------------------------------------------------------------------

def _row_key(r: dict) -> str:
    """把 source_row 归一为一个可哈希的 key。"""
    return str(sorted((k, str(v)) for k, v in r.items()))


def source_overlap(a: LineageClue, b: LineageClue) -> float:
    """两条线索 source_rows 的 Jaccard 相似度。空集合返回 0。"""
    if not a.source_rows or not b.source_rows:
        return 0.0
    sa = {_row_key(r) for r in a.source_rows}
    sb = {_row_key(r) for r in b.source_rows}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ----------------------------------------------------------------------
# 去重 + 合并
# ----------------------------------------------------------------------

def dedupe_and_merge(
    clues: list[LineageClue],
    threshold: float = 0.5,
) -> list[LineageClue]:
    """
    基于血缘去重合并。

    算法（并查集）：
      1. 两两比较 source_overlap，>= threshold 的线索归为同一组
      2. 每组合并为一条 LineageClue：取字段并集、拼接 title
      3. 合并后若 jian_types 增加，触发用间交叉升格检查

    参数:
        clues     : 待去重的线索列表
        threshold : source_rows Jaccard 相似度阈值（默认 0.5）

    返回:
        去重合并后的线索列表（数量 <= 输入）
    """
    if not clues:
        return []

    n = len(clues)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # 1. 两两合并（同一假设链 + source 重叠度高）
    for i in range(n):
        for j in range(i + 1, n):
            a, b = clues[i], clues[j]
            # 同源条件：source 重叠 或 (同假设链 且 间类有交集)
            same_assumption = bool(set(a.assumption_chain) & set(b.assumption_chain))
            jian_intersect = bool(set(a.jian_types) & set(b.jian_types))
            if source_overlap(a, b) >= threshold or (same_assumption and jian_intersect):
                union(i, j)

    # 2. 按组合并
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    merged: list[LineageClue] = []
    for root, idxs in groups.items():
        if len(idxs) == 1:
            merged.append(clues[idxs[0]])
            continue
        # 合并多条 → 一条
        parts = [clues[i] for i in idxs]
        base = parts[0]
        jian = sorted(set(j for c in parts for j in c.jian_types))
        assump = sorted(set(a for c in parts for a in c.assumption_chain))
        rows: list[dict] = []
        seen: set[str] = set()
        for c in parts:
            for r in c.source_rows:
                k = _row_key(r)
                if k not in seen:
                    seen.add(k)
                    rows.append(r)
        merged_clue = LineageClue(
            clue_id=base.clue_id,  # 保留根线索 id，便于追溯
            skill_id=base.skill_id,
            title=" | ".join(sorted({p.title for p in parts if p.title})),
            detail={"merged_from": [p.clue_id for p in parts]},
            assumption_chain=assump,
            source_rows=rows,
            jian_types=jian,
            needs_human_review=any(p.needs_human_review for p in parts),
            定性_policy=base.定性_policy,
        )
        merged.append(merged_clue)

    return merged


# ----------------------------------------------------------------------
# 用间交叉升格（合并后的副产品）
# ----------------------------------------------------------------------

def cross_level(clues: list[LineageClue]) -> str:
    """
    根据线索覆盖的间类数量决定交叉等级。
    单源=观察 → 双源=线索 → 三源=可立案依据候选
    （与 yong_jian.JIAN_MAP 的升格规则保持一致）
    """
    all_jian: set[str] = set()
    for c in clues:
        all_jian.update(c.jian_types)
    n = len(all_jian)
    if n >= 3:
        return "可立案依据候选"
    if n == 2:
        return "线索"
    return "观察"


def lineage_report(clues: list[LineageClue]) -> dict:
    """给正兵操作台的汇总报告：按间类分组 + 升格等级 + 处置状态统计。"""
    by_jian: dict[str, list[str]] = defaultdict(list)
    for c in clues:
        for j in c.jian_types:
            by_jian[j].append(c.clue_id)
    by_status: dict[str, list[str]] = defaultdict(list)
    for c in clues:
        by_status[c.status].append(c.clue_id)
    active = [c for c in clues if c.is_active()]
    return {
        "total_clues": len(clues),
        "active_clues": len(active),            # 仍需跟进
        "jian_coverage": dict(by_jian),
        "cross_level": cross_level(clues),
        "by_status": dict(by_status),           # 处置状态分组
        "clues": [c.to_dict() for c in clues],
    }


# ----------------------------------------------------------------------
# 优先级排序（正兵工作台：先看最值得查的线索）
# ----------------------------------------------------------------------
# 间类覆盖权重：越"硬"的数据源权重越高（反间 > 因间 > 死间 > 生间 > 内间）
_JIAN_WEIGHT = {"内间": 5, "死间": 4, "因间": 3, "反间": 2, "生间": 1}


def prioritize_clues(
    clues: list[LineageClue],
    assumption_confidence: Optional[dict[str, float]] = None,
) -> list[LineageClue]:
    """
    对线索按 假设置信度 × 间类覆盖度 × 数据强度 三维打分，降序排列（原地返回新列表）。

    公式：
        score = confidence*0.4 + jian_coverage*0.35 + data_strength*0.25
        confidence     : 假设置信度（默认 H1=0.9, 其余=0.7）
        jian_coverage  : 命中间类的最大权重 / 5
        data_strength  : min(1.0, 溯源行数 / 10)

    每条线索被打上 priority_score 字段（detail 里），供操作台排序展示。
    """
    assumption_confidence = assumption_confidence or {"H1": 0.9, "H4": 0.7}

    def score(c: LineageClue) -> float:
        # 假设置信度：取该线索关联假设的最高置信度
        conf = max(
            (assumption_confidence.get(h, 0.7) for h in c.assumption_chain),
            default=0.7,
        )
        # 间类覆盖度：命中间类权重之和归一（0~1）
        jian = sum(_JIAN_WEIGHT.get(j, 1) for j in c.jian_types)
        jian_cov = min(1.0, jian / 5.0)
        # 数据强度：溯源行数（封顶 1.0，避免"行数多=一定对"）
        data_str = min(1.0, len(c.source_rows) / 10.0)
        return conf * 0.4 + jian_cov * 0.35 + data_str * 0.25

    scored = sorted(clues, key=score, reverse=True)
    for i, c in enumerate(scored):
        c.detail = {**c.detail, "priority_rank": i + 1,
                    "priority_score": round(score(c), 3)}
    return scored


# ----------------------------------------------------------------------
# 处置状态持久化（落到 L2 DuckDB，正兵跨会话跟踪）
# ----------------------------------------------------------------------

_STATUS_TABLE = "clue_disposal_status"


def ensure_status_table(conn) -> None:
    """建状态表（幂等）。仅存状态机所需最小字段，审计链存在 LineageClue.audit_log 里。"""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_STATUS_TABLE} (
            clue_id      VARCHAR PRIMARY KEY,
            status       VARCHAR NOT NULL,
            note         VARCHAR DEFAULT '',
            operator     VARCHAR DEFAULT '',
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def save_statuses(conn, clues: list[LineageClue]) -> int:
    """
    把线索当前处置状态批量落 DuckDB。
    采用 INSERT ... ON CONFLICT DO UPDATE，幂等可重跑；
    返回实际写入行数。
    """
    ensure_status_table(conn)
    from datetime import datetime
    now = datetime.now()
    rows = [
        (c.clue_id, c.status, c.note,
         (c.audit_log[-1]["operator"] if c.audit_log else ""), now)
        for c in clues
    ]
    if not rows:
        return 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for clue_id, status, note, operator, ts in rows:
            conn.execute(
                f"INSERT INTO {_STATUS_TABLE} (clue_id, status, note, operator, updated_at) "
                f"VALUES (?, ?, ?, ?, ?) "
                f"ON CONFLICT (clue_id) DO UPDATE SET "
                f"  status=excluded.status, note=excluded.note, "
                f"  operator=excluded.operator, updated_at=excluded.updated_at",
                (clue_id, status, note, operator, ts),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def load_statuses(conn, clues: list[LineageClue]) -> int:
    """
    从 DuckDB 回灌处置状态到内存线索（恢复上次会话进度）。
    仅回填存在的行，未记录的线索保持 待查。
    返回实际回灌条数。
    """
    ensure_status_table(conn)
    if not clues:
        return 0
    placeholders = ",".join(["?"] * len(clues))
    ids = [c.clue_id for c in clues]
    rows = conn.execute(
        f"SELECT clue_id, status, note FROM {_STATUS_TABLE} "
        f"WHERE clue_id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    n = 0
    for c in clues:
        if c.clue_id in by_id:
            status, note = by_id[c.clue_id][1], by_id[c.clue_id][2]
            # 只回灌终态/已变更状态；审计链以内存为准，不做覆盖
            if status != c.status:
                c.status = status
                c.note = note or c.note
                n += 1
    return n
