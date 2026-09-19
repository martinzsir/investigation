"""
packs/vlm/impl.py
P8 VLM 镜头 handler（skill_invoke 契约的薄编排层）。

核心实现全部在 core.llm.draft_image（双闸/脱敏/TTL/shadow 单一事实源）；
本层只从 ctx 取运行依赖并透传 params。shadow 语义：handler 恒返回 []——
VLM 草案只落 image_draft 提案队列，不产 LineageClue、不进 finding，
人验通过 verify_image Action 后才由副作用成证据。
"""
from __future__ import annotations

from typing import Any

from core.llm.draft_image import draft_image_inspect


def inspect_lens(miao=None, store=None, ctx: dict | None = None,
                 params: dict | None = None, health=None) -> list:
    """视觉图像分析镜头。

    ctx 约定键：
      conn         —— 全局提案库 DuckDB 连接（必填）
      access       —— AccessContext（必填，network 决定部署档）
      image_loader —— Callable[[uri], bytes]（必填，生产从 evidence 目录读）
      llm_client   —— LLMClient（可选，测试注入）
    params 见 pack.json params_schema。
    """
    ctx = ctx or {}
    params = params or {}
    conn = ctx.get("conn")
    access = ctx.get("access")
    if conn is None or access is None:
        if health is not None:
            health.record(
                "skill_failed", severity="warning", source="vlm_inspect",
                reason="ctx 缺 conn/access（镜头无法执行）")
        return []

    result = draft_image_inspect(
        conn,
        access,
        case_id=str(ctx.get("case_id") or params.get("case_id") or ""),
        image_uri=str(params.get("image_uri") or ""),
        content_class=str(params.get("content_class") or ""),
        instruction=str(params.get("instruction") or ""),
        prompt_version=params.get("prompt_version") or None,
        subject_type=str(params.get("subject_type") or ""),
        subject_id=str(params.get("subject_id") or ""),
        pack=str(ctx.get("ontology_pack") or "default"),
        base_dir=ctx.get("base_dir"),
        llm_client=ctx.get("llm_client"),
        image_loader=ctx.get("image_loader"),
    )

    if health is not None:
        if result.get("ok"):
            health.record(
                "draft_created", severity="info", source="vlm_inspect",
                count=len(result.get("proposals") or []),
                dropped=len(result.get("dropped") or []))
        elif result.get("degraded"):
            health.record(
                "skill_degraded", severity="info", source="vlm_inspect",
                reason=result.get("reason", ""))
        else:
            health.record(
                "skill_failed", severity="warning", source="vlm_inspect",
                reason=str(result.get("error", "")))

    # shadow：草案不线索化
    return []
