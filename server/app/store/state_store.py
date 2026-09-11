"""
server/app/store/state_store.py
D1 骨架（M2，决策 D-M2-2）：per-case state.sqlite 业务写承载层。

定位（M2_plan 附录 A）：
  - 业务面短频写（处置/人审裁决/审计链追加）由 per-case state.sqlite（WAL）
    承载，不随分析版本复制 GB 级 DuckDB（ADR 2.7 缺口修正）；
  - M3 接线方式：AuditChain 构造参数扩可选 backend（duckdb 缺省/sqlite），
    ActionExecutor 由 server 注入；本地 CLI/MCP 不传 backend 行为零变化；
  - **M2 不接任何写路径**：本模块只被 store/ 与 tests/ 引用（grep 门禁
    tests/test_state_store.py 硬断言），接线随 M3 处置端点同批。

schema 纪律：
  - audit_chain 与 DuckDB 版**逐列同构**（seq/event_id/.../signature），
    签名算法复用 core.audit._compute_signature——迁移后 chain_verify 逐条可验；
  - action_request / clue_disposal_status 与 core 同构；review_decision 为
    M3 人审裁决预留（接线时若需调整以 actions.json/处置端点为准）；
  - BUILD 任务不触碰 state.sqlite（决策 D1 原文：决策不随分析版本重建丢失）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from core.audit import _compute_signature

_GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_chain (
    seq               INTEGER NOT NULL,
    event_id          TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL,
    ontology_version  TEXT NOT NULL,
    rule_version      TEXT,
    function_version  TEXT,
    params_hash       TEXT,
    source_row_ids    TEXT,
    operator          TEXT NOT NULL,
    before_state      TEXT,
    after_state       TEXT,
    prev_hash         TEXT NOT NULL,
    signature         TEXT NOT NULL,
    occurred_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ac_seq ON audit_chain(seq);
CREATE TABLE IF NOT EXISTS action_request (
    action_id       TEXT PRIMARY KEY,
    idempotency_key TEXT,
    action_name     TEXT NOT NULL,
    clue_id         TEXT,
    target_status   TEXT,
    params_json     TEXT,
    status          TEXT NOT NULL,
    submitted_by    TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    approved_by     TEXT,
    approved_at     TEXT,
    dispatched_at   TEXT,
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    external_id     TEXT,
    writeback_status TEXT
);
CREATE TABLE IF NOT EXISTS clue_disposal_status (
    clue_id    TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    note       TEXT DEFAULT '',
    operator   TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS review_decision (
    decision_id TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT '',
    target_id   TEXT,
    verdict     TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    payload_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS quality_check (
    check_id     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL DEFAULT '',
    data_version INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    checks_json  TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS de_recommendation (
    rid                  TEXT PRIMARY KEY,
    upload_id            TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT '待核实',
    created_at           TEXT NOT NULL,
    created_by           TEXT NOT NULL DEFAULT '',
    decided_by           TEXT NOT NULL DEFAULT '',
    decided_at           TEXT NOT NULL DEFAULT '',
    note                 TEXT NOT NULL DEFAULT '',
    recommendations_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_de_reco_upload ON de_recommendation(upload_id);
CREATE TABLE IF NOT EXISTS etl_fix_draft (
    draft_id          TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL,
    upload_id         TEXT NOT NULL,
    target_object     TEXT NOT NULL,
    target_prop       TEXT NOT NULL,
    op_token          TEXT NOT NULL,
    op_class          TEXT NOT NULL,
    source            TEXT NOT NULL,
    preview_affected_rows INTEGER NOT NULL DEFAULT 0,
    preview_samples_json  TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT '待复核',
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL DEFAULT '',
    reviewed_by       TEXT NOT NULL DEFAULT '',
    reviewed_at       TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_etl_fix_upload ON etl_fix_draft(upload_id);
CREATE INDEX IF NOT EXISTS idx_etl_fix_status ON etl_fix_draft(case_id, status);
"""

_AC_COLUMNS = (
    "seq, event_id, case_id, ontology_version, rule_version, function_version, "
    "params_hash, source_row_ids, operator, before_state, after_state, "
    "prev_hash, signature, occurred_at")


def _as_duckdb_conn(source):
    """接受 CaseStore（.read_conn）/ 裸 duckdb 连接。"""
    conn = getattr(source, "read_conn", None)
    if conn is not None:
        return conn
    return getattr(source, "conn", source)


class StateStore:
    """per-case 业务状态库（WAL；M2 骨架：迁移演练 + 只读校验面）。"""

    def __init__(self, case_id: str, path: str | Path):
        self.case_id = case_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=5,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    @property
    def conn(self):
        """sqlite3 写连接（StateSink 暴露给 core ActionExecutor/AuditChain）。"""
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ---- 写面（M3 D1 接线；core 经 StateSink 窄协议调用）----
    def insert_decision(self, *, kind: str, target_id: str | None,
                        verdict: str, decided_by: str,
                        payload: dict) -> dict:
        """file 动作副作用：决策落 state.review_decision（不写版本文件语义表）。"""
        import json as _json
        import time as _time
        import uuid as _uuid
        decision_id = f"decision_{_uuid.uuid4().hex[:12]}"
        decided_at = _time.strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO review_decision "
            "(decision_id, kind, target_id, verdict, decided_by, decided_at, "
            " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [decision_id, kind, target_id, verdict, decided_by, decided_at,
             _json.dumps(payload, ensure_ascii=False, default=str)])
        self._conn.commit()
        return {"decision_id": decision_id, "persisted": True,
                "created_at": decided_at}

    def status_map(self) -> dict[str, dict]:
        """处置状态真值只读面（clue_id → {status,note,operator,updated_at}）。"""
        rows = self._conn.execute(
            "SELECT clue_id, status, note, operator, updated_at "
            "FROM clue_disposal_status").fetchall()
        return {r["clue_id"]: {"status": r["status"], "note": r["note"],
                               "operator": r["operator"],
                               "updated_at": r["updated_at"]}
                for r in rows}

    def list_decisions(self, target_id: str | None = None) -> list[dict]:
        """决策只读列表（审计/详情面用）。"""
        import json as _json
        if target_id is not None:
            rows = self._conn.execute(
                "SELECT decision_id, kind, target_id, verdict, decided_by, "
                "decided_at, payload_json FROM review_decision "
                "WHERE target_id=? ORDER BY decided_at",
                [target_id]).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT decision_id, kind, target_id, verdict, decided_by, "
                "decided_at, payload_json FROM review_decision "
                "ORDER BY decided_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = _json.loads(d.pop("payload_json") or "{}")
            except _json.JSONDecodeError:
                d["payload"] = {}
            out.append(d)
        return out

    # ---- W-P-008 质量检查（短频写；落 state 不产 DuckDB 版本）----
    def save_quality_check(self, *, check_id: str, created_at: str,
                           created_by: str, data_version: int,
                           summary: dict, checks: list) -> None:
        import json as _json
        self._conn.execute(
            "INSERT OR REPLACE INTO quality_check "
            "(check_id, created_at, created_by, data_version, summary_json, "
            " checks_json) VALUES (?, ?, ?, ?, ?, ?)",
            [check_id, created_at, created_by, int(data_version),
             _json.dumps(summary, ensure_ascii=False, default=str),
             _json.dumps(checks, ensure_ascii=False, default=str)])
        self._conn.commit()

    def latest_quality_check(self) -> dict | None:
        import json as _json
        row = self._conn.execute(
            "SELECT check_id, created_at, created_by, data_version, "
            "summary_json, checks_json FROM quality_check "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {
            "check_id": row["check_id"], "created_at": row["created_at"],
            "created_by": row["created_by"],
            "data_version": row["data_version"],
            "summary": _json.loads(row["summary_json"] or "{}"),
            "checks": _json.loads(row["checks_json"] or "[]"),
        }

    # ---- W-P-007 数据元智能推荐（待核实/采纳/驳回；永不自动生效）----
    def save_de_reco(self, *, rid: str, upload_id: str, created_at: str,
                     created_by: str, recommendations: list,
                     status: str = "待核实") -> None:
        import json as _json
        self._conn.execute(
            "INSERT OR REPLACE INTO de_recommendation "
            "(rid, upload_id, status, created_at, created_by, decided_by, "
            " decided_at, note, recommendations_json) "
            "VALUES (?, ?, ?, ?, ?, '', '', '', ?)",
            [rid, upload_id, status, created_at, created_by,
             _json.dumps(recommendations, ensure_ascii=False, default=str)])
        self._conn.commit()

    def get_de_reco(self, rid: str) -> dict | None:
        return self._de_reco_row(
            "SELECT * FROM de_recommendation WHERE rid=?", [rid])

    def find_de_reco_by_upload(self, upload_id: str) -> dict | None:
        return self._de_reco_row(
            "SELECT * FROM de_recommendation WHERE upload_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", [upload_id])

    def list_de_reco(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM de_recommendation ORDER BY created_at DESC, "
            "rowid DESC").fetchall()
        return [self._de_reco_dict(r) for r in rows]

    def decide_de_reco(self, rid: str, *, status: str, decided_by: str,
                       decided_at: str, note: str) -> dict | None:
        cur = self._conn.execute(
            "UPDATE de_recommendation SET status=?, decided_by=?, "
            "decided_at=?, note=? WHERE rid=?",
            [status, decided_by, decided_at, note, rid])
        self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_de_reco(rid)

    def _de_reco_row(self, sql: str, args: list) -> dict | None:
        row = self._conn.execute(sql, args).fetchone()
        return self._de_reco_dict(row) if row is not None else None

    @staticmethod
    def _de_reco_dict(row) -> dict:
        import json as _json
        d = dict(row)
        try:
            d["recommendations"] = _json.loads(
                d.pop("recommendations_json") or "[]")
        except _json.JSONDecodeError:
            d["recommendations"] = []
        return d

    # ---- v1.3 ETL 处置草稿（与 de_recommendation 分表；不自动生效）----
    # 生命周期：Step2 质检创建 → 待复核 → 已确认/已驳回 → 已发布
    # 落点：bindings.clean / bindings.source_sql（发布时写入，非此处）
    # 红线：status != 已确认 时 publish 拒绝（fail-closed）；B 类需 reviewed_by
    def save_etl_fix_draft(self, *, draft_id: str, case_id: str,
                          upload_id: str, target_object: str,
                          target_prop: str, op_token: str, op_class: str,
                          source: str = "Step2",
                          preview_affected_rows: int = 0,
                          preview_samples: list | None = None,
                          created_at: str, created_by: str,
                          note: str = "") -> None:
        """创建/更新 ETL 处置草稿（幂等：draft_id 存在则覆盖，status 重置为待复核）。"""
        import json as _json
        self._conn.execute(
            "INSERT OR REPLACE INTO etl_fix_draft "
            "(draft_id, case_id, upload_id, target_object, target_prop, "
            " op_token, op_class, source, preview_affected_rows, "
            " preview_samples_json, status, created_at, created_by, "
            " reviewed_by, reviewed_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '待复核', ?, ?, '', '', ?)",
            [draft_id, case_id, upload_id, target_object, target_prop,
             op_token, op_class, source, int(preview_affected_rows),
             _json.dumps(preview_samples or [], ensure_ascii=False,
                         default=str),
             created_at, created_by, note])
        self._conn.commit()

    def get_etl_fix_draft(self, draft_id: str) -> dict | None:
        return self._etl_fix_row(
            "SELECT * FROM etl_fix_draft WHERE draft_id=?", [draft_id])

    def list_etl_fix_drafts(self, *, case_id: str, upload_id: str | None = None,
                            status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM etl_fix_draft WHERE case_id=?"
        params: list = [case_id]
        if upload_id:
            sql += " AND upload_id=?"
            params.append(upload_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, rowid DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._etl_fix_dict(r) for r in rows]

    def confirm_etl_fix_draft(self, draft_id: str, *,
                              reviewed_by: str,
                              reviewed_at: str) -> dict | None:
        """复核通过：待复核 → 已确认（A 类 created_by 可自审；B 类需独立 reviewer）。"""
        cur = self._conn.execute(
            "UPDATE etl_fix_draft SET status='已确认', reviewed_by=?, "
            "reviewed_at=? WHERE draft_id=? AND status='待复核'",
            [reviewed_by, reviewed_at, draft_id])
        self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_etl_fix_draft(draft_id)

    def reject_etl_fix_draft(self, draft_id: str, *,
                             reviewed_by: str, reviewed_at: str,
                             note: str = "") -> dict | None:
        """复核驳回：待复核 → 已驳回（不可再发布）。"""
        cur = self._conn.execute(
            "UPDATE etl_fix_draft SET status='已驳回', reviewed_by=?, "
            "reviewed_at=?, note=? WHERE draft_id=? AND status='待复核'",
            [reviewed_by, reviewed_at, note, draft_id])
        self._conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_etl_fix_draft(draft_id)

    def publish_etl_fix_draft(self, draft_id: str, *,
                               reviewed_by: str = "",
                               reviewed_at: str = "") -> dict | None:
        """发布：已确认 → 已发布（fail-closed：其他状态拒绝）。

        发布只改 state 状态；真正写 bindings.clean/source_sql 由调用方
        在本方法返回后执行（state 不触 bindings，保持单写口红线）。
        A 类（op_class='A'）允许 reviewed_by 留空（免复核）；B/C 类必填。
        """
        row = self._conn.execute(
            "SELECT status, op_class FROM etl_fix_draft WHERE draft_id=?",
            [draft_id]).fetchone()
        if row is None:
            raise KeyError(f"草稿不存在：{draft_id}")
        if row["status"] != "已确认":
            raise ValueError(
                f"仅已确认草稿可发布，当前状态：{row['status']}")
        if row["op_class"] != "A" and not reviewed_by:
            raise ValueError(
                f"op_class={row['op_class']} 需 reviewed_by（B 类强制复核）")
        self._conn.execute(
            "UPDATE etl_fix_draft SET status='已发布', "
            "reviewed_by=COALESCE(NULLIF(reviewed_by,''), ?), "
            "reviewed_at=COALESCE(NULLIF(reviewed_at,''), ?) "
            "WHERE draft_id=?",
            [reviewed_by, reviewed_at, draft_id])
        self._conn.commit()
        return self.get_etl_fix_draft(draft_id)

    def _etl_fix_row(self, sql: str, args: list) -> dict | None:
        row = self._conn.execute(sql, args).fetchone()
        return self._etl_fix_dict(row) if row is not None else None

    @staticmethod
    def _etl_fix_dict(row) -> dict:
        import json as _json
        d = dict(row)
        try:
            d["preview_samples"] = _json.loads(
                d.pop("preview_samples_json") or "[]")
        except _json.JSONDecodeError:
            d["preview_samples"] = []
        return d

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 只读校验面（迁移断言用）----
    def event_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM audit_chain").fetchone()
        return int(row[0]) if row else 0

    def root_hash(self) -> str:
        """末条 signature 作为根哈希（与 DuckDB AuditChain.root_hash 同语义）。"""
        row = self._conn.execute(
            "SELECT signature FROM audit_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else _GENESIS_HASH

    def chain_verify(self) -> bool:
        """整链校验：prev_hash 衔接 + signature 重算（复用 core 签名算法）。"""
        rows = self._conn.execute(
            f"SELECT {_AC_COLUMNS} FROM audit_chain ORDER BY seq").fetchall()
        import json as _json
        prev = _GENESIS_HASH
        for r in rows:
            if r["prev_hash"] != prev:
                return False
            try:
                source_row_ids = (_json.loads(r["source_row_ids"])
                                  if r["source_row_ids"] else [])
            except _json.JSONDecodeError:
                source_row_ids = []
            before = _json.loads(r["before_state"]) if r["before_state"] else None
            after = _json.loads(r["after_state"]) if r["after_state"] else None
            recomputed = _compute_signature(
                r["event_id"], r["case_id"], r["ontology_version"],
                r["rule_version"], r["function_version"], r["params_hash"],
                source_row_ids, r["operator"], before, after, r["prev_hash"])
            if recomputed != r["signature"]:
                return False
            prev = r["signature"]
        return True

    # ---- 迁移演练（幂等；M3 上线后按案件一次性执行，附录 A）----
    def import_from_duckdb(self, source) -> dict:
        """读案件 DuckDB audit_chain 全量行 → 写 state.sqlite（幂等）。

        source：CaseStore（读模式）或裸 duckdb 连接。event_id 冲突
        INSERT OR REPLACE（重跑行数不变）；返回行数/根哈希比对结果。
        """
        conn = _as_duckdb_conn(source)
        try:
            rows = conn.execute(
                f"SELECT {_AC_COLUMNS} FROM audit_chain ORDER BY seq"
            ).fetchall()
        except Exception as e:
            raise RuntimeError(f"读取 DuckDB audit_chain 失败：{e}") from e
        # duckdb fetchall 返回原生元组：index 12 = signature
        duckdb_root = rows[-1][12] if rows else _GENESIS_HASH
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO audit_chain ({_AC_COLUMNS}) "
                    f"VALUES ({','.join('?' * 14)})", tuple(r))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return {
            "duckdb_count": len(rows),
            "state_count": self.event_count(),
            "duckdb_root_hash": duckdb_root,
            "state_root_hash": self.root_hash(),
            "counts_match": len(rows) == self.event_count(),
            "root_match": duckdb_root == self.root_hash(),
        }
