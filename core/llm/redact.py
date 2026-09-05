"""
core/llm/redact.py
REQ-038 LLM 治理基座：策略装载（fail-closed）、PII 脱敏、脱敏上下文构造、
LLM 调用闸门与审计日志。

红线（与 AGENTS.md 三条禁令一致）：
  - 内核纯离线：network=isolated 时一切 LLM 调用被拒（core.access.require_llm_allowed）；
  - allowed_models 为空 = 无模型可用，全拒（白名单第二道闸门）；
  - 出网文本必须脱敏：身份证/手机号/银行卡正则遮蔽（310****1234 风格）、
    精确轨迹与通话/正文类字段整段丢弃（[REDACTED:...]）、人名字段 tokenize
    （当事人#<hash6>，同名同 token 保留关联分析能力）；
  - call_llm 闸门处对"已脱敏输入"复扫 PII，零命中才放行（双保险）；
  - 每次调用尝试（允许/拒绝）落 llm_call_log 表 + AuditChain 哈希链，可审计；
  - prompt 不留存（只记 prompt_hash）、原始上下文永不入库（retention.raw_context=never_store）。

生产环境不内置任何模型：fake_invoke 是唯一模型出口（测试注入），
未注入时调用被拒并走 fallback=deterministic_only（仅确定性计算）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import uuid
from typing import Any, Callable

from core.access import AccessContext, LLMBlockedError, require_llm_allowed
from core.ontology_loader import PACK_ROOT

# ----------------------------------------------------------------------
# PII 正则（与 tests/test_golden.py 夹具脱敏扫描同款，单一形态定义）
# 顺序敏感：身份证 18 位先于银行卡 16~19 位（身份证形态也会被银行卡正则匹配），
# 遮蔽后星号打断数字串，后续正则不会重复命中。
# ----------------------------------------------------------------------
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 身份证带数字边界：避免在 19 位银行卡号内部误匹配 18 位子串
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
]

# 整段丢弃的结构化敏感字段（键名小写子串匹配；策略动作 drop 时生效）
_DROP_TRACK_KEYS = ("轨迹", "track", "地点", "location", "address", "地址")
_DROP_CONTENT_KEYS = ("通话内容", "call_content", "内容", "content",
                      "备注", "remark", "note", "说明", "description")
# 人名 tokenize 字段（键名小写子串匹配）
_NAME_KEYS = ("name", "姓名", "主体", "caller", "callee", "owner",
              "target", "reporter", "legal_rep", "法人", "关联人", "分管领导")

_FAIL_CLOSED_POLICY: dict[str, Any] = {
    "schema_version": 2,
    "network": "isolated",
    "allowed_models": [],
    "pii_redaction": {
        "id_card": "redact",
        "phone": "redact",
        "bank_card": "redact",
        "precise_track": "drop",
        "call_content": "drop",
        "name": "tokenize",
    },
    "retention": {"prompt_days": 0, "raw_context": "never_store"},
    "fallback": "deterministic_only",
    "_source": "fail_closed_default",
}


# ----------------------------------------------------------------------
# 策略装载（fail-closed）
# ----------------------------------------------------------------------
def load_llm_policy(pack: str = "default", base_dir=None) -> dict[str, Any]:
    """装载 ontology/<pack>/llm_policy.json。

    fail-closed（AC5）：文件缺失、JSON 非法、缺 network 声明或 allowed_models
    非列表 → 返回内置最严默认策略（network=isolated、allowed_models=[]）。
    """
    root = (base_dir or PACK_ROOT) / pack
    path = root / "llm_policy.json"
    if not path.exists():
        return json.loads(json.dumps(_FAIL_CLOSED_POLICY))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_FAIL_CLOSED_POLICY))
    if not isinstance(data, dict) or not data.get("network") \
            or not isinstance(data.get("allowed_models"), list):
        return json.loads(json.dumps(_FAIL_CLOSED_POLICY))
    # 补齐脱敏动作缺省（缺省按最严：PII 遮蔽、轨迹/正文丢弃、人名 tokenize）
    red = dict(_FAIL_CLOSED_POLICY["pii_redaction"])
    red.update(data.get("pii_redaction") or {})
    data["pii_redaction"] = red
    data.setdefault("retention", _FAIL_CLOSED_POLICY["retention"])
    data.setdefault("fallback", "deterministic_only")
    data["_source"] = str(path)
    return data


# ----------------------------------------------------------------------
# 文本脱敏
# ----------------------------------------------------------------------
def _mask_match(m: re.Match) -> str:
    """310****1234 风格：保留前 3 后 4，中间星号。"""
    s = m.group(0)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def redact_text(text: str, policy: dict | None = None) -> tuple[str, dict]:
    """遮蔽自由文本中的身份证/手机号/银行卡形态。

    返回 (redacted_text, report)；report = {counts: {type: n}, redaction_hash, clean}。
    策略中对应动作非 "redact" 时跳过该类型（fail-safe：未知动作仍遮蔽）。
    """
    policy = policy or _FAIL_CLOSED_POLICY
    red_cfg = policy.get("pii_redaction") or {}
    counts: dict[str, int] = {}
    out = str(text)
    for label, pat in _PII_PATTERNS:
        action = red_cfg.get(label, "redact")
        if action != "redact":
            continue

        def _sub(m, _label=label):
            counts[_label] = counts.get(_label, 0) + 1
            return _mask_match(m)

        out = pat.sub(_sub, out)
    h = hashlib.sha256(out.encode("utf-8")).hexdigest()
    return out, {"counts": counts, "redaction_hash": h, "clean": not counts}


def scan_pii(text: str) -> dict[str, int]:
    """PII 复扫：返回非零计数 {type: n}（闸门双保险用，不受策略动作影响）。"""
    counts: dict[str, int] = {}
    s = str(text)
    for label, pat in _PII_PATTERNS:
        n = len(pat.findall(s))
        if n:
            counts[label] = n
    return counts


def tokenize_name(name: str) -> str:
    """人名 → 当事人#<sha1[:6]>；同名恒同 token（保留跨记录关联，不暴露真名）。"""
    h = hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:6]
    return f"当事人#{h}"


def _key_action(key: str, policy: dict) -> str | None:
    """键名 → 脱敏动作：'drop:precise_track' | 'drop:call_content' | 'tokenize_name' | None。"""
    k = str(key).lower()
    red = policy.get("pii_redaction") or {}
    if red.get("precise_track", "drop") == "drop" \
            and any(s in k for s in _DROP_TRACK_KEYS):
        return "drop:precise_track"
    if red.get("call_content", "drop") == "drop" \
            and any(s in k for s in _DROP_CONTENT_KEYS):
        return "drop:call_content"
    if red.get("name", "tokenize") == "tokenize" \
            and any(s in k for s in _NAME_KEYS):
        return "tokenize_name"
    return None


def _tokenize_value(v: Any) -> Any:
    if isinstance(v, str):
        return tokenize_name(v)
    if isinstance(v, list):
        return [tokenize_name(x) if isinstance(x, str) else x for x in v]
    return v


def redact_payload(obj: Any, policy: dict | None = None, _depth: int = 0) -> Any:
    """递归脱敏任意 dict/list 结构：

    - 轨迹/地点类键 → 整值替换 "[REDACTED:precise_track]"；
    - 通话正文/内容/备注类键 → 整值替换 "[REDACTED:call_content]"；
    - 人名类键 → tokenize（当事人#hash6）；
    - 其余字符串值走 redact_text 遮蔽 PII 数字形态；数值/布尔等原样返回。
    """
    policy = policy or _FAIL_CLOSED_POLICY
    if _depth > 20:
        return "[REDACTED:nested_too_deep]"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            act = _key_action(k, policy)
            if act and act.startswith("drop:"):
                out[k] = f"[REDACTED:{act.split(':', 1)[1]}]"
            elif act == "tokenize_name":
                out[k] = _tokenize_value(v) if not isinstance(v, (dict, list)) \
                    else redact_payload(v, policy, _depth + 1)
            else:
                out[k] = redact_payload(v, policy, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact_payload(v, policy, _depth + 1) for v in obj]
    if isinstance(obj, str):
        text, _ = redact_text(obj, policy)
        return text
    return obj


# ----------------------------------------------------------------------
# 脱敏上下文构造（给 LLM 的唯一输入形态）
# ----------------------------------------------------------------------
def _evidence_uris(rows: list[Any]) -> list[str]:
    """从结果行中尽力提取语义层证据 URI（obj_<type>/<pk>、lnk_<type>/<pk>）。

    仅收形如 ^[a-z]+_\\d{4}$ 的代理键（person_0001/transaction_0001 等），
    无法构造 URI 的行不计入（原始行文本不出网）。
    """
    pk_re = re.compile(r"^[a-z]+_\d{4,}$")
    uris: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            if isinstance(k, str) and k.endswith("_id") and isinstance(v, str) \
                    and pk_re.match(v):
                t = k[:-3]
                prefix = "lnk_" if t.endswith("_link") or t in {
                    "time_window", "co_located", "transfers", "owns",
                    "involved_in", "decision_for"} else "obj_"
                uris.add(f"{prefix}{t}/{v}")
    return sorted(uris)


def build_redacted_context(findings: list[dict],
                           policy: dict | None = None) -> dict[str, Any]:
    """把规则 findings 压缩为可出网的脱敏上下文（AC3）。

    只含：rule_id / 级别 / dimension / jian_types / source_row 计数 /
    字段名清单 / evidence URI / 降级标记；不含任何原始行文本与明细值。
    返回体自带 redaction_hash 与 pii_rescan 复扫结果（闸门验真用）。
    """
    policy = policy or _FAIL_CLOSED_POLICY
    items: list[dict[str, Any]] = []
    for f in findings:
        rows = f.get("source_rows") or []
        fields = sorted({k for r in rows if isinstance(r, dict) for k in r})
        items.append({
            "rule_id": f.get("rule_id"),
            "级别": f.get("级别"),
            "dimension": f.get("dimension"),
            "jian_types": list(f.get("jian_types") or []),
            "source_row_count": len(rows),
            "source_row_fields": fields,
            "evidence_uris": _evidence_uris(rows),
            "is_degraded": bool(f.get("is_degraded")),
        })
    ctx: dict[str, Any] = {
        "schema": "sunzi.redacted_context/v1",
        "note": "仅含规则ID/级别/维度/间类/行数计数/字段名/证据URI；原始明细不出网（REQ-038 AC3）",
        "finding_count": len(items),
        "findings": items,
    }
    blob = json.dumps(ctx, ensure_ascii=False, sort_keys=True, default=str)
    counts = scan_pii(blob)
    ctx["pii_rescan"] = {"clean": not counts, "counts": counts}
    ctx["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return ctx


# ----------------------------------------------------------------------
# 调用日志（llm_call_log）+ 审计链
# ----------------------------------------------------------------------
_LOG_DDL = """
CREATE TABLE IF NOT EXISTS llm_call_log (
    log_id                VARCHAR PRIMARY KEY,
    occurred_at           TIMESTAMP NOT NULL,
    operator              VARCHAR   NOT NULL,
    network               VARCHAR   NOT NULL,
    model                 VARCHAR,
    allowed               BOOLEAN   NOT NULL,
    prompt_hash           VARCHAR,
    input_redaction_hash  VARCHAR,
    tool_calls            VARCHAR,
    blocked_reason        VARCHAR,
    audit_event_id        VARCHAR
)
"""


def ensure_llm_call_log(conn) -> None:
    """幂等建表（表名不带 obj_/lnk_ 前缀，编译器不 DROP）。"""
    conn.execute(_LOG_DDL)


def log_llm_call(conn, *, operator: str, network: str, model: str | None,
                 allowed: bool, prompt_hash: str | None = None,
                 input_redaction_hash: str | None = None,
                 tool_calls: list | None = None,
                 blocked_reason: str | None = None) -> str:
    """记录一次 LLM 调用尝试（允许/拒绝都记）；同步落 AuditChain，返回 log_id。"""
    ensure_llm_call_log(conn)
    log_id = "llmlog_" + uuid.uuid4().hex
    now = _dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO llm_call_log
           (log_id, occurred_at, operator, network, model, allowed,
            prompt_hash, input_redaction_hash, tool_calls, blocked_reason, audit_event_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        [log_id, now, operator, network, model, allowed,
         prompt_hash, input_redaction_hash,
         json.dumps(tool_calls or [], ensure_ascii=False), blocked_reason])
    # 审计链（REQ-007）：before/after 快照不含 prompt 原文（retention: 不留存）
    from core.audit import AuditChain
    chain = AuditChain(conn)
    event_id = chain.append(
        operator=operator,
        before=None,
        after={"action": "llm_call", "model": model, "allowed": allowed,
               "blocked_reason": blocked_reason,
               "input_redaction_hash": input_redaction_hash},
        source_row_ids=[log_id],
        ontology_version=chain.current_ontology_version())
    conn.execute("UPDATE llm_call_log SET audit_event_id = ? WHERE log_id = ?",
                 [event_id, log_id])
    return log_id


# ----------------------------------------------------------------------
# 调用闸门（LLM 唯一出口）
# ----------------------------------------------------------------------
def call_llm(conn, ctx: AccessContext, policy: dict | None = None, *,
             model: str, prompt: str, redacted_input: dict[str, Any],
             fake_invoke: Callable | None = None) -> dict[str, Any]:
    """LLM 调用唯一闸门。

    闸门序列：① require_llm_allowed（isolated 拒）；② model ∈ allowed_models；
    ③ redacted_input 必须带 redaction_hash 且 PII 复扫零命中；
    ④ 落 llm_call_log + AuditChain（允许/拒绝都落）；
    ⑤ fake_invoke 注入点——生产无模型即拒（fallback=deterministic_only）。

    拒绝时落日志后抛 LLMBlockedError；放行返回 {ok, model, log_id, result}。
    """
    policy = policy or load_llm_policy()
    blocked: str | None = None

    # ① 网络闸门
    try:
        require_llm_allowed(ctx)
    except LLMBlockedError as e:
        blocked = str(e)

    # ② 模型白名单
    if blocked is None and model not in (policy.get("allowed_models") or []):
        blocked = (f"model {model!r} 不在 allowed_models 白名单"
                   f" {policy.get('allowed_models')}（REQ-038 模型白名单）")

    # ③ 脱敏双保险：必须有 redaction_hash，且复扫零命中
    input_hash = None
    if blocked is None:
        if not isinstance(redacted_input, dict):
            blocked = "redacted_input 必须是 build_redacted_context/redact_payload 产物（dict）"
        else:
            input_hash = redacted_input.get("redaction_hash")
            if not input_hash:
                blocked = "redacted_input 缺 redaction_hash：未走脱敏通道的输入禁止出网（REQ-038 AC2）"
            else:
                # 复扫只扫内容载荷，排除派生摘要字段（哈希十六进制串可能偶然形成手机号形态）
                scan_obj = {k: v for k, v in redacted_input.items()
                            if k not in ("redaction_hash", "pii_rescan")}
                blob = json.dumps(scan_obj, ensure_ascii=False,
                                  sort_keys=True, default=str)
                counts = scan_pii(blob)
                if counts:
                    blocked = f"脱敏复扫仍检出 PII {counts}：禁止出网（REQ-038 AC2）"

    # ⑤ 前置检查：生产无模型（fake_invoke 未注入）→ 确定性回退
    if blocked is None and fake_invoke is None:
        blocked = ("无可用模型：内核不内置模型且未注入 fake_invoke；"
                   f"fallback={policy.get('fallback', 'deterministic_only')}，"
                   "LLM 不可用时仅允许确定性计算")

    # ④ 日志（无论允许/拒绝）
    prompt_hash = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest() \
        if prompt else None
    log_id = log_llm_call(
        conn, operator=ctx.operator, network=ctx.network, model=model,
        allowed=blocked is None, prompt_hash=prompt_hash,
        input_redaction_hash=input_hash, tool_calls=[],
        blocked_reason=blocked)

    if blocked:
        raise LLMBlockedError(blocked)

    result = fake_invoke(model=model, prompt=prompt,
                         redacted_input=redacted_input)
    return {"ok": True, "model": model, "log_id": log_id, "result": result}
