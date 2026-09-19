"""
core/llm/draft_image.py
P8 多模态图像研判草案通道（复刻 core.llm.draft_verify 双闸轨道）。

边界（模型直入生产=0）：
  - VLM 调用只产 image_draft 提案（ProposalStore.submit status=draft），
    永不写语义层/永不进 finding；正兵比对原件、执行 verify_image Action
    人验通过，才由 ActionExecutor 副作用创建 obj_image_evidence +
    lnk_image_for_person/org/bid_project；
  - 镜头元数据（enabled/external_services/timeout_ms/result_ttl_s）单一
    事实源是 packs/vlm/pack.json，经 pack_loader 注册进 SkillRegistry；
  - 出网前确定性闸门序列：
    ① 总开关 ensure_llm_capability；② 会话∩策略选档 select_deployment；
    ③ 部署档 allowed_vision_models 非空（与文本 allowed_models 物理隔离）；
    ④ clearance；⑤ 端点闸 endpoint_guard；
    ⑥ image_guard.assert_sendable（face/id_document block、未声明类别拒）；
    ⑦ image_guard.strip_metadata 纯字节剥 EXIF + has_exif 复核；
    ⑧ 文本面 PII 复扫（instruction 含手机号/身份证即拒）；
    ⑨ call_llm 闸门 model_field=allowed_vision_models；
  - 降级有痕：总闸关闭/镜头停用/选档 off/视觉白名单空 → degraded，
    零模型调用、落 llm_call_log(allowed=false)；断网/超时/解析失败 →
    ok=false（error 留痕），确定性结果不受影响；
  - TTL：提案 input.created_epoch + spec.result_ttl_s；list_image_drafts
    派生 stale=true（stale 草案不得人验升格，须重新分析）；
  - shadow 断言：除 proposal/llm_call_log/audit_chain 外表行数零变化。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any, Callable

from core.access import AccessContext
from core.llm.draft_verify import (
    _check_clearance,
    _deployment_policy,
    select_deployment,
)
from core.llm.endpoint_guard import EndpointBlocked, assert_endpoint_allowed
from core.llm.fallback import LLMDegraded, ensure_llm_capability, shadow_diff, table_snapshot
from core.llm.guard import assert_no_status_change
from core.llm.image_guard import ImageBlocked, assert_sendable, has_exif, strip_metadata
from core.llm.llm_client import LLMClient
from core.llm.redact import (
    load_llm_policy,
    log_llm_call,
    scan_pii,
)
from core.proposal import ProposalStore, ProposalValidationError, validate_proposal

#: 镜头 ID（与 packs/vlm/pack.json 对齐）
LENS_ID = "vlm_inspect"

#: prompt 版本（单一事实源；模型/prompt 版本独立落字段）
DEFAULT_PROMPT_VERSION = "vlm-invoice-v1"

#: 合法 severity 与数值范围
_SEVERITIES = frozenset({"info", "warn"})


# ----------------------------------------------------------------------
# TTL / stale
# ----------------------------------------------------------------------
def is_draft_stale(payload: dict[str, Any], now_epoch: float | None = None) -> bool:
    """按 input.created_epoch + input.ttl_s 派生 stale；缺字段 fail-closed=True。"""
    inp = (payload or {}).get("input") if isinstance(payload, dict) else None
    if not isinstance(inp, dict):
        return True
    created = inp.get("created_epoch")
    ttl = inp.get("ttl_s")
    if not isinstance(created, (int, float)) or not isinstance(ttl, int) or ttl <= 0:
        return True
    now = time.time() if now_epoch is None else now_epoch
    return (now - float(created)) > float(ttl)


def list_image_drafts(conn, *, case_id: str | None = None,
                      now_epoch: float | None = None,
                      status: str | None = None) -> list[dict[str, Any]]:
    """列 image_draft 提案并派生 stale 标记（不物理改状态）。"""
    store = ProposalStore(conn)
    out: list[dict[str, Any]] = []
    for rec in store.list(kind="image_draft", status=status):
        if case_id is not None and rec.get("case_id") != case_id:
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        rec["stale"] = is_draft_stale(payload, now_epoch)
        out.append(rec)
    return out


# ----------------------------------------------------------------------
# 模型产出守门
# ----------------------------------------------------------------------
def _clean_finding(raw: Any, index: int) -> tuple[dict | None, float | None, str | None]:
    """单条发现守门 → (candidate, score, drop_reason)。"""
    if not isinstance(raw, dict):
        return None, None, f"#{index} finding 不是对象"
    title = str(raw.get("title") or "").strip()
    detail = str(raw.get("detail") or "").strip()
    if not title:
        return None, None, f"#{index} title 为空"
    if not detail:
        return None, None, f"#{index} detail 为空"
    severity = str(raw.get("severity") or "info").strip()
    if severity not in _SEVERITIES:
        return None, None, f"#{index} severity={severity!r} 非法（{sorted(_SEVERITIES)}）"
    cand = {"title": title, "detail": detail, "severity": severity}
    try:  # 禁状态变更/写回
        assert_no_status_change(cand, "image_draft")
    except PermissionError as e:
        return None, None, f"#{index} 含状态变更/写回内容，已拦截：{e}"
    score: float | None = None
    if raw.get("score") is not None:
        try:
            score = float(raw["score"])
        except (TypeError, ValueError):
            return None, None, f"#{index} score 非数值"
        if not 0.0 <= score <= 1.0:
            return None, None, f"#{index} score={score} 超出 [0,1]"
    return cand, score, None


def _mime_of(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    raise ImageBlocked("剥离后图像格式不再可识别（fail-closed）")


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def draft_image_inspect(
    conn,
    ctx: AccessContext,
    *,
    case_id: str,
    image_uri: str,
    content_class: str,
    instruction: str = "",
    prompt_version: str | None = None,
    subject_type: str = "",
    subject_id: str = "",
    pack: str = "default",
    base_dir=None,
    llm_client: LLMClient | None = None,
    image_loader: Callable[[str], bytes] | None = None,
    policy: dict | None = None,
    model: str | None = None,
    resolve: Callable[[str], list[str]] | None = None,
) -> dict[str, Any]:
    """P8：VLM 图像分析草案（同步；人显式点击触发，不自动发起）。

    Returns:
      能力不可用：{ok:False, degraded:True, mode:"off", reason, proposals:[]}
      闸门拒绝：{ok:False, blocked:True, mode, error, ...}
      调用/解析失败：{ok:False, error, ...}（确定性结果不受影响）
      成功：{ok:True, mode, model, proposals:[{proposal_id,title,detail,
            severity,model_score,stale}], dropped}
    """
    from core.llm.redact import call_llm
    from core.registry import get_registry

    policy = policy if policy is not None else load_llm_policy(pack, base_dir)
    content_class = str(content_class or "").strip()
    prompt_version = (str(prompt_version).strip()
                      if prompt_version else DEFAULT_PROMPT_VERSION)
    snapshot_before = table_snapshot(conn)

    def _fail(**kw: Any) -> dict[str, Any]:
        return {"proposals": [], "dropped": [], **kw}

    # ⓪ 镜头存在 + 单镜头开关（pack.json 为元数据单一事实源）
    try:
        spec = get_registry().skill(LENS_ID)
    except KeyError:
        reason = f"镜头 {LENS_ID} 未挂载（packs/vlm 包未装载）"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=None,
            allowed=False, blocked_reason=reason)
        return _fail(ok=False, degraded=True, mode="off", reason=reason,
                     model=None, log_id=log_id)
    if not spec.enabled:
        reason = f"镜头 {LENS_ID} 已停用（pack.json enabled=false）"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=None,
            allowed=False, blocked_reason=reason)
        return _fail(ok=False, degraded=True, mode="off", reason=reason,
                     model=None, log_id=log_id)

    # ① 能力总闸（llm_enabled=false：零模型调用）
    try:
        ensure_llm_capability(conn, ctx, policy, model=model,
                              source="draft_image")
    except LLMDegraded as e:
        return _fail(ok=False, degraded=True, mode="off", reason=str(e),
                     model=None, log_id=None)

    # ② 会话 ∩ 策略选档
    mode, dep, off_reason = select_deployment(ctx, policy)
    if mode == "off":
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=off_reason)
        return _fail(ok=False, degraded=True, mode="off", reason=off_reason,
                     model=None, log_id=log_id)

    # ③ 部署档视觉模型白名单（与文本物理隔离；空=视觉能力未启用 → degraded）
    vision_models = list((dep or {}).get("allowed_vision_models") or [])
    model = model or (vision_models or [None])[0]
    if not vision_models or not model:
        reason = (f"{mode} 部署档未声明 allowed_vision_models"
                  "（视觉能力未启用，fail-closed）")
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=None,
            allowed=False, blocked_reason=reason)
        return _fail(ok=False, degraded=True, mode=mode, reason=reason,
                     model=None, log_id=log_id)
    if model not in vision_models:
        blocked = f"model {model!r} 不在 allowed_vision_models {vision_models}"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=blocked)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=blocked, model=model, log_id=log_id)

    # ④ clearance
    clear_err = _check_clearance(ctx, dep)
    if clear_err:
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=clear_err)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=clear_err, model=model, log_id=log_id)

    # ⑤ 端点闸
    try:
        assert_endpoint_allowed(mode, dep.get("base_url") or "",
                                dep.get("endpoints"), resolve=resolve)
    except EndpointBlocked as e:
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=f"endpoint_gate: {e}")
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=str(e), model=model, log_id=log_id)

    # ⑥ 读取原图（loader 必须注入；不猜测路径）
    if image_loader is None:
        blocked = "未提供 image_loader（fail-closed：不自行定位文件）"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=blocked)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=blocked, model=model, log_id=log_id)
    try:
        raw_bytes = bytes(image_loader(image_uri))
    except Exception as e:  # 文件缺失/不可读：有痕，不出网
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=f"image_load: {e}")
        return _fail(ok=False, degraded=False, blocked=False, mode=mode,
                     error=f"图像读取失败：{e}", model=model, log_id=log_id)

    # ⑦ 敏感区域 block（face/id_document/未声明类别 fail-closed）
    try:
        assert_sendable({"content_class": content_class},
                        policy.get("image_pii"))
    except ImageBlocked as e:
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=f"image_gate: {e}")
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=str(e), model=model, log_id=log_id)

    # ⑧ 纯字节剥离 EXIF + 复核
    try:
        stripped = strip_metadata(raw_bytes)
        mime = _mime_of(stripped)
        if has_exif(stripped):
            raise ImageBlocked("EXIF 剥离复核仍检出元数据块（fail-closed）")
    except ImageBlocked as e:
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=f"exif_gate: {e}")
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=str(e), model=model, log_id=log_id)

    # ⑨ 文本面组装 + PII 复扫（instruction 无 PII 才放行）
    text_input = {
        "instruction": str(instruction or "").strip(),
        "content_class": content_class,
        "prompt_version": prompt_version,
    }
    pii_counts = scan_pii(json.dumps(text_input, ensure_ascii=False))
    if pii_counts:
        blocked = f"分析要求文本检出 PII {pii_counts}：禁止出网（请改写）"
        log_id = log_llm_call(
            conn, operator=ctx.operator, network=ctx.network, model=model,
            allowed=False, blocked_reason=blocked)
        return _fail(ok=False, degraded=False, blocked=True, mode=mode,
                     error=blocked, model=model, log_id=log_id)
    blob = json.dumps(text_input, ensure_ascii=False, sort_keys=True)
    text_input["redaction_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ⑩ prompt + 视觉调用（图像字节走闭包，不进 redacted_input）
    system_prompt = (
        "你是侦查图像分析助手。分析用户提供的票据/文书图像，输出其中可见的"
        "结构化事实线索（只陈述图像中可见内容，不推测、不下定性结论）。\n"
        "严格约束：\n"
        "1. 输出 JSON：{\"findings\": [{title, detail, severity, score}], "
        "\"summary\"}\n"
        "2. title=短标题；detail=图像中可见的事实；severity ∈ "
        f"{sorted(_SEVERITIES)}；score∈[0,1]（把握度，仅供排序）\n"
        "3. 不得输出任何状态变更指令（立案/固证/核实）或写操作建议\n"
        "以 ```json``` 代码块输出。"
    )
    prompt_text = json.dumps(
        {"分析要求": text_input["instruction"] or "提取图像中的全部可见事实线索",
         "图像类别": content_class}, ensure_ascii=False)

    # 必须把闸门白名单选出的视觉模型显式传入：LLMClient() 默认模型是
    # 文本模型 qwen-plus，收图像部件不报错但“看不见图”，会回 findings 恒空的
    # 合法 JSON（曾导致线上草案队列永远为 0，且回包 model 标签还是外层贴的
    # qwen-vl-max，极具迷惑性）
    client = llm_client or LLMClient(model=model)

    def _invoke(model, prompt, redacted_input):
        data_url = f"data:{mime};base64,{base64.b64encode(stripped).decode()}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt_text},
            ]},
        ]
        resp = client.chat_json(messages, temperature=0.1, max_tokens=2048)
        if resp.get("ok"):
            return resp["result"]
        return resp

    eff_policy = _deployment_policy(policy, dep)
    eff_policy["allowed_vision_models"] = vision_models
    try:
        llm_result = call_llm(
            conn, ctx, eff_policy, model=model, prompt=prompt_text,
            redacted_input=text_input, fake_invoke=_invoke,
            model_field="allowed_vision_models")
    except Exception as e:  # 闸门拒绝 / 断网 / 超时 / 解析异常：有痕降级
        return _fail(ok=False, degraded=False, blocked=False, mode=mode,
                     error=str(e), model=model, log_id=None)

    raw_result = llm_result["result"]
    # 调用层失败（无 API key / HTTP 错误 / 超时被 client 捕获后以 ok=False
    # 返回，而非抛异常）：必须透传真实原因，不得伪装成“JSON 解析失败”，
    # 否则会把环境/网络问题误导成模型输出格式问题（成功回包 result 无 ok 键）
    if isinstance(raw_result, dict) and raw_result.get("ok") is False:
        call_error = str(raw_result.get("error") or "未知调用错误")
        return _fail(ok=False, degraded=False, blocked=False, mode=mode,
                     error=f"VLM 调用失败：{call_error}",
                     model=raw_result.get("model") or model,
                     log_id=llm_result.get("log_id"))
    parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else None
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if not isinstance(findings, list):
        # chat_json 已做围栏兼容 + 一次格式纠偏重问；仍失败时带上回包片段，
        # 区分截断/拒答/散文偏航（片段只回传发起人，不落库）
        parse_error = (raw_result.get("parse_error")
                       if isinstance(raw_result, dict) else "") or ""
        reason = 'VLM 输出无法解析为 {"findings": [...]} JSON'
        if parse_error:
            reason = f"{reason}；{parse_error}"
        return _fail(ok=False, degraded=False, blocked=False, mode=mode,
                     error=reason,
                     model=model, log_id=llm_result.get("log_id"))

    # ⑪ 守门 + 逐条落 image_draft 提案
    store = ProposalStore(conn, pack=pack)
    proposals: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    created_epoch = time.time()
    for i, raw in enumerate(findings):
        cand, score, reason = _clean_finding(raw, i)
        if cand is None:
            dropped.append({"index": i, "reason": reason})
            continue
        proposal = {
            "proposal_id": f"pp-{uuid.uuid4().hex[:12]}",
            "kind": "image_draft",
            "case_id": case_id,
            "author": f"model:{model}",
            "candidate": cand,
            "input": {
                "origin": "ai_draft",
                "purpose": LENS_ID,
                "mode": mode,
                "model": model,
                "prompt_version": prompt_version,
                "image_uri": image_uri,
                "content_class": content_class,
                "subject_type": str(subject_type or ""),
                "subject_id": str(subject_id or ""),
                "exif_stripped": True,
                "created_epoch": created_epoch,
                "ttl_s": int(spec.result_ttl_s),
                "prompt_sha1": hashlib.sha1(
                    prompt_text.encode("utf-8")).hexdigest(),
                "llm_log_id": llm_result.get("log_id"),
            },
            "_sort_hint": {"model": model,
                           "model_score": score if score is not None else 0.0},
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
            "title": cand["title"],
            "detail": cand["detail"],
            "severity": cand["severity"],
            "model_score": score,
            "stale": False,
            "model": model,
            "prompt_version": prompt_version,
        })

    # ⑫ shadow 断言
    diff = shadow_diff(snapshot_before, table_snapshot(conn))
    if diff:  # pragma: no cover - 结构性防御
        raise RuntimeError(f"P8 shadow 隔离被破坏：{diff}")

    return _fail(ok=True, degraded=False, blocked=False, mode=mode,
                 model=model, log_id=llm_result.get("log_id"),
                 proposals=proposals, dropped=dropped)
