"""
server/app/routers/escape_hatch.py
W-031：代码逃生舱——四类扩展代码桩生成。

- generate：POST 传入 ext_type/name/description，返回代码桩文本（不写文件、不注册）；
- stats：GET 逃生舱触发统计（哪类需求反复出现，飞轮报表）。

不执行、不注册 core：仅生成文本交开发者手动合入（req.md 明确不做租户沙箱在线执行）。
"""
from __future__ import annotations

import json
from collections import Counter

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from server.app.deps import WebContext, get_ctx, get_principal
from server.app.envelope import ERR_VALIDATION, APIError, ok
from server.app.security import Principal
from server.app.worker.escape_hatch import EXT_TYPES, generate_stub

router = APIRouter(prefix="/escape-hatch", tags=["escape-hatch"])


class GenerateIn(BaseModel):
    ext_type: str = Field(..., description="扩展类型")
    name: str = Field(min_length=1, max_length=64)
    description: str = Field("", max_length=500)


@router.post("/generate")
def generate(body: GenerateIn,
             p: Principal = Depends(get_principal),
             ctx: WebContext = Depends(get_ctx)):
    """W-031 AC-1~4：生成代码桩（签名 + 契约 + 测试骨架 + 注册点说明）。"""
    if body.ext_type not in EXT_TYPES:
        raise APIError(
            ERR_VALIDATION,
            f"不支持的扩展类型：{body.ext_type}（可用：{list(EXT_TYPES)}）",
            400)
    result = generate_stub(body.ext_type, body.name, body.description)
    # AC-5：统计触发
    ctx.repo.record_ops(
        "escape_hatch_generate", case_id="",
        payload={
            "operator": p.operator,
            "ext_type": body.ext_type,
            "name": body.name,
            "description": body.description,
        })
    return ok(result)


@router.get("/stats")
def stats(p: Principal = Depends(get_principal),
          ctx: WebContext = Depends(get_ctx)):
    """W-031 AC-5：逃生舱触发统计（按 ext_type 聚合）。"""
    events = ctx.repo.list_ops(kind="escape_hatch_generate", limit=1000)
    counter: Counter[str] = Counter()
    for ev in events:
        try:
            payload = json.loads(ev.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        counter[payload.get("ext_type", "unknown")] += 1
    return ok({
        "items": [{"ext_type": k, "count": v} for k, v in counter.most_common()],
        "total": sum(counter.values()),
    })
