"""
server/app/worker/lifecycle_audit.py
B5 生命周期事件补录：在建案/导入/构建成功后向 audit_chain 追加事件。

与 append_config_event（state_sink.py L29-57）同模式：
打开 per-case state.sqlite → AuditChain.append → 关闭。
失败不阻断主流程（调用方 try/except 降级 ops 留痕）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.audit import AuditChain
from server.app.store.state_store import StateStore


def _append_lifecycle(*, state_path: Path, case_id: str,
                       operator: str, ontology_version: str,
                       event: str, detail: dict[str, Any]) -> None:
    """向 audit_chain 追加生命周期事件。

    event ∈ case_created / source_imported / build_succeeded
    detail 作为 after_state 落链（含事件类型+业务上下文）。
    """
    store = StateStore(case_id, state_path)
    try:
        chain = AuditChain(store.conn, case_id, backend="sqlite",
                           ontology_version=ontology_version)
        after = {"event": event, "by": operator,
                 "ontology_version": ontology_version, **detail}
        chain.append(operator=operator, before=None, after=after,
                     source_row_ids=[], ontology_version=ontology_version)
    finally:
        store.close()


def on_case_created(*, case_dir: Path, case_id: str,
                     operator: str, pack_id: str) -> None:
    """建案后补录 case_created 事件。"""
    state_path = case_dir / "state.sqlite"
    _append_lifecycle(
        state_path=state_path, case_id=case_id,
        operator=operator, ontology_version="待建案",
        event="case_created",
        detail={"pack_id": pack_id})


def on_source_imported(*, case_dir: Path, case_id: str,
                        operator: str, version: int,
                        upload_id: str, table: str,
                        rows: int) -> None:
    """数据导入后补录 source_imported 事件。"""
    state_path = case_dir / "state.sqlite"
    _append_lifecycle(
        state_path=state_path, case_id=case_id,
        operator=operator, ontology_version=f"v{version}",
        event="source_imported",
        detail={"upload_id": upload_id, "table": table, "rows": rows})


def on_build_succeeded(*, case_dir: Path, case_id: str,
                         operator: str, prev_version: int,
                         new_version: int, objects: int,
                         links: int) -> None:
    """构建成功后补录 build_succeeded 事件。"""
    state_path = case_dir / "state.sqlite"
    _append_lifecycle(
        state_path=state_path, case_id=case_id,
        operator=operator, ontology_version=f"v{new_version}",
        event="build_succeeded",
        detail={"prev_version": prev_version, "new_version": new_version,
                "objects": objects, "links": links})
