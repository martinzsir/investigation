"""
server/app/routers/rule_workshop.py
W-014 规则工坊 + W-025 LLM 边界守卫（M3）。

读：GET /cases/{cid}/rules —— 案件快照 rules.json（建案锁定，多租户隔离）。
写：PUT /cases/{cid}/rules/{rid} —— 仅允许 rule_text（判据文本）/ params
    （阈值调参）/ enabled（启停）；function 等结构字段不可改（绑定关系可审计
    而不可偷换，AC-4）。写盘前在临时副本上过 core loader 全量强校验
    （function 白名单/参数 enum/类型/维度/间类/hit_when），不合法不落盘
    （AC-1/2/3）；params/enabled 变更入队 RESCAN（AC-6）。
草案：POST /cases/{cid}/rules/draft —— LLM 守卫层（M3 不接模型 SDK）：
    llm_enabled=false → 503；注入特征拒绝；PII 脱敏前置；模型输出只许
    rule_text 键（含 function/params/SQL/动作一律拦截，AC-1）；产物标
    "待核实"、永不落盘、永不自动生效（AC-2/5）。

权限：GET 登录即可；PUT/draft 需偏将及以上（clearance>=2）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.llm.redact import load_llm_policy, redact_text
from core.ontology_loader import load_pack

from server.app.deps import WebContext, get_ctx, get_principal, task_dto
from server.app.envelope import (
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_VALIDATION,
    APIError,
    ok,
)
from server.app.routers.cases import _get_owned_case
from server.app.security import Principal
from server.app.worker.tasks import TASK_RESCAN, enqueue_task

router = APIRouter(tags=["rule-workshop"])

# PUT 允许修改的字段（结构字段 function/id/stage/hit_when 等一律拒绝）
_EDITABLE_FIELDS = {"rule_text", "params", "enabled"}
# LLM 草案输出只许出现的键（W-025 AC-1：含机器挂钩/写动作一律拦截）
_DRAFT_ALLOWED_KEYS = {"rule_text"}
_DRAFT_FORBIDDEN_KEYS = (
    "function", "params", "sql", "action", "writeback", "status",
    "hit_when", "stage", "id", "rule_id",
)
_RULE_TEXT_MIN = 20

# 注入特征（中英；命中即拒，AC-4）——守卫层宁枉勿纵
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous", r"ignore\s+above", r"disregard\s+(all|previous)",
    r"system\s*prompt", r"you\s+are\s+now", r"new\s+instructions",
    r"忽略(以上|前面|之前|上述)", r"无视(上述|以上|前面)", r"跳过(以上|前面|规则|校验)",
    r"系统提示词", r"你现在是", r"扮演(一个|一位)?", r"解除(限制|封印)",
    r"developer\s+mode", r"jailbreak", r"DROP\s+TABLE", r"--\s*$",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


class RuleEditIn(BaseModel):
    rule_text: str | None = None
    params: dict | None = None
    enabled: bool | None = None


class RuleDraftIn(BaseModel):
    question: str
    model_output: dict | None = None  # 测试/未来客户端接缝（M3 不接模型 SDK）
    model: str | None = None


def _snapshot_paths(ctx: WebContext, case_id: str) -> tuple[str, Path, Path]:
    """返回 (pack_id, 快照包目录, 快照 base_dir)。"""
    case = ctx.repo.get_case(case_id)
    pack_id = case.pack_id if case else "default"
    snap_dir = ctx.cases.snapshot_dir(case_id, pack_id)
    base_dir = ctx.cases.snapshot_ontology_root(case_id)
    if not snap_dir.exists():
        raise APIError(ERR_NOT_FOUND, f"案件快照不存在：{case_id}", 404)
    return pack_id, snap_dir, base_dir


def _require_analyst(p: Principal) -> None:
    if p.role not in ("human", "system") and p.clearance < 2:
        raise APIError(ERR_FORBIDDEN,
                       "规则工坊需偏将及以上（clearance>=2）", 403)


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


@router.get("/cases/{case_id}/rules")
def list_rules(case_id: str,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """规则手册只读列表 + function 白名单目录（工坊 UI 挂参用）。"""
    _get_owned_case(case_id, p, ctx.cases)
    pack_id, _, base_dir = _snapshot_paths(ctx, case_id)
    spec = load_pack(pack_id, base_dir=base_dir)
    rules = [r.to_dict() for r in spec.rules.values()]
    return ok({
        "rules": rules,
        "function_catalog": sorted(spec.functions.keys()),
        "pack": pack_id,
    }, data_version=ctx.repo.current_version(case_id))


@router.put("/cases/{case_id}/rules/{rule_id}")
def edit_rule(case_id: str, rule_id: str, body: RuleEditIn,
              p: Principal = Depends(get_principal),
              ctx: WebContext = Depends(get_ctx)):
    """编辑规则（判据文本/阈值/启停）；loader 强校验通过才落盘。"""
    _get_owned_case(case_id, p, ctx.cases)
    _require_analyst(p)
    pack_id, snap_dir, base_dir = _snapshot_paths(ctx, case_id)

    rules_path = snap_dir / "rules.json"
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    target = next((r for r in data.get("rules", []) if r.get("id") == rule_id),
                  None)
    if target is None:
        raise APIError(ERR_NOT_FOUND, f"规则不存在：{rule_id}", 404)

    changes: list[str] = []
    if body.rule_text is not None:
        text = body.rule_text.strip()
        if len(text) < _RULE_TEXT_MIN:
            raise APIError(ERR_VALIDATION,
                           f"rule_text 过短（<{_RULE_TEXT_MIN} 字）：判据须写明"
                           "模式/反常理由/边界排除", 400)
        target["rule_text"] = text
        changes.append("rule_text")
    if body.params is not None:
        if not isinstance(body.params, dict):
            raise APIError(ERR_VALIDATION, "params 必须是对象", 400)
        merged = dict(target.get("params") or {})
        merged.update(body.params)
        target["params"] = merged
        changes.append("params")
    if body.enabled is not None:
        target["enabled"] = bool(body.enabled)
        changes.append("enabled")
    if not changes:
        raise APIError(ERR_VALIDATION,
                       f"未提供可修改字段（允许：{sorted(_EDITABLE_FIELDS)}）",
                       400)

    # 写盘前强校验：临时副本整包过 loader（function 白名单/参数 enum/类型/
    # 维度/间类/hit_when 交叉引用），不合法不落盘
    with tempfile.TemporaryDirectory() as td:
        tmp_root = Path(td)
        shutil.copytree(snap_dir, tmp_root / pack_id)
        _atomic_write_json(tmp_root / pack_id / "rules.json", data)
        try:
            load_pack(pack_id, base_dir=tmp_root)
        except Exception as e:
            raise APIError(ERR_VALIDATION, f"规则校验失败，未落盘：{e}", 400)

    _atomic_write_json(rules_path, data)
    ctx.repo.record_ops("rule_edit", case_id,
                        {"rule_id": rule_id, "changed": changes,
                         "by": p.operator})

    # params/enabled 变更影响机器结果 → 入队 RESCAN；纯 rule_text 文本修订
    # 不改变确定性执行结果，不触发重跑
    task = None
    if {"params", "enabled"} & set(changes):
        task = enqueue_task(
            ctx.repo, case_id=case_id, task_type=TASK_RESCAN,
            params={"rule_id": rule_id, "changed": changes},
            idem_key=f"rescan:{rule_id}:{ctx.repo.current_version(case_id)}",
            created_by=p.operator)
    return ok({"rule_id": rule_id, "changed": changes,
               "rescan_task": task_dto(task) if task else None},
              data_version=ctx.repo.current_version(case_id))


@router.post("/cases/{case_id}/rules/draft")
def draft_rule(case_id: str, body: RuleDraftIn,
               p: Principal = Depends(get_principal),
               ctx: WebContext = Depends(get_ctx)):
    """LLM 规则草案守卫层（W-025）：只产出待核实 rule_text，永不落盘。"""
    _get_owned_case(case_id, p, ctx.cases)
    _require_analyst(p)
    pack_id, _, base_dir = _snapshot_paths(ctx, case_id)

    policy = load_llm_policy(pack_id, base_dir=base_dir)
    if policy.get("llm_enabled") is False:
        raise APIError("LLM_DISABLED",
                       "本案件 LLM 能力已被策略一键关闭（llm_enabled=false）",
                       503)

    question = (body.question or "").strip()
    if not question:
        raise APIError(ERR_VALIDATION, "question 不能为空", 400)
    # AC-4：注入特征拒绝（脱敏前扫原文，防遮蔽绕过）
    if _INJECTION_RE.search(question):
        raise APIError("LLM_INJECTION",
                       "问句含提示注入特征，已拒绝（W-025 AC-4）", 400)
    # AC-3：脱敏前置（证件/手机号/银行卡遮蔽）
    redacted_q, redact_stats = redact_text(question, policy)

    # M3 仅交付守卫层：不引入模型 SDK；模型输出经接缝注入（测试/未来客户端）
    if body.model_output is None:
        raise APIError("LLM_CHANNEL_UNAVAILABLE",
                       "模型通道未接线（M3 仅交付守卫与拦截层）", 503)

    out = body.model_output
    if not isinstance(out, dict):
        raise APIError("LLM_OUTPUT_REJECTED", "模型输出必须是 JSON 对象", 400)
    # AC-1：只许 rule_text；含 function/params/SQL/动作键一律拦截
    extra = set(out) - _DRAFT_ALLOWED_KEYS
    forbidden = set(out) & set(_DRAFT_FORBIDDEN_KEYS)
    if forbidden or extra:
        hit = sorted(forbidden or extra)
        raise APIError("LLM_OUTPUT_REJECTED",
                       f"模型输出含禁止字段 {hit}：草案只允许 rule_text 键，"
                       "机器挂钩（function/params）须人工在工坊显式绑定", 400)
    rule_text = (out.get("rule_text") or "").strip()
    if len(rule_text) < _RULE_TEXT_MIN:
        raise APIError("LLM_OUTPUT_REJECTED",
                       f"rule_text 缺失或过短（<{_RULE_TEXT_MIN} 字）", 400)
    if _INJECTION_RE.search(rule_text):
        raise APIError("LLM_INJECTION",
                       "草案文本含注入特征，已拒绝（W-025 AC-4）", 400)

    # AC-2/5：待核实标记 + 永不落盘断言（本函数不执行任何 rules.json 写）
    return ok({
        "rule_text": rule_text,
        "review_status": "待核实",
        "persisted": False,
        "redacted_question": redacted_q,
        "redaction": redact_stats,
        "model": body.model or "guard-seam",
        "note": "草案永不自动生效；核实后由偏将及以上在工坊手动落 rule_text",
    }, data_version=ctx.repo.current_version(case_id))
