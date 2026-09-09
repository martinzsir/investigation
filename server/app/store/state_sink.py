"""
server/app/store/state_sink.py
D1 写路径适配（决策 D-M3-1）：core ActionExecutor / DisposalBoard / AuditChain
在 Web 处置快速通道下写 per-case state.sqlite，而不是不可变版本文件。

窄协议（鸭子类型；core 不 import 本模块，依赖方向 server → core）：
  - .conn               sqlite3 连接：action_request 两阶段 SQL 与
                        lineage.save_statuses/load_statuses 方言兼容，
                        core 既有 SQL 直接复用（不在 server 重写写逻辑）；
  - .case_id            审计链 case_id 列；
  - .ontology_version   审计链版本锚点（Worker 取案件当前数据版本 vN）；
  - create_decision(spec, clue, operator, params)：
                        file 副作用写 state.review_decision（替代版本文件
                        obj_decision/lnk_decision_for 语义表，决策不随 BUILD 丢失）。

红线：本模块不含校验逻辑——角色/必填参数/状态机/权限上下文四步校验全部在
core ActionExecutor._validate()，Web 与 CLI 单点维护。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.audit import AuditChain

from server.app.store.state_store import StateStore


def append_config_event(*, state_path: str | Path, case_id: str,
                        operator: str, op: str, filename: str,
                        ontology_version: str, reason: str = "",
                        summary: dict[str, Any] | None = None) -> None:
    """配置写追加进案件审计链（FE-T-012）：打开 per-case state.sqlite，
    向 audit_chain 哈希链追加 after_state.config_action 事件后关闭。

    after_state 带 config_action/config_file/note/by（+可选 summary）；
    timeline 的 action 派生为 "config"（core.audit._event_action），
    note 列落变更理由。配置整包不进链（快照文件本身可溯）。

    失败向上抛（调用方负责降级 ops 留痕，不阻断已生效的配置写）。
    """
    store = StateStore(case_id, state_path)
    try:
        chain = AuditChain(store.conn, case_id, backend="sqlite",
                           ontology_version=ontology_version)
        after: dict[str, Any] = {
            "config_action": op,
            "config_file": filename,
            "note": (reason or "").strip(),
            "by": operator,
        }
        if summary:
            after["summary"] = summary
        chain.append(operator=operator, before=None, after=after,
                     source_row_ids=[], ontology_version=ontology_version)
    finally:
        store.close()


class StateSink:
    """core 写汇聚点的 state.sqlite 后端（Worker 处置/裁决任务构造）。"""

    def __init__(self, state_store: StateStore, *,
                 ontology_version: str = "unknown"):
        self._state = state_store
        self.case_id = state_store.case_id
        self.ontology_version = ontology_version or "unknown"

    @property
    def conn(self):
        """sqlite 写连接（core 按 .conn 鸭子类型访问，同 Store 协议）。"""
        return self._state.conn

    def create_decision(self, spec, clue, operator: str,
                        params: dict[str, Any]) -> dict:
        """file 动作副作用：决策落 state.review_decision。

        与 DuckDB 路径 _create_decision 返回同构 dict（decision_id/persisted/
        created_at），_apply 调用方无感知。
        """
        return self._state.insert_decision(
            kind=f"action:{spec.name}",
            target_id=getattr(clue, "clue_id", None),
            verdict=spec.target_status,
            decided_by=operator,
            payload={
                "action": spec.name,
                "clue_id": getattr(clue, "clue_id", None),
                "title": getattr(clue, "title", ""),
                "legal_basis": params.get("legal_basis", ""),
                "note": params.get("note", ""),
            })

    def close(self) -> None:
        self._state.close()
