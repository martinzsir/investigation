"""
core/llm/guard.py
REQ-039 提示注入防护。

四道防线（全部确定性、可单测）：
  1. 分框：wrap_untrusted 把任何外部/模型产出内容包成 untrusted_content 帧
     （source + sha256），数据与指令在结构上分离；
  2. 扫描：INJECTION_PATTERNS 命中"忽略指令/标记已核实/跳过复核/诱导 SQL/
     角色扮演/隐匿痕迹"等注入特征；high 命中 → 整帧隔离（quarantine，
     不进入模型上下文），med 命中 → 保留但告警；
  3. 候选白名单：sanitize_candidate 按 proposal kind 只收声明字段，
     status/writeback/action/override 等字段一律丢弃（与 core.proposal
     的 AC5/AC6 双保险）；assert_no_status_change 再拦一道；
  4. 原始证据可取回：raw_evidence_fragment 供人工复核直接读 obj_/lnk_ 行
     （过 access 对象级策略），复核不只依赖模型概括。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from core.ontology_loader import load_pack

# ----------------------------------------------------------------------
# 注入特征库（pattern_id → severity + 正则）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class InjectionPattern:
    pattern_id: str
    severity: str            # "high" | "med"
    regex: re.Pattern
    description: str


INJECTION_PATTERNS: list[InjectionPattern] = [
    InjectionPattern(
        "ignore_previous", "high",
        re.compile(
            r"(忽略|无视|忘掉|forget)[^。；\n]{0,12}(之前|以上|前面|先前|所有|全部|上述)"
            r"[^。；\n]{0,10}(指令|指示|规则|要求|约束|prompt)"
            r"|ignore\s+(all\s+)?(previous|prior|above|earlier)\b[^.\n]{0,40}"
            r"(instruction|prompt|rule)",
            re.IGNORECASE),
        "忽略/推翻既有指令与规则"),
    InjectionPattern(
        "override_verdict", "high",
        re.compile(
            r"(标记|标为|置|设为|改成|升级为|直接定|直接)[^。；\n]{0,10}"
            r"(已核实|已立案|已固证|已确认|审核通过|verified|approved)",
            re.IGNORECASE),
        "指令模型直接给出核实/立案结论"),
    InjectionPattern(
        "skip_review", "high",
        re.compile(
            r"(跳过|免除|免去|无需|不需要|不用|不要)[^。；\n]{0,10}"
            r"(人工)?(复核|审核|审查|审批|review)"
            r"|skip\s+(the\s+)?(human\s+)?review",
            re.IGNORECASE),
        "要求跳过人工复核/审批"),
    InjectionPattern(
        "sql_injection", "high",
        re.compile(
            r"\b(DROP|ALTER|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b"
            r"|\b(DELETE\s+FROM|INSERT\s+INTO)\b"
            r"|\bUPDATE\s+[`\"\[]?[A-Za-z_][\w]*[`\"\]]?\s+SET\b",
            re.IGNORECASE),
        "诱导执行写库/DDL SQL（自由 SQL 无通道）"),
    InjectionPattern(
        "role_play", "high",
        re.compile(
            r"你(现在)?(就)?是[^。；\n]{0,14}(系统管理员|管理员|系统|开发者|超级用户|root)"
            r"|you\s+are\s+(now\s+)?(a|an|the)?\s*(system\s+|root\s+)?"
            r"(administrator|admin|root|developer|superuser)",
            re.IGNORECASE),
        "角色扮演提权（冒充系统管理员/root）"),
    InjectionPattern(
        "stealth", "med",
        re.compile(
            r"(不要|别|勿|无需)[^。；\n]{0,12}(告诉|通知|记录|写入|日志|操作员)"
            r"|don'?t\s+(tell|log|record|notify)|never\s+(tell|log|record)",
            re.IGNORECASE),
        "要求隐匿痕迹/瞒报操作员（med：告警保留）"),
    InjectionPattern(
        "reveal_prompt", "med",
        re.compile(
            r"(输出|打印|透露|泄露|给出)[^。；\n]{0,12}(系统提示|系统指令|提示词|系统消息)"
            r"|(?:reveal|show|print|leak)[^.\n]{0,20}(?:system\s+prompt|prompt)",
            re.IGNORECASE),
        "套取系统提示词（med：告警保留）"),
]


@dataclass(frozen=True)
class InjectionHit:
    pattern_id: str
    severity: str
    start: int
    end: int
    excerpt: str
    description: str = ""


def scan_text(text: str) -> list[InjectionHit]:
    """扫描单段文本，返回全部命中（high/med）。"""
    hits: list[InjectionHit] = []
    s = str(text)
    for p in INJECTION_PATTERNS:
        for m in p.regex.finditer(s):
            hits.append(InjectionHit(
                pattern_id=p.pattern_id, severity=p.severity,
                start=m.start(), end=m.end(),
                excerpt=s[max(0, m.start() - 8):m.end() + 8].replace("\n", " "),
                description=p.description))
    return hits


# ----------------------------------------------------------------------
# 分框（数据/指令分离）
# ----------------------------------------------------------------------
def wrap_untrusted(text: str, source: str) -> dict:
    """把外部/模型产出内容包成不可信帧（带 source 与 sha256，防篡改可追溯）。"""
    t = str(text)
    return {
        "_frame": "untrusted_content",
        "source": str(source),
        "text": t,
        "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest(),
    }


def scan_bundle(frames: list[dict]) -> dict:
    """扫描分框内容：high 命中整帧隔离（不进模型上下文），med 命中保留告警。

    返回 {clean, safe_frames, quarantined, warnings}。
    """
    safe, quarantined, warnings = [], [], []
    for fr in frames:
        text = fr.get("text", "") if isinstance(fr, dict) else str(fr)
        hits = scan_text(text)
        high = [h for h in hits if h.severity == "high"]
        med = [h for h in hits if h.severity == "med"]
        if high:
            quarantined.append({
                "source": fr.get("source") if isinstance(fr, dict) else "inline",
                "sha256": fr.get("sha256") if isinstance(fr, dict) else None,
                "hits": [h.__dict__ for h in high],
            })
        else:
            safe.append(fr)
        for h in med:
            warnings.append({
                "source": fr.get("source") if isinstance(fr, dict) else "inline",
                "hit": h.__dict__,
            })
    return {"clean": not quarantined, "safe_frames": safe,
            "quarantined": quarantined, "warnings": warnings}


# ----------------------------------------------------------------------
# 候选白名单（与 core.proposal AC5/AC6 双保险）
# ----------------------------------------------------------------------
_CANDIDATE_FIELDS: dict[str, set[str]] = {
    "rule_draft": {"rule_text", "function", "params", "dimension",
                   "jian_types", "evidence_row_uris"},
    "parameter_draft": {"parameter", "value", "evidence", "evidence_row_uris"},
    "alignment_review": {"merge_risk", "support", "conflict",
                         "question_for_operator"},
    "explanation": {"sentences", "evidence_map"},
}

# 任何 kind 都不允许出现在候选里的字段（状态变更/写回/提权）
_FORBIDDEN_CANDIDATE_KEYS = {
    "status", "to_status", "transition", "set_state", "state_change",
    "new_status", "writeback", "write_back", "action", "actions", "dispatch",
    "auto_approve", "auto_apply", "apply_now", "execute", "override",
    "needs_human_review", "approved",
}


def sanitize_candidate(raw: dict, kind: str) -> tuple[dict, list[str]]:
    """按 kind 白名单收字段；非白名单字段一律丢弃并记录（不抛错，由调用方审计 dropped）。

    _sort_hint 为唯一通用透传键（confidence 仅排序，proposal AC7 校验其位置）。
    """
    if kind not in _CANDIDATE_FIELDS:
        raise ValueError(f"未知 proposal kind {kind!r}，可用 {sorted(_CANDIDATE_FIELDS)}")
    allowed = _CANDIDATE_FIELDS[kind]
    clean, dropped = {}, []
    for k, v in (raw or {}).items():
        if k in allowed or k == "_sort_hint":
            clean[k] = v
        else:
            dropped.append(k)
    return clean, dropped


def assert_no_status_change(candidate: dict, kind: str) -> None:
    """guard 层再拦一道：候选递归出现写回/状态变更字段或指令值 → PermissionError。"""
    from core.proposal import (
        _FORBIDDEN_WRITEBACK_KEYS,
        _FORBIDDEN_STATUS_KEYS,
        _STATUS_DIRECTIVE_RE,
        _iter_keys,
    )
    violations: list[str] = []
    for path, key, v in _iter_keys(candidate or {}, "$.candidate"):
        kl = str(key).lower()
        if kl in _FORBIDDEN_WRITEBACK_KEYS or kl in _FORBIDDEN_CANDIDATE_KEYS \
                or kl in _FORBIDDEN_STATUS_KEYS \
                or any(s in kl for s in ("to_status", "transition")):
            violations.append(f"{path} 含禁字段 {key!r}")
        if isinstance(v, str) and _STATUS_DIRECTIVE_RE.search(v):
            violations.append(f"{path} 含状态变更指令值 {v[:40]!r}")
    if violations:
        raise PermissionError(
            f"候选含状态变更/写回内容（REQ-039，kind={kind}），已拦截：\n  - "
            + "\n  - ".join(violations))


# ----------------------------------------------------------------------
# 原始证据片段取回（人工复核通道，过 access 策略；不得进入模型上下文）
# ----------------------------------------------------------------------
def raw_evidence_fragment(conn, uri: str, access=None, pack: str = "default",
                          limit: int = 5) -> dict:
    """按证据 URI 取回原始语义层行（只读），供人工复核对照模型概括。

    - obj_<type>/<pk>：按 ontology 声明主键取 1 行；
    - lnk_<type>/<pk>：按端点/任意 *_id 列匹配取至多 limit 行；
    - 过 PolicyEngine 对象级/链接级策略（非 system 上下文无权即拒）。
    """
    from core.access import system_context
    from core.policy import PolicyEngine

    m = re.compile(r"^(obj|lnk)_([a-z_]+)/([A-Za-z0-9_\-]+)$").match(str(uri))
    if not m:
        raise ValueError(f"证据 URI 不合法：{uri!r}（应为 obj_<类型>/<主键> 或 lnk_<类型>/<主键>）")
    kind_prefix, otype, pk = m.group(1), m.group(2), m.group(3)
    table = f"{kind_prefix}_{otype}"

    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table]).fetchone()[0]
    if not exists:
        raise KeyError(f"证据 URI {uri!r}：语义表 {table} 不存在")

    ctx = access if access is not None else system_context()
    if not bool(ctx.is_system):
        engine = PolicyEngine(pack)
        if kind_prefix == "obj":
            engine.check_object(ctx, otype)
        else:
            engine.check_link(ctx, otype)

    spec_pack = load_pack(pack)
    obj_map = {o.name: o for o in spec_pack.objects}
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?", [table]).fetchall()]

    if kind_prefix == "obj":
        ospec = obj_map.get(otype)
        pk_col = getattr(ospec, "pk", None) or f"{otype}_id"
        if pk_col not in cols:
            raise KeyError(f"{table} 缺主键列 {pk_col}（语义层结构异常）")
        rows = conn.execute(
            f'SELECT * FROM "{table}" WHERE "{pk_col}" = ? LIMIT 1', [pk]
        ).fetchall()
    else:
        id_cols = [c for c in cols if c.endswith("_id")]
        if not id_cols:
            raise KeyError(f"{table} 无 *_id 端点列，无法按 URI 定位")
        where = " OR ".join(f'"{c}" = ?' for c in id_cols)
        rows = conn.execute(
            f'SELECT * FROM "{table}" WHERE {where} LIMIT ?',
            [pk] * len(id_cols) + [limit]).fetchall()

    if not rows:
        raise KeyError(f"证据 URI {uri!r} 在 {table} 中无对应行")

    def _rowdict(r):
        return {c: (str(v) if hasattr(v, "isoformat") else v)
                for c, v in zip(cols, r)}

    return {
        "uri": uri,
        "table": table,
        "columns": cols,
        "rows": [_rowdict(r) for r in rows],
        "note": "原始证据片段仅供人工复核，不得进入模型上下文（REQ-039 AC5）",
    }
