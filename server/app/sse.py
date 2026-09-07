"""
server/app/sse.py
任务进度 SSE（D4：MVP 轮询 tasks 表，500ms 节流）。

纪律（S1）：
  - 与普通端点同一 Bearer 鉴权（路由层 Depends(get_principal)），
    不开 Cookie / query-token 旁路；
  - Last-Event-ID：重连后事件序号从该值继续（进度为轮询快照，
    断线期间的中间帧不补发——终态帧保证送达）。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi.responses import StreamingResponse

from server.app.deps import task_dto
from server.app.meta.models import TASK_FAILED, TASK_SUCCEEDED
from server.app.meta.repo import MetaRepo

_TERMINAL = frozenset({TASK_SUCCEEDED, TASK_FAILED})
POLL_INTERVAL = 0.5


def _frame(seq: int, event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n"


async def task_event_stream(repo: MetaRepo, task_id: str,
                            last_event_id: str | None) -> AsyncIterator[str]:
    try:
        seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        seq = 0
    last_sig: tuple | None = None
    while True:
        task = repo.get_task(task_id)
        if task is None:
            seq += 1
            yield _frame(seq, "error", {"message": "任务不存在"})
            return
        sig = (task.status, task.progress_pct, task.progress_stage,
               task.progress_label, task.progress_detail,
               task.error_code, task.error_message)
        if sig != last_sig:
            seq += 1
            event = "terminal" if task.status in _TERMINAL else "progress"
            yield _frame(seq, event, task_dto(task))
            last_sig = sig
            if task.status in _TERMINAL:
                return
        await asyncio.sleep(POLL_INTERVAL)


def sse_response(repo: MetaRepo, task_id: str,
                 last_event_id: str | None) -> StreamingResponse:
    return StreamingResponse(
        task_event_stream(repo, task_id, last_event_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"})
