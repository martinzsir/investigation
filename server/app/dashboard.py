"""
server/app/dashboard.py
W-018 治理仪表盘组装器：单读连接内编排 core 只读面 + meta ops_events。

纪律：
  - 组装器只编排聚合，不含业务 SQL（读面一律经 core：RunHealth.readonly
    的 health_section/rows、disposal.status_counts）；
  - 单请求单连接单遍取数（各读面 O(诊断行数)，行级筛选在内存完成）；
  - 每节独立降级：某表未生成（如纯 BUILD 无 run_diagnostic）→ 该节
    available=False 计数归零，不 500、不拖垮其余卡片。

双覆盖（REQ-G-008/009 既有产物）：coverage_gap 诊断按 source 分两路并列——
  miaosuan:dimension（声明覆盖）/ miaosuan:dimension:empirical（实证覆盖），
  reason 与 detail.missing（具体维度名）原样透传（AC-4）。
"""
from __future__ import annotations

from typing import Any

from core.disposal import status_counts
from core.registry import ClueStatus
from core.run_health import RunHealth

# 双覆盖两路的 source 标识（与 core/hypotheses.py record 调用同源）
_COVERAGE_DECLARED = "miaosuan:dimension"
_COVERAGE_EMPIRICAL = "miaosuan:dimension:empirical"

_DEFAULT_HEALTH: dict[str, Any] = {
    "available": False, "status": "healthy", "run_id": "",
    "诊断总数": 0, "计数": {"critical": 0, "warning": 0, "info": 0},
    "分类计数": {}, "说明": "运行诊断未生成（纯 BUILD 案件无诊断留痕）",
}


def _latest_run(conn) -> RunHealth:
    return RunHealth.readonly(conn)


def _run_rows(conn) -> list[dict]:
    """最新 run 的诊断行（表未生成 → 空列表）。"""
    try:
        return _latest_run(conn).rows()
    except Exception:
        return []


def assemble(case_id: str, conn, repo=None, state_counts: dict | None = None) -> dict:
    """组装仪表盘首屏（全部只读；任一节失败降级不阻塞整卡）。

    state_counts：M3 处置真值卡片——state.sqlite clue_disposal_status 聚合
    （core.status_counts 同构复用）；None 时回落库内留痕旧路径。
    """
    # 1) 健康度横幅（healthy/degraded/critical + 计数）
    try:
        health = {"available": True, **_latest_run(conn).health_section()}
    except Exception:
        health = dict(_DEFAULT_HEALTH)

    rows = _run_rows(conn)

    # 2) 诊断类别计数（AC-2：零命中/跳过/覆盖缺口/版本锚定/脏值等全覆盖）
    by_kind: dict[str, int] = {}
    by_severity: dict[str, int] = {"info": 0, "warning": 0, "critical": 0}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + 1

    # 3) 双覆盖并列（AC-3/4：声明覆盖 + 实证覆盖，维度名透传）
    def _cov_card(src: str) -> list[dict]:
        return [{"reason": r.get("reason"),
                 "missing": (r.get("detail") or {}).get("missing") or [],
                 "severity": r.get("severity"),
                 "created_at": r.get("created_at")}
                for r in rows
                if r.get("kind") == "coverage_gap" and r.get("source") == src]

    coverage = {
        "declared": _cov_card(_COVERAGE_DECLARED),
        "empirical": _cov_card(_COVERAGE_EMPIRICAL),
    }

    # 4) 待办计数：线索处置（M3 起真值在 state.sqlite；缺省回落库内留痕
    #    聚合）+ 实体裁决（候选为管道运行期构造、无持久化表，后续接线）
    try:
        disposal = state_counts if state_counts is not None else status_counts(conn)
    except Exception:
        disposal = {"available": False, "total": 0,
                    "by_status": {s: 0 for s in (
                        ClueStatus.PENDING, ClueStatus.VERIFYING,
                        ClueStatus.EXCLUDED, ClueStatus.CONFIRMED,
                        ClueStatus.FILED)}}
    disposal["source"] = "state" if state_counts is not None else "version"
    todo = {
        "disposal": disposal,
        "review": {"available": False, "pending": 0,
                   "note": "实体裁决候选无持久化读面（M3 接线）"},
    }

    # 5) ops 摘要（meta ops_events：孤儿扫描/版本回收/归档压实）
    ops: dict[str, Any] = {"recent": [], "by_kind": {}}
    if repo is not None:
        try:
            recent = [e for e in repo.list_ops(limit=100)
                      if e.get("case_id") in ("", case_id)][:20]
            kinds: dict[str, int] = {}
            for e in recent:
                kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
            ops = {"recent": recent, "by_kind": kinds}
        except Exception:
            pass

    return {
        "case_id": case_id,
        "health": health,
        "diagnostics": {"total": len(rows), "by_kind": by_kind,
                        "by_severity": by_severity},
        "coverage": coverage,
        "todo": todo,
        "ops": ops,
    }


def list_diagnostics(conn, *, kind: str | None = None,
                     severity: str | None = None,
                     limit: int = 200) -> list[dict]:
    """诊断行 listing（最新 run；kind/severity 内存过滤，AC-6 下钻入口）。"""
    rows = _run_rows(conn)
    if kind is not None:
        rows = [r for r in rows if r.get("kind") == kind]
    if severity is not None:
        rows = [r for r in rows if r.get("severity") == severity]
    return rows[:max(0, limit)]


def diagnostic_detail(conn, seq: int) -> dict | None:
    """单条诊断下钻（MVP：诊断行本体 + detail 已含关联 ID/维度名/样本）。"""
    for r in _run_rows(conn):
        if int(r.get("seq") or 0) == seq:
            return r
    return None
