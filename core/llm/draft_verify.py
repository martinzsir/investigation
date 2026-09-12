"""
core/llm/draft_verify.py
REQ-V-019 LLM 核查方向草案（shadow → 提案 → 人审；复刻 REQ-034 draft_rule 轨道）。

边界（ADR-V-7 双闸 + ADR-V-8 三档）：
  - LLM 草案永不进 L1 供给：L1 playbook 纯离线确定性渲染、永远兜底；
  - 草案只落提案队列（ProposalStore.submit status=draft），**人审批通过**才经
    REQ-V-014 桥接生成 TASK_VERIFY（operator=审批人，origin='ai_draft'）；
  - 能力闸门前置：llm_enabled=false 或会话/策略交集无可用部署档 → degraded
    信号（零模型调用、落 llm_call_log allowed=false），L1 与手动构造器不受影响；
  - 端点闸（endpoint_guard）：选定档后先验 base_url 边界，不过不发起请求；
  - 脱敏分档：cloud/strict——人名 tokenize（含自由文本内的人名，token map
    仅存请求内存）、轨迹/正文整段丢弃、PII 遮蔽；local/relaxed——人名/单位
    可明文（免 rehydrate），证件/手机号/银行卡仍遮蔽、轨迹/正文仍 drop；
    两档 llm_call_log 都只有 prompt_hash（prompt 正文不留存）；
  - 幻觉护栏全部确定性：channel 枚举、按渠道槽位白名单（与 playbook 装载
    校验同构）、function/fallback_function 名必须存在于案件包 functions.json
    白名单（SQL/py 实现的单一事实源；未知即丢弃该候选、记 dropped，不生成
    自由 SQL）、assert_no_status_change（禁止状态变更指令值）；
  - 幂等：同 clue 同 content_sha1 草案不重复提交（in-batch 与跨请求两级）；
  - shadow 断言：调用前后 table_snapshot + shadow_diff——除
    proposal/llm_call_log/audit_chain 外任何表行数变化即抛错。

purpose 留痕说明：ADR-V-8 要求 cloud 档 llm_call_log 记 purpose；既有
log_llm_call 无 purpose 列（不改表结构），purpose 记在提案
input.purpose 与提案审计事件，经 llm_result.log_id 可关联。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

from core.access import AccessContext
from core.llm.endpoint_guard import EndpointBlocked, assert_endpoint_allowed
from core.llm.fallback import LLMDegraded, ensure_llm_capability, shadow_diff, table_snapshot
from core.llm.guard import assert_no_status_change
from core.llm.llm_client import LLMClient
from core.llm.redact import (
    _DROP_CONTENT_KEYS,
    _DROP_TRACK_KEYS,
    _NAME_KEYS,
    load_llm_policy,
    log_llm_call,
    redact_payload,
    scan_pii,
    tokenize_name,
)
from core.ontology_loader import load_pack
from core.proposal import ProposalStore, validate_proposal, ProposalValidationError

#: 庙算维度全集（与 draft_rule 提示词同源）
DIMENSIONS = ("资金", "通讯", "轨迹", "交叉", "招投", "举报", "开源")

#: 候选渠道枚举（function=库内可复跑 / external=外部调取 / manual=纯人工方向）
CHANNELS = ("function", "external", "manual")

#: 按渠道槽位白名单（与 playbook 装载校验同构；越槽候选整体丢弃）
_SLOT_BY_CHANNEL: dict[str, frozenset[str]] = {
    "function": frozenset({"text", "dimension", "channel", "function",
                           "fallback_function", "falsification"}),
    "external": frozenset({"text", "dimension", "channel", "external",
                           "falsification"}),
    "manual": frozenset({"text", "dimension", "channel", "falsification"}),
}


# ----------------------------------------------------------------------
# 部署档选择（会话面 ∩ 策略面，fail-closed）
# ----------------------------------------------------------------------
def select_deployment(ctx: AccessContext,
                      policy: dict) -> tuple[str, dict | None, str | None]:
    """按 ctx.network 与 policy.deployments 交集选档。

    返回 (mode, dep_cfg, off_reason)：mode ∈ {"local","cloud","off"}；
    off 时 dep_cfg=None 且给 reason。fail-closed：无 deployments 段 /
    段非法 / enabled 非 true / 会话网络不匹配 → off。
    """
    deps = policy.get("deployments")
    if not isinstance(deps, dict):
        return "off", None, "策略未声明 deployments 段（fail-closed：两档均不可用）"
    net = getattr(ctx, "network", "isolated")
    if net == "local":
        dep = deps.get("local")
        if isinstance(dep, dict) and dep.get("enabled") is True:
            return "local", dep, None
        return "off", None, "会话 network=local 但策略未启用 local 部署档"
    if net == "web":
        dep = deps.get("cloud")
        if isinstance(dep, dict) and dep.get("enabled") is True:
            return "cloud", dep, None
        return "off", None, "会话 network=web 但策略未启用 cloud 部署档"
    return "off", None, f"会话 network={net!r}：isolated 全拒（内核纯离线）"


def _deployment_policy(policy: dict, dep: dict) -> dict:
    """部署档生效策略：模型白名单换档 + relaxed 档人名免 tokenize。"""
    eff = dict(policy)
    eff["allowed_models"] = list(dep.get("allowed_models") or [])
    if str(dep.get("redaction", "strict")) == "relaxed":
        pr = dict(policy.get("pii_redaction") or {})
        pr["name"] = "keep"  # 未知动作 → redact 层不 tokenize（PII 数字仍遮蔽）
        eff["pii_redaction"] = pr
    return eff


def _check_clearance(ctx: AccessContext, dep: dict) -> str | None:
    """cloud 档操作员 clearance 校验（require_clearance=true 时生效）。"""
    if dep.get("require_clearance") is not True:
        return None
    try:
        need = int(dep.get("min_clearance", 2))
    except (TypeError, ValueError):
        need = 2
    have = getattr(ctx, "clearance", 0) or 0
    if have < need:
        return (f"cloud 档要求 clearance≥{need}，当前操作员 clearance={have}"
                "（ADR-V-8 授权面）")
    return None


# ----------------------------------------------------------------------
# 脱敏辅助：token map 构建 / 自由文本人名替换 / rehydrate
# ----------------------------------------------------------------------
def _walk_strings(obj: Any):
    """递归产出 (path_key, str_value)；dict/list 穿透。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                yield str(k), v
            else:
                yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, str):
                yield "", v
            else:
                yield from _walk_strings(v)


def _is_name_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _NAME_KEYS)


def _is_sensitive_key(key: str) -> bool:
    """精确轨迹点/通话正文类键（两档都整段 drop，部署模式不豁免）。"""
    k = key.lower()
    return (any(s in k for s in _DROP_TRACK_KEYS)
            or any(s in k for s in _DROP_CONTENT_KEYS))


def build_token_map(context: Any) -> dict[str, str]:
    """从原始上下文收集人名 → {token: 真名}（仅请求内存，不入库）。"""
    names: set[str] = set()
    for key, v in _walk_strings(context):
        if key and _is_name_key(key) and v.strip():
            names.add(v.strip())
    return {tokenize_name(n): n for n in sorted(names)}


def _replace_all(text: str, mapping: dict[str, str]) -> str:
    out = text
    for src, dst in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(src, dst)
    return out


def _tokenize_free_names(redacted: Any, names: set[str],
                         token_map: dict[str, str]) -> Any:
    """strict 档自由文本兜底：结构化 name 键已被 redact_payload tokenize，
    标题/判据/建议文本里内嵌的人名按已知名单整体替换（AC6：出网 payload
    不含真实人名）。"""
    if not names:
        return redacted
    name_to_token = {n: t for t, n in token_map.items()}

    def _walk(o):
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_walk(v) for v in o]
        if isinstance(o, str):
            return _replace_all(o, name_to_token)
        return o

    return _walk(redacted)


def rehydrate(text: str, token_map: dict[str, str]) -> str:
    """模型产出的 token 文本 → 真实主体（同请求内服务端完成；token map
    不入库、不跨会话）。"""
    return _replace_all(text, token_map) if token_map else text


# ----------------------------------------------------------------------
# 模型产出守门（全部确定性）
# ----------------------------------------------------------------------
def _clean_candidate(raw: Any, func_names: set[str],
                     index: int) -> tuple[dict | None, str | None]:
    """单候选守门：返回 (clean, drop_reason)；clean=None 表示丢弃。"""
    if not isinstance(raw, dict):
        return None, f"#{index} 候选不是对象"
    slots = _SLOT_BY_CHANNEL.get(str(raw.get("channel") or ""))
    if slots is None:
        return None, (f"#{index} 非法 channel={raw.get('channel')!r}"
                      f"（合法：{'/'.join(CHANNELS)}）")
    unknown = sorted(set(raw) - slots)
    if unknown:
        return None, f"#{index} 含白名单外槽位 {unknown}（channel={raw.get('channel')}）"
    text = str(raw.get("text") or "").strip()
    if not text:
        return None, f"#{index} text 为空"
    cand: dict[str, Any] = {"text": text,
                            "channel": str(raw["channel"])}
    if isinstance(raw.get("dimension"), str) and raw["dimension"].strip():
        cand["dimension"] = raw["dimension"].strip()
    if cand["channel"] == "function":
        fn = str(raw.get("function") or "").strip()
        if not fn:
            return None, f"#{index} channel=function 缺 function 名"
        if fn not in func_names:
            return None, (f"#{index} function={fn!r} 不在 functions.json 白名单"
                          "（LLM 不得自创函数/写 SQL）")
        cand["function"] = fn
        fb = str(raw.get("fallback_function") or "").strip()
        if fb:
            if fb not in func_names:
                return None, f"#{index} fallback_function={fb!r} 不在 functions.json 白名单"
            cand["fallback_function"] = fb
    if cand["channel"] == "external":
        ext = raw.get("external")
        if ext is not None:
            if not isinstance(ext, dict):
                return None, f"#{index} external 必须是对象 {{target, material}}"
            target = str(ext.get("target") or "").strip()
            material = str(ext.get("material") or "").strip()
            if not target or not material:
                return None, f"#{index} external.target/material 不能为空"
            cand["external"] = {"target": target, "material": material}
    if isinstance(raw.get("falsification"), str) and raw["falsification"].strip():
        cand["falsification"] = raw["falsification"].strip()
    try:
        assert_no_status_change(cand, "verify_item")
    except PermissionError as e:
        return None, f"#{index} 含状态变更/写回内容，已拦截：{e}"
    return cand, None


def _content_sha1(clue_id: str, cand: dict) -> str:
    blob = json.dumps({"clue_id": clue_id, "cand": cand},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _dimension_gap(context: dict, func_names_meta: dict[str, str]) -> list[str]:
    """覆盖缺口（确定性粗粒度）：已有 function 渠道核查项命中的维度之外。"""
    covered: set[str] = set()
    for it in context.get("已有核查项") or []:
        fn = (it or {}).get("ref_function") if isinstance(it, dict) else None
        dim = func_names_meta.get(str(fn or ""))
        if dim:
            covered.add(dim)
    return [d for d in DIMENSIONS if d not in covered]


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def draft_verify_items(
    conn,
    ctx: AccessContext,
    clue_id: str,
    *,
    case_id: str,
    context: dict,
    pack: str = "default",
    base_dir=None,
    llm_client: LLMClient | None = None,
    policy: dict | None = None,
    model: str | None = None,
    resolve: Callable[[str], list[str]] | None = None,
) -> dict[str, Any]:
    """REQ-V-019：核查方向草案生成（同步；人显式点击触发，不自动发起）。

    Args:
        conn: 提案/llm_call_log/审计链所在 DuckDB 连接（Web 全局提案库）。
        ctx: 会话 AccessContext（network=local|web 决定可用部署档）。
        clue_id / case_id: 线索与案件（提案 case_id 路由用）。
        context: 聚合案件上下文（调用方组装；本函数负责脱敏）。
        pack / base_dir: 案件包（函数白名单、llm_policy 装载）。
        llm_client: 注入客户端（测试 fake）；缺省 LLMClient()。
        model: 指定模型；缺省取部署档 allowed_models 首个。
        resolve: 端点闸 DNS 解析注入（测试假解析）。

    Returns:
        off：{ok:False, degraded:True, mode:"off", reason, proposals:[]}
        端点闸/调用失败：{ok:False, degraded:False, blocked:bool, error, ...}
        成功：{ok:True, mode, model, proposals:[{proposal_id, text, dimension,
              channel, ref_function, author, content_sha1}], dropped, duplicates}
    """
    from core.llm.redact import call_llm

    policy = policy if policy is not None else load_llm_policy(pack, base_dir)
    snapshot_before = table_snapshot(conn)

    def _fail(**kw: Any) -> dict[str, Any]:
        return {"proposals": [], "dropped": [], "duplicates": 0, **kw}

    # ① 能力总闸（llm_enabled=false：零模型调用 + llm_call_log(allowed=false)）
    try:
        ensure_llm_capability(conn, ctx, policy, model=model,
                              source="draft_verify")
    except LLMDegraded as e:
        return _fail(ok=False, degraded=True, mode="off", reason=str(e),
                     model=None, log_id=None)

    # ② 会话 ∩ 策略选档（off：落 llm_call_log(allowed=false)，零模型调用）
    mode, dep, off_reason = select_deployment(ctx, policy)
    if mode == "off":
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=off_reason)
        return _fail(ok=False, degraded=True, mode="off", reason=off_reason,
                     model=None, log_id=log_id)

    # ③ cloud 档 clearance 校验（fail-closed）
    clear_err = _check_clearance(ctx, dep)
    if clear_err:
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=clear_err)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=clear_err, model=model, log_id=log_id)

    spec = load_pack(pack, base_dir)
    func_names = set(spec.functions)
    func_meta = {name: (fspec.description or fspec.title or "").split("：")[0]
                 for name, fspec in spec.functions.items()}

    # ④ 端点闸（不通过不发起请求；llm_call_log 记 blocked）
    model = model or (dep.get("allowed_models") or [None])[0]
    if not model:
        blocked = "部署档未声明 allowed_models（fail-closed：无模型可用）"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=None,
            allowed=False, blocked_reason=blocked)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=blocked, model=None, log_id=log_id)
    if model not in (dep.get("allowed_models") or []):
        blocked = f"model {model!r} 不在部署档 allowed_models {dep.get('allowed_models')}"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=blocked)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=blocked, model=model, log_id=log_id)
    try:
        assert_endpoint_allowed(mode, dep.get("base_url") or "",
                                dep.get("endpoints"), resolve=resolve)
    except EndpointBlocked as e:
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=f"endpoint_gate: {e}")
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=str(e), model=model, log_id=log_id)

    # ⑤ 脱敏（分档）+ redaction_hash（call_llm 闸门验真用）
    eff_policy = _deployment_policy(policy, dep)
    strict = str(dep.get("redaction", "strict")) != "relaxed"
    token_map: dict[str, str] = {}
    if strict:
        token_map = build_token_map(context)
        redacted = redact_payload(context, eff_policy)
        redacted = _tokenize_free_names(
            redacted, set(token_map.values()), token_map)
    else:
        redacted = redact_payload(context, eff_policy)
    blob = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    redacted["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ⑥ prompt + 调用（走 call_llm 闸门；prompt 只留 hash）
    gap = _dimension_gap(context, func_meta)
    system_prompt = (
        "你是侦查核查方向分析师。基于脱敏后的聚合案件上下文，提出核查手册"
        "（L1 playbook）覆盖不到的核查方向草案。\n"
        "严格约束：\n"
        f"1. channel 只能是 {'/'.join(CHANNELS)}；channel=function 时 function"
        " 必须从以下白名单选择，不得自创函数或写 SQL：\n"
        f"   {json.dumps(sorted(func_names), ensure_ascii=False)}\n"
        "2. 输出 JSON：{\"items\": [{text, dimension, channel, function?, "
        "fallback_function?, external?, falsification?}]}\n"
        "3. text 是一条可裁决的核查项描述；dimension 从 "
        f"{list(DIMENSIONS)} 中选\n"
        "4. 不得包含任何状态变更指令（立案/核实/固证）或写操作\n"
        "以 ```json``` 代码块输出。"
    )
    user_prompt = json.dumps({
        "案件上下文（脱敏）": redacted,
        "维度覆盖缺口（供参考）": gap,
        "输出要求": {"items": "最多 5 条，按价值排序"},
    }, ensure_ascii=False, indent=2)

    client = llm_client or LLMClient()

    def _invoke(model, prompt, redacted_input):  # pragma: no cover - 生产路径
        resp = client.chat_json(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=1024)
        # 对齐 draft_rule：ok 时剥壳返回 {content, raw, parsed}；
        # 失败原样透传（上层按解析失败处理）
        if resp.get("ok"):
            return resp["result"]
        return resp

    try:
        llm_result = call_llm(
            conn, ctx, eff_policy, model=model, prompt=user_prompt,
            redacted_input=redacted, fake_invoke=_invoke)
    except Exception as e:  # LLMBlockedError / 网络 / 解析异常
        return _fail(ok=False, degraded=False, blocked=False, mode=mode,
                     error=str(e), model=model, log_id=None)

    raw_result = llm_result["result"]
    parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return _fail(ok=False, degraded=False, blocked=False, mode=mode,
                     error="LLM 输出无法解析为 {\"items\": [...]} JSON",
                     model=model, log_id=llm_result.get("log_id"))

    # ⑦ 候选守门 + rehydrate + 幂等去重
    store = ProposalStore(conn, pack=pack)
    existing_sha: set[str] = set()
    for rec in store.list(kind="verify_item"):
        sha = ((rec.get("payload") or {}).get("input") or {}).get("content_sha1")
        if rec.get("status") in ("draft", "approved") and sha:
            existing_sha.add(sha)

    proposals: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for i, raw in enumerate(items):
        cand, reason = _clean_candidate(raw, func_names, i)
        if cand is None:
            dropped.append({"index": i, "reason": reason})
            continue
        if strict and token_map:
            for key in ("text", "falsification"):
                if key in cand:
                    cand[key] = rehydrate(cand[key], token_map)
            if "external" in cand:
                cand["external"] = {
                    k: rehydrate(v, token_map)
                    for k, v in cand["external"].items()}
        csha = _content_sha1(clue_id, cand)
        if csha in seen or csha in existing_sha:
            duplicates += 1
            continue
        seen.add(csha)
        proposal = {
            "proposal_id": f"pp-{uuid.uuid4().hex[:12]}",
            "kind": "verify_item",
            "case_id": case_id,
            "author": f"model:{model}",
            "candidate": {"text": cand["text"], "clue_id": clue_id},
            "constraints": {},
            "input": {
                "origin": "ai_draft",
                "purpose": "draft_verify",
                "mode": mode,
                "model": model,
                "channel": cand["channel"],
                "ref_function": cand.get("function") or "",
                "falsification": cand.get("falsification") or "",
                "external": cand.get("external"),
                "dimension": cand.get("dimension") or "",
                "dimension_gap": gap,
                "content_sha1": csha,
                "prompt_sha1": hashlib.sha1(
                    user_prompt.encode("utf-8")).hexdigest(),
                "llm_log_id": llm_result.get("log_id"),
            },
            "_sort_hint": {"content_sha1": csha, "model": model,
                           "channel": cand["channel"]},
        }
        try:
            errors = validate_proposal(proposal, pack=pack, conn=conn)
            if errors:
                dropped.append({"index": i,
                                "reason": f"提案校验失败：{errors}"})
                continue
            pid = store.submit(proposal)
        except ProposalValidationError as e:
            dropped.append({"index": i, "reason": str(e)})
            continue
        proposals.append({
            "proposal_id": pid,
            "text": cand["text"],
            "dimension": cand.get("dimension") or "",
            "channel": cand["channel"],
            "ref_function": cand.get("function") or "",
            "author": proposal["author"],
            "content_sha1": csha,
        })

    # ⑧ shadow 断言：除 proposal/llm_call_log/audit_chain 外零变化
    diff = shadow_diff(snapshot_before, table_snapshot(conn))
    if diff:  # pragma: no cover - 结构性保证，防御性断言
        raise RuntimeError(f"REQ-V-019 shadow 隔离被破坏：{diff}")

    return _fail(ok=True, degraded=False, blocked=False, mode=mode,
                 model=model, log_id=llm_result.get("log_id"),
                 proposals=proposals, dropped=dropped, duplicates=duplicates)
