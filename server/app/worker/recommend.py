"""
server/app/worker/recommend.py
W-P-007 数据元智能推荐快速通道。

  - TASK_DE_RECO：读暂存上传件（uploads/<uid>.<fmt>）采样列值，经
    core.de_recommend 推荐列→数据元映射，落 state.de_recommendation
    （状态恒"待核实"）；同 upload_id 幂等（已有推荐直接回跳，不重复生成）。
  - TASK_DE_DECIDE：采纳/驳回裁决——**只更新 state 状态 + 审计链留痕，
    永不自动改写 bindings/数据元配置**（红线：推荐不自动生效）。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from core.de_recommend import recommend_for_table
from core.ontology_loader import load_data_elements

from server.app import ingest_io
from server.app.store.state_store import StateStore
from server.app.worker.tasks import TaskExecError

_DE_CONF = {"high": 0.9, "medium": 0.6}
_SAMPLE_ROWS = 50
_DECISIONS = {"adopt": "采纳", "reject": "驳回"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def handle_de_reco(task, *, repo, factory, snapshot_base_for,
                   **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    params = task.params or {}
    upload_id = str(params.get("upload_id") or "").strip()
    operator = str(params.get("operator") or "")
    if not upload_id:
        raise TaskExecError("UPLOAD_REQUIRED", "缺少 upload_id")

    state = StateStore(case.id, factory.case_dir(case.id) / "state.sqlite")
    try:
        existing = state.find_de_reco_by_upload(upload_id)
        if existing is not None:
            return {"rid": existing["rid"], "reused": True,
                    "status": existing["status"]}

        src = repo.get_source(task.case_id, upload_id)
        if src is None:
            raise TaskExecError("UPLOAD_NOT_FOUND",
                                f"上传件不存在：{upload_id}")
        staged = (factory.case_dir(task.case_id) / "uploads"
                  / f"{upload_id}.{src['fmt']}")
        if not staged.exists():
            raise TaskExecError("UPLOAD_NOT_FOUND",
                                f"暂存文件已丢失：{staged.name}")
        try:
            df = ingest_io.read_table(staged, src["fmt"])
        except Exception as e:
            raise TaskExecError("PARSE_FAILED", f"解析失败：{e}")

        base = snapshot_base_for(task.case_id)
        elements = load_data_elements(case.pack_id, base)
        cols = [str(c) for c in df.columns]
        col_values: dict[str, list] = {}
        for c in cols:
            try:
                vals = [v for v in df[c].dropna().tolist()[:_SAMPLE_ROWS]
                        if v is not None]
            except Exception:
                vals = []
            col_values[c] = vals

        recommendations: list[dict] = []
        for r in recommend_for_table(cols, col_values, elements):
            recs = r.get("recommendations") or []
            top = next((x for x in recs if x.get("data_element")), None)
            if top is None:
                continue
            recommendations.append({
                "col": r["col"],
                "element_id": top["data_element"],
                "element_name": top.get("de_name"),
                "confidence": _DE_CONF.get(top.get("confidence"), 0.6),
                "evidence": {"match_values": (col_values.get(r["col"])
                                              or [])[:3]},
            })

        rid = f"der_{uuid.uuid4().hex[:12]}"
        state.save_de_reco(rid=rid, upload_id=upload_id, created_at=_now(),
                           created_by=operator,
                           recommendations=recommendations)
        return {"rid": rid, "reused": False, "status": "待核实",
                "recommendations": len(recommendations)}
    finally:
        state.close()


def handle_de_decide(task, *, repo, factory, **_: Any) -> dict[str, Any]:
    case = repo.get_case(task.case_id)
    if case is None:
        raise TaskExecError("CASE_NOT_FOUND", f"案件不存在：{task.case_id}")
    params = task.params or {}
    rid = str(params.get("rid") or "").strip()
    decision = str(params.get("decision") or "").strip()
    operator = str(params.get("operator") or "")
    note = str(params.get("note") or "")
    if not rid:
        raise TaskExecError("RID_REQUIRED", "缺少 rid")
    if decision not in _DECISIONS:
        raise TaskExecError("INVALID_DECISION",
                            f"decision 非法：{decision}（adopt|reject）")

    state = StateStore(case.id, factory.case_dir(case.id) / "state.sqlite")
    try:
        rec = state.get_de_reco(rid)
        if rec is None:
            raise TaskExecError("RECO_NOT_FOUND", f"推荐不存在：{rid}")
        status = _DECISIONS[decision]
        updated = state.decide_de_reco(
            rid, status=status, decided_by=operator,
            decided_at=_now(), note=note)
        # 审计留痕（只记 state；不改 bindings/数据元配置）
        state.insert_decision(
            kind="de_reco_decide", target_id=rid, verdict=status,
            decided_by=operator,
            payload={"upload_id": rec.get("upload_id"), "decision": decision,
                     "note": note})
        return {"rid": rid, "status": updated["status"] if updated else status}
    finally:
        state.close()
