"""
server/app/routers/anomaly.py
W-022 异常线索通道（M4 阶段 B）。

读：GET /cases/{cid}/anomalies —— 从案件库 run_diagnostic 表读全部诊断，
    经 core.anomaly_channel.emit_anomaly_clues 转换为异常线索（按主体
    聚合）；级别恒"待核实"、needs_human_review=True、携带 diagnostic_ids。
红线 AC-4：异常线索不参与五间交叉等级计算（core anomaly_channel 已硬编码，
    Web 仅分区展示，不混入正常线索流）。
权限：登录即可。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from core.anomaly_channel import emit_anomaly_clues

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ok
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal

router = APIRouter(tags=["anomaly"])


class _AllRowsHealth:
    """伪 health：rows() 返回 run_diagnostic 全表（不限 run_id）。

    避开 RunHealth.__init__ 的 DDL（只读版本库不可 CREATE）。
    """

    def __init__(self, conn):
        self._rows: list[dict] = []
        try:
            cur = conn.execute(
                "SELECT run_id, seq, kind, severity, source, reason, detail, "
                "created_at FROM run_diagnostic ORDER BY run_id, seq")
        except Exception:
            return  # 表不存在（BUILD 未产诊断）→ 空
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except Exception:
                    pass
            self._rows.append(d)

    def rows(self, run_id: str | None = None) -> list[dict]:
        return self._rows


@router.get("/cases/{case_id}/anomalies")
def list_anomalies(case_id: str,
                   p: Principal = Depends(get_principal),
                   ctx: WebContext = Depends(get_ctx)):
    _get_owned_case(case_id, p, ctx.cases)
    items: list[dict] = []
    store = None
    try:
        try:
            store = ctx.factory.for_case(case_id, mode="read")
            health = _AllRowsHealth(store.read_conn)
            items = emit_anomaly_clues(health, record=False)
        except FileNotFoundError:
            pass  # 未 BUILD → 空
    finally:
        if store is not None:
            store.close()
    return ok({
        "items": items,
        "total": len(items),
        "level": "待核实",
        "note": "异常线索不参与五间交叉等级计算（W-022 AC-4 红线）",
    }, data_version=ctx.repo.current_version(case_id))
