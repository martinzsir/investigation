"""
core/proposal.py
REQ-033 LLM 提案（Proposal）强类型校验与提案存储。

提案是 LLM 产出的"只读建议信封"，边界如下：
  - 可承载：规则草案（rule_draft）、参数草案（parameter_draft）、
    对齐复核（alignment_review）、解释（explanation）；
  - 七项硬校验（validate_proposal 返回错误列表，空=通过）：
    AC1 jsonschema 信封校验；
    AC2 candidate.function 必须在 load_pack(pack).functions 白名单；
    AC3 candidate.params 逐参数走 core.functions.check_param_value
        （类型/enum/数值正则，与规则装载期同一决策点）；
    AC4 candidate.evidence_row_uris 形如 obj_<type>/<pk> | lnk_<type>/<pk>，
        且 conn 给定时表/行存在（不可读硬失败）；
    AC5 rule_draft 的 candidate 含 writeback/action/dispatch 等写回字段 → 拒；
    AC6 explanation 的 candidate 含 status/to_status/transition 等状态变更字段
        或"置已立案"类指令值 → 拒；
    AC7 confidence 只允许出现在 _sort_hint，且不参与命中判定
        （confidence_only_sorts 辅助断言：不同 confidence 下命中集合不变）；
  - submit 须具名 author（拒 system/ai/llm 匿名）；
  - decide 状态机 draft→approved/rejected/expired，落 AuditChain；
  - 永不自动生效：approve 只表示"允许进入人工实施队列"，不触发任何写动作。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from core.functions import check_param_value
from core.ontology_loader import load_pack

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "proposal.schema.json"

KINDS = ("rule_draft", "parameter_draft", "alignment_review", "explanation")
STATUSES = ("draft", "approved", "rejected", "expired")

# AC5：rule_draft 禁止的写回类字段名（递归键名扫描，小写精确匹配）
_FORBIDDEN_WRITEBACK_KEYS = frozenset({
    "writeback", "write_back", "action", "actions", "dispatch",
    "auto_approve", "auto_apply", "apply_now", "execute",
})
# AC6：explanation 禁止的状态变更类字段名（递归键名扫描，小写子串匹配）
_FORBIDDEN_STATUS_KEYS = (
    "status", "to_status", "transition", "set_state", "state_change",
    "new_status",
)
# AC6：状态变更指令值（"置已立案/标记已核实"类祈使）
_STATUS_DIRECTIVE_RE = re.compile(
    r"(置|设为|标记为|改成|升级为|直接).{0,6}(已立案|已核实|已固证|已排除)"
    r"|跳过.{0,4}(复核|审核|review)")
# AC4：证据 URI 形态
_EVIDENCE_URI_RE = re.compile(r"^(obj|lnk)_([a-z_]+)/([A-Za-z0-9_\-]+)$")
# AC7：confidence 只允许出现在 _sort_hint
_CONFIDENCE_KEY = "confidence"

_ANON_AUTHORS = frozenset({"", "system", "ai", "llm", "assistant", "model"})


class ProposalValidationError(ValueError):
    """提案校验失败：errors 为全部违规项（七项硬校验一次性收集）。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("提案校验失败（{} 项）：\n  - {}".format(
            len(errors), "\n  - ".join(errors)))


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _iter_keys(obj: Any, path: str = "$"):
    """递归产出 (path, key, value)，dict/list 穿透；list 内标量按下标下发。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            yield p, k, v
            yield from _iter_keys(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            ip = f"{path}[{i}]"
            if isinstance(v, (dict, list)):
                yield from _iter_keys(v, ip)
            else:
                yield ip, i, v


def _check_evidence_uri(uri: str, conn, pack: str = "default") -> str | None:
    """AC4：URI 形态 + 表/行存在性；返回错误串或 None。

    obj 表主键列以 ontology 声明（ObjectType.pk，如 transaction→txn_id）为准；
    lnk 表无独立代理键，查声明端点列（<from_obj>_id/<to_obj>_id）与任意 *_id 列。
    """
    m = _EVIDENCE_URI_RE.match(str(uri))
    if not m:
        return f"evidence_row_uris 条目 {uri!r} 不合法（应为 obj_<类型>/<主键> 或 lnk_<类型>/<主键>）"
    kind_prefix, otype, pk = m.group(1), m.group(2), m.group(3)
    table = f"{kind_prefix}_{otype}"
    if conn is None:
        return None  # 无连接时只校验形态
    # 表存在
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table]).fetchone()[0]
    if not exists:
        return f"证据 URI {uri!r} 指向的语义表 {table} 不存在（不可读硬失败）"
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?", [table]).fetchall()]
    spec_pack = load_pack(pack)
    obj_map = {o.name: o for o in spec_pack.objects}
    link_map = {l.name: l for l in spec_pack.links}
    if kind_prefix == "obj":
        # 主键列以 ontology 类型声明为准（pk 不一定叫 <type>_id，如 txn_id）
        ospec = obj_map.get(otype)
        pk_col = getattr(ospec, "pk", None) or f"{otype}_id"
        if pk_col not in cols:
            return f"证据 URI {uri!r}：{table} 缺主键列 {pk_col}（语义层结构异常）"
        n = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{pk_col}" = ?', [pk]
        ).fetchone()[0]
    else:
        # 链接表：声明端点列 + 任意 *_id 列
        id_cols = {c for c in cols if c.endswith("_id")}
        lspec = link_map.get(otype)
        if lspec is not None:
            id_cols.update({f"{lspec.from_obj}_id", f"{lspec.to_obj}_id"})
        id_cols &= set(cols)
        n = 0
        for c in id_cols:
            n += conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{c}" = ?', [pk]
            ).fetchone()[0]
    if n == 0:
        return f"证据 URI {uri!r} 在 {table} 中无对应行（证据必须可溯源）"
    return None


def validate_proposal(p: dict, pack: str = "default", conn=None) -> list[str]:
    """七项硬校验；返回错误列表（空列表=通过）。"""
    errors: list[str] = []

    # ---- AC1：jsonschema 信封 ----
    validator = Draft7Validator(_load_schema())
    for err in validator.iter_errors(p):
        loc = "/".join(str(x) for x in err.absolute_path) or "<root>"
        errors.append(f"[AC1 信封] {loc}: {err.message}")
    if errors:
        return errors  # 信封不合法，后续字段校验无意义

    kind = p["kind"]
    cand = p.get("candidate") or {}

    # ---- AC2：函数白名单 ----
    func_name = cand.get("function")
    spec = None
    if func_name is not None:
        functions = load_pack(pack).functions
        if func_name not in functions:
            errors.append(
                f"[AC2 函数白名单] candidate.function={func_name!r} 不在 "
                f"functions.json 白名单 {sorted(functions)}（LLM 不得自创函数/写 SQL）")
        else:
            spec = functions[func_name]

    # ---- AC3：参数强类型校验（复用 check_param_value）----
    params = cand.get("params")
    if params is not None:
        if not isinstance(params, dict):
            errors.append("[AC3 参数] candidate.params 必须是对象")
        elif spec is None:
            errors.append("[AC3 参数] params 必须与白名单 function 一起声明（无函数挂钩的参数不接受）")
        else:
            spec_params = spec.parameters or {}
            for pname, pval in params.items():
                if pname not in spec_params:
                    errors.append(
                        f"[AC3 参数] 参数 {pname!r} 不在函数 {func_name!r} 声明的 "
                        f"parameters {sorted(spec_params)} 中")
                    continue
                try:
                    check_param_value(pname, spec_params[pname], pval,
                                      ctx=f"proposal candidate[{func_name}]")
                except ValueError as e:
                    errors.append(f"[AC3 参数] {e}")

    # ---- AC4：证据 URI ----
    uris = cand.get("evidence_row_uris") or []
    if not isinstance(uris, list):
        errors.append("[AC4 证据] evidence_row_uris 必须是数组")
    else:
        for uri in uris:
            err = _check_evidence_uri(uri, conn, pack)
            if err:
                errors.append(f"[AC4 证据] {err}")

    # ---- AC5：rule_draft 禁写回字段 ----
    if kind == "rule_draft":
        for path, key, _v in _iter_keys(cand, "$.candidate"):
            if str(key).lower() in _FORBIDDEN_WRITEBACK_KEYS:
                errors.append(
                    f"[AC5 禁写回] rule_draft 候选在 {path} 含写回/动作字段 "
                    f"{key!r}（提案只读，写操作唯一入口是 ActionExecutor）")

    # ---- AC6：explanation 禁状态变更 ----
    if kind == "explanation":
        for path, key, v in _iter_keys(cand, "$.candidate"):
            kl = str(key).lower()
            if kl in _FORBIDDEN_STATUS_KEYS or any(
                    s in kl for s in ("to_status", "transition")):
                errors.append(
                    f"[AC6 禁状态变更] explanation 候选在 {path} 含状态变更字段 "
                    f"{key!r}（解释提案不得驱动处置状态）")
            if isinstance(v, str) and _STATUS_DIRECTIVE_RE.search(v):
                errors.append(
                    f"[AC6 禁状态变更] explanation 候选在 {path} 含状态变更指令值 "
                    f"{v[:40]!r}（立案/固证是人工专属动作）")

    # ---- AC7：confidence 仅排序提示 ----
    for path, key, _v in _iter_keys(p, "$"):
        if str(key) == _CONFIDENCE_KEY:
            # path 已含键名（如 $.candidate._sort_hint.confidence），判其父节点
            parent = path.rsplit(".", 1)[0] if "." in path else path
            if not parent.endswith("._sort_hint"):
                errors.append(
                    f"[AC7 confidence] confidence 只允许出现在 _sort_hint（仅排序用），"
                    f"违规位置 {path}")

    return errors


def confidence_only_sorts(runs: list[tuple[float, Any]]) -> None:
    """AC7 辅助断言：不同 confidence 下命中集合必须一致（confidence 只许影响排序）。

    runs = [(confidence, hit_ids_set_or_list), ...]；不一致 → ProposalValidationError。
    """
    if len(runs) < 2:
        return
    baseline = frozenset(runs[0][1])
    for conf, hits in runs[1:]:
        if frozenset(hits) != baseline:
            raise ProposalValidationError(
                [f"[AC7 confidence] confidence={conf} 时命中集合 {sorted(hits)} "
                 f"与 baseline {sorted(baseline)} 不一致：confidence 不得参与命中判定"])


# ----------------------------------------------------------------------
# 提案存储
# ----------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS proposal (
    proposal_id     VARCHAR PRIMARY KEY,
    kind            VARCHAR NOT NULL,
    case_id         VARCHAR NOT NULL,
    status          VARCHAR NOT NULL DEFAULT 'draft',
    payload         VARCHAR NOT NULL,
    author          VARCHAR NOT NULL,
    created_at      TIMESTAMP NOT NULL,
    expires_at      TIMESTAMP,
    decided_by      VARCHAR,
    decided_at      TIMESTAMP,
    decision_reason VARCHAR,
    audit_event_id  VARCHAR
)
"""


class ProposalStore:
    """提案表：submit（校验+具名）→ get/list → decide（状态机+审计）。永不自动生效。"""

    def __init__(self, conn, pack: str = "default"):
        self.conn = conn
        self.pack = pack
        conn.execute(_DDL)

    def submit(self, proposal: dict, actor: str | None = None) -> str:
        """校验全过才入库；返回 proposal_id。author 必须具名（拒 system/ai）。

        actor（如 "agent:<id>"）为实际提交代理身份：提供时落 AuditChain
        （REQ-021-write AC3，agent 提交可审计）；None 时不落链（兼容既有调用）。
        """
        errors = validate_proposal(proposal, pack=self.pack, conn=self.conn)
        if errors:
            raise ProposalValidationError(errors)
        author = str(proposal.get("author", "")).strip()
        if author.lower() in _ANON_AUTHORS:
            raise ProposalValidationError(
                [f"author={author!r}：提案提交须具名自然人（system/ai/llm 不得作为作者）"])
        pid = proposal["proposal_id"]
        exists = self.conn.execute(
            "SELECT COUNT(*) FROM proposal WHERE proposal_id = ?", [pid]).fetchone()[0]
        if exists:
            raise ProposalValidationError([f"proposal_id={pid} 已存在（提案不可覆盖，恒新版本）"])
        expires = (proposal.get("constraints") or {}).get("expires_at")
        self.conn.execute(
            """INSERT INTO proposal
               (proposal_id, kind, case_id, status, payload, author, created_at, expires_at)
               VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)""",
            [pid, proposal["kind"], proposal["case_id"],
             json.dumps(proposal, ensure_ascii=False, default=str),
             author, _dt.datetime.now().isoformat(timespec="seconds"),
             expires])
        if actor:
            from core.audit import AuditChain
            chain = AuditChain(self.conn)
            event_id = chain.append(
                operator=author,
                before=None,
                after={"proposal_id": pid, "kind": proposal["kind"],
                       "status": "draft", "actor": str(actor),
                       "note": "提案提交：永不自动生效，须人审 decide"},
                source_row_ids=[pid],
                ontology_version=chain.current_ontology_version())
            self.conn.execute(
                "UPDATE proposal SET audit_event_id = ? WHERE proposal_id = ?",
                [event_id, pid])
        return pid

    def get(self, proposal_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT proposal_id, kind, case_id, status, payload, author, created_at, "
            "expires_at, decided_by, decided_at, decision_reason, audit_event_id "
            "FROM proposal WHERE proposal_id = ?", [proposal_id]).fetchone()
        if not row:
            return None
        d = dict(zip(["proposal_id", "kind", "case_id", "status", "payload", "author",
                      "created_at", "expires_at", "decided_by", "decided_at",
                      "decision_reason", "audit_event_id"], row))
        d["payload"] = json.loads(d["payload"])
        return d

    def list(self, kind: str | None = None, status: str | None = None) -> list[dict]:
        sql = ("SELECT proposal_id, kind, case_id, status, payload, author, created_at, "
               "expires_at, decided_by, decided_at, decision_reason, audit_event_id "
               "FROM proposal")
        where, args = [], []
        if kind:
            where.append("kind = ?"); args.append(kind)
        if status:
            where.append("status = ?"); args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at"
        cols = ["proposal_id", "kind", "case_id", "status", "payload", "author",
                "created_at", "expires_at", "decided_by", "decided_at",
                "decision_reason", "audit_event_id"]
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            d = dict(zip(cols, r))
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def refresh_expiry(self) -> int:
        """把过期 draft 置为 expired（惰性过期）；返回过期条数。"""
        now = _dt.datetime.now().isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT proposal_id FROM proposal WHERE status = 'draft' "
            "AND expires_at IS NOT NULL AND expires_at < ?", [now]).fetchall()
        for (pid,) in rows:
            self.conn.execute(
                "UPDATE proposal SET status = 'expired' WHERE proposal_id = ?", [pid])
        return len(rows)

    def decide(self, proposal_id: str, decision: str, operator: str,
               reason: str = "") -> dict:
        """审批：draft → approved/rejected（过期 draft 不可批）；落 AuditChain。

        decide 不触发任何写动作/状态迁移（提案永不自动生效）。
        """
        if decision not in ("approve", "reject"):
            raise ValueError(f"decision 仅允许 approve/reject，收到 {decision!r}")
        if str(operator).strip().lower() in _ANON_AUTHORS:
            raise PermissionError("审批须具名自然人（system/ai/llm 不得审批提案）")
        rec = self.get(proposal_id)
        if rec is None:
            raise KeyError(f"提案不存在：{proposal_id}")
        if rec["status"] != "draft":
            raise ProposalValidationError(
                [f"提案 {proposal_id} 当前状态 {rec['status']}，不可重复 decide"
                 f"（状态机：draft→approved/rejected/expired 单向）"])
        self.refresh_expiry()
        rec = self.get(proposal_id)
        new_status = "approved" if decision == "approve" else "rejected"
        if rec["status"] == "expired":
            raise ProposalValidationError(
                [f"提案 {proposal_id} 已过 expires_at，禁止审批（过期只能重新提交）"])
        # 审计链
        from core.audit import AuditChain
        chain = AuditChain(self.conn)
        event_id = chain.append(
            operator=operator,
            before={"proposal_id": proposal_id, "status": "draft"},
            after={"proposal_id": proposal_id, "status": new_status,
                   "decision": decision, "reason": reason},
            source_row_ids=[proposal_id],
            ontology_version=chain.current_ontology_version())
        self.conn.execute(
            """UPDATE proposal SET status = ?, decided_by = ?, decided_at = ?,
                   decision_reason = ?, audit_event_id = ?
               WHERE proposal_id = ?""",
            [new_status, operator,
             _dt.datetime.now().isoformat(timespec="seconds"),
             reason, event_id, proposal_id])
        return self.get(proposal_id)
