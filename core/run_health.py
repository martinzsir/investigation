"""
core/run_health.py
运行诊断与健康度层（REQ-G-010，统一降级协议的出口）。

设计（见 .trae/documents/REQ-G实施计划_统一降级协议.md）：
  - 系统里"失败被表达成数据"的统一收口：规则零命中、函数空转/降级、事件发布失败、
    唤醒条件不可解析、实体解析跳过/插件失败、版本锚定缺失、派发失败、推翻率告警、
    审计完备性缺口、覆盖缺口、异常线索等，一律落 run_diagnostic 表，而非 print/吞掉。
  - 诊断与 findings 主列表分离：诊断进 run_diagnostic（及产物"健康度"小节），
    绝不污染线索/findings，绝不参与五间交叉升格。
  - 失败可见 ≠ 失败容错：本模块只"留痕"，不把任何硬失败改成放行。

兼容红线：所有接入点签名 health=None；None 时落 NullRunHealth（record 空操作），
现有不传参调用行为零变化。

run_diagnostic 为运行期表（CREATE IF NOT EXISTS），语义层重建编译器只 DROP obj_*/lnk_*，
诊断不丢（与 event_log / audit_chain 同级）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any


# 诊断类型枚举（kind）——新增静默点在此登记，便于汇总口径统一
KINDS = (
    "rule_zero_hit",              # 规则零命中（zero_type: data_absent/config_missing/empty_result_suspect/clean_scan）
    "function_empty_degraded",    # 函数空转/结构降级（表/列缺失、配置缺失导致无法计算）
    "event_publish_failed",       # 事件落盘失败（调用方原先 except: pass）
    "event_dead_letter_summary",  # 事件死信汇总（event_dead_letter 计数）
    "wake_condition_unparseable", # 唤醒条件无法解析（任务转 condition_error）
    "entity_table_skipped",       # 实体解析缺表/缺列跳过
    "entity_plugin_failed",       # 实体解析插件加载失败，回退内置规则
    "version_anchor_missing",     # 本体版本号取不到，审计/血缘锚定 unknown
    "dispatch_failed",            # Action 派发失败（fail-closed，不得停在 dispatching）
    "override_rate_alert",        # 规则推翻率超阈
    "audit_integrity_gap",        # 审计链完备性缺口（记录数/必填字段）
    "coverage_gap",               # 庙算覆盖缺口
    "anomaly_clue_emitted",       # 异常线索已产出（REQ-G-019）
    "profile_missing_column",     # REQ-P-012/021：对象已物化但表缺列（schema 演进）
    "profile_unmaterialized",     # REQ-P-021：画像对象未物化（info，非错误）
    "map_normalize_gap",          # REQ-P-021：数据地图检出归一缺口（M 波 build_sql 未 JOIN 实体表）
)

SEVERITIES = ("info", "warning", "critical")
_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}

_DDL = """CREATE TABLE IF NOT EXISTS run_diagnostic (
    run_id VARCHAR NOT NULL,
    seq BIGINT NOT NULL,
    kind VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    source VARCHAR,
    reason VARCHAR,
    detail VARCHAR,
    created_at VARCHAR NOT NULL
)"""


def _as_conn(db: Any):
    """接受 Store（.conn）或裸 duckdb 连接。"""
    return getattr(db, "conn", db)


class RunHealth:
    """运行诊断记录器与健康度汇总。"""

    def __init__(self, db: Any, run_id: str | None = None):
        self._conn = _as_conn(db)
        self.run_id = run_id or uuid.uuid4().hex
        self._dropped: list[dict] = []  # 诊断层自身写入失败的内存兜底（实例隔离）
        self._conn.execute(_DDL)
        self._seq = self._next_seq()

    def _next_seq(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_diagnostic "
                "WHERE run_id=?", [self.run_id]).fetchone()
            return int(row[0]) if row else 1
        except Exception:
            return 1

    def record(self, kind: str, severity: str = "info", source: str = "",
               reason: str = "", **detail: Any) -> None:
        """落一条诊断。kind/severity 非法即编程错误（fail-loud）。

        诊断写入本身失败不应拖垮主流程：兜底进内存 _dropped 并在 summary 计数，
        不静默吞掉（避免重蹈"失败不可见"）。
        """
        if kind not in KINDS:
            raise ValueError(f"未知诊断 kind：{kind}（合法值见 run_health.KINDS）")
        if severity not in SEVERITIES:
            raise ValueError(f"非法 severity：{severity}（合法 {SEVERITIES}）")
        seq = self._seq
        self._seq += 1
        try:
            self._conn.execute(
                """INSERT INTO run_diagnostic
                   (run_id, seq, kind, severity, source, reason, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [self.run_id, seq, kind, severity, source, reason,
                 json.dumps(detail, ensure_ascii=False, default=str),
                 datetime.now().isoformat(timespec="seconds")])
        except Exception as e:  # 诊断层自身故障：不阻断侦查，但留内存痕迹
            self._dropped.append({"kind": kind, "error": str(e)})

    # ------------------------------------------------------------------
    # 查询 / 汇总
    # ------------------------------------------------------------------
    def rows(self, run_id: str | None = None) -> list[dict]:
        rid = run_id or self.run_id
        cur = self._conn.execute(
            "SELECT run_id, seq, kind, severity, source, reason, detail, created_at "
            "FROM run_diagnostic WHERE run_id=? ORDER BY seq", [rid])
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except Exception:
                    pass
            out.append(d)
        return out

    def _dead_letter_count(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM event_dead_letter").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def summary(self, run_id: str | None = None) -> dict:
        """按 kind/severity 聚合并拼接跨表信号（死信计数等）。"""
        rows = self.rows(run_id)
        by_kind: dict[str, int] = {}
        by_severity: dict[str, int] = {s: 0 for s in SEVERITIES}
        zero_hit: list[dict] = []
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
            by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + 1
            if r["kind"] == "rule_zero_hit":
                d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
                zero_hit.append({
                    "rule_id": d.get("rule_id"),
                    "zero_type": d.get("zero_type"),
                    "scan_rows": d.get("scan_rows"),
                    "matched_rows": d.get("matched_rows"),
                })
        dead_letter = self._dead_letter_count()
        if dead_letter:
            by_kind["event_dead_letter_summary"] = dead_letter
        return {
            "run_id": run_id or self.run_id,
            "total": len(rows),
            "by_kind": by_kind,
            "by_severity": by_severity,
            "zero_hit_rules": zero_hit,
            "event_dead_letter": dead_letter,
            "health_self_dropped": len(self._dropped),
        }

    def health_section(self, run_id: str | None = None) -> dict:
        """产物首部"健康度"小节：字段固定、位置在所有结论字段之前。"""
        s = self.summary(run_id)
        sev = s["by_severity"]
        if sev.get("critical"):
            status = "critical"
        elif sev.get("warning") or s["event_dead_letter"]:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "status": status,
            "run_id": s["run_id"],
            "诊断总数": s["total"],
            "计数": {
                "critical": sev.get("critical", 0),
                "warning": sev.get("warning", 0),
                "info": sev.get("info", 0),
            },
            "分类计数": s["by_kind"],
            "零命中规则": s["zero_hit_rules"],
            "事件死信": s["event_dead_letter"],
            "说明": "健康度为运行留痕，不参与线索升格；warning/critical 项需人工复核。",
        }


class NullRunHealth:
    """空操作实现：health=None 时使用，保证既有不传参调用零行为变化。"""

    run_id = ""

    def record(self, *args, **kwargs) -> None:  # noqa: D401
        return None

    def rows(self, run_id: str | None = None) -> list[dict]:
        return []

    def summary(self, run_id: str | None = None) -> dict:
        return {"run_id": "", "total": 0, "by_kind": {}, "by_severity":
                {s: 0 for s in SEVERITIES}, "zero_hit_rules": [],
                "event_dead_letter": 0, "health_self_dropped": 0}

    def health_section(self, run_id: str | None = None) -> dict:
        return {"status": "healthy", "run_id": "", "诊断总数": 0,
                "计数": {s: 0 for s in SEVERITIES}, "分类计数": {},
                "零命中规则": [], "事件死信": 0,
                "说明": "未启用运行诊断（health=None）。"}


def get_health(health) -> RunHealth | NullRunHealth:
    """统一入口：None → NullRunHealth；RunHealth 原样返回。"""
    if health is None:
        return NullRunHealth()
    return health
