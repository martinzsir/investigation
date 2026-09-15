"""
server/app/ontology_proposals.py
S5-F4 本体变更提案（轻量版）存储：per-case ontology_proposals.sqlite。

状态机（S5 PRD §9.2，轻量版无多人评审）：
  draft → impact_ready → published（终止）
                    ↘ discarded（终止）

🔴 红线：
  R4 提案绝不自动发布——唯一发布入口是 publish()，由人工点击触发；
     worker / 定时任务 / LLM 均不读本库（无任何自动路径）。
  E4-1 未看影响面（draft）直接发布 → 服务端拒绝。
  E4-3 attach 时锁定影响面指纹，发布前重算，不一致 → 拒绝并要求重新评估。
  R3 发布只落本体文件 + 审计/版本历史，绝不迁移已生成线索。

与 core.proposal.ProposalStore（LLM 只读建议信封，pp- 前缀，DuckDB）
 deliberately 分离：本库存的是「本体文件整体变更」，op- 前缀，sqlite。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

DRAFT = "draft"
IMPACT_READY = "impact_ready"
PUBLISHED = "published"
DISCARDED = "discarded"

_TERMINAL = frozenset({PUBLISHED, DISCARDED})

_DDL = """
CREATE TABLE IF NOT EXISTS ontology_proposal (
    proposal_id      TEXT PRIMARY KEY,
    file             TEXT NOT NULL,
    doc_json         TEXT NOT NULL,
    reason           TEXT NOT NULL DEFAULT '',
    author           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft',
    impact_json      TEXT,
    impact_fprint    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    published_at     TEXT,
    published_by     TEXT,
    discarded_by     TEXT,
    ontology_version TEXT
)
"""


class ProposalError(ValueError):
    """提案状态机/参数违规（路由层转 400/409）。"""


class OntologyProposalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.execute(_DDL)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["doc"] = json.loads(d.pop("doc_json"))
        d["impact"] = json.loads(d["impact_json"]) if d["impact_json"] else None
        return d

    def create(self, *, file: str, doc: dict, reason: str,
               author: str) -> dict:
        pid = f"op-{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as con:
            con.execute(
                "INSERT INTO ontology_proposal "
                "(proposal_id, file, doc_json, reason, author, status, "
                " created_at, updated_at) VALUES (?,?,?,?,?, 'draft', ?,?)",
                [pid, file, json.dumps(doc, ensure_ascii=False),
                 reason, author, now, now])
        return self.get(pid)  # type: ignore[return-value]

    def get(self, pid: str) -> dict | None:
        with self._conn() as con:
            r = con.execute(
                "SELECT * FROM ontology_proposal WHERE proposal_id = ?",
                [pid]).fetchone()
        return self._row(r) if r else None

    def attach_impact(self, pid: str, impact: dict,
                      fingerprint: str) -> dict:
        """draft → impact_ready；impact_ready 允许重新评估（E4-3 基准变化后
        必须能刷新影响面与指纹再发布）。影响面由服务端计算，客户端不可自报。"""
        rec = self.get(pid)
        if rec is None:
            raise ProposalError(f"提案不存在：{pid}")
        if rec["status"] not in (DRAFT, IMPACT_READY):
            raise ProposalError(
                f"提案 {pid} 当前状态 {rec['status']}，不能再挂影响面")
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as con:
            con.execute(
                "UPDATE ontology_proposal SET impact_json=?, impact_fprint=?, "
                "status='impact_ready', updated_at=? WHERE proposal_id=?",
                [json.dumps(impact, ensure_ascii=False), fingerprint, now, pid])
        return self.get(pid)  # type: ignore[return-value]

    def publish(self, pid: str, operator: str, fingerprint: str,
                ontology_version: str = "") -> dict:
        """impact_ready → published。🔴 唯一人工发布入口，前置条件服务端硬校验。"""
        rec = self.get(pid)
        if rec is None:
            raise ProposalError(f"提案不存在：{pid}")
        if rec["status"] == DRAFT:
            # E4-1：未看影响面直接发布 → 阻止（流程顺序：影响面在发布前）
            raise ProposalError("发布前必须先查看影响面分析（未生成影响面的提案不能发布）")
        if rec["status"] in _TERMINAL:
            raise ProposalError(f"提案已终态（{rec['status']}），不可重复发布")
        if rec["impact_fprint"] != fingerprint:
            # E4-3：影响面生成后下游已变化 → 要求重新评估
            raise ProposalError(
                "影响面基准已变化（规则/视图/函数/物化表/线索产物在提案后发生变动），"
                "请重新生成影响面后再发布")
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as con:
            con.execute(
                "UPDATE ontology_proposal SET status='published', "
                "published_at=?, published_by=?, ontology_version=?, "
                "updated_at=? WHERE proposal_id=?",
                [now, operator, ontology_version, now, pid])
        return self.get(pid)  # type: ignore[return-value]

    def discard(self, pid: str, operator: str) -> dict:
        rec = self.get(pid)
        if rec is None:
            raise ProposalError(f"提案不存在：{pid}")
        if rec["status"] in _TERMINAL:
            raise ProposalError(f"提案已终态（{rec['status']}），不能废弃")
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn() as con:
            con.execute(
                "UPDATE ontology_proposal SET status='discarded', "
                "discarded_by=?, updated_at=? WHERE proposal_id=?",
                [operator, now, pid])
        return self.get(pid)  # type: ignore[return-value]
