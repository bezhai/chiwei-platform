"""write_note / resolve_note tools — 赤尾自己的主动清单。"""

from __future__ import annotations

import logging
from datetime import datetime

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.ids import new_id
from app.data.queries import get_active_notes, insert_note
from app.data.queries import resolve_note as resolve_note_query
from app.data.session import get_session

logger = logging.getLogger(__name__)


async def _write_note_impl(
    *,
    persona_id: str,
    content: str,
    when_at: datetime | None,
) -> dict:
    content = (content or "").strip()
    if not content:
        return {"error": "content 不能为空"}

    nid = new_id("n")
    async with get_session() as s:
        await insert_note(
            s,
            id=nid,
            persona_id=persona_id,
            content=content,
            when_at=when_at,
        )

    async with get_session() as s:
        active = await get_active_notes(s, persona_id=persona_id)

    return {
        "id": nid,
        "active_notes": [
            {
                "id": n.id,
                "content": n.content,
                "when_at": n.when_at.isoformat() if n.when_at else None,
            }
            for n in active
        ],
    }


async def _resolve_note_impl(
    *,
    persona_id: str,
    note_id: str,
    resolution: str,
) -> dict:
    resolution = (resolution or "").strip()
    if not note_id or not resolution:
        return {"error": "note_id 和 resolution 都不能为空"}

    async with get_session() as s:
        await resolve_note_query(s, note_id=note_id, resolution=resolution)

    return {"ok": True}


@tool
@tool_error("笔记保存失败")
async def write_note(content: str, when_at: str | None = None) -> dict:
    """把一件你觉得必须记住的事写进清单。

    这是你自己的清单，不是系统强加的承诺列表。只有你觉得"不能忘"、"需要专门记住"的
    事才写。当时间相关的（比如"周五和浩南看电影"），传 when_at（ISO 8601 格式）。
    普通的备忘、情绪留痕也行。

    Args:
        content: 笔记内容
        when_at: 可选，ISO 8601 时间戳（"2026-04-18T19:00:00+08:00"）
    """
    context = get_runtime(AgentContext).context
    parsed_when_at: datetime | None = None
    if when_at is not None:
        try:
            parsed_when_at = datetime.fromisoformat(when_at)
        except ValueError:
            return {"error": f"when_at 格式无效: {when_at}"}
    return await _write_note_impl(
        persona_id=context.persona_id,
        content=content,
        when_at=parsed_when_at,
    )


@tool
@tool_error("清单更新失败")
async def resolve_note(note_id: str, resolution: str) -> dict:
    """把一条已经完结的笔记划掉。

    比如电影看了、想法落实了、或者你改主意不做了。resolution 写一句话说明结果
    （"看完了"/"改了主意"/"忘了"）。

    Args:
        note_id: 笔记 id（形如 "n_xxxxxx"）
        resolution: 结果描述
    """
    context = get_runtime(AgentContext).context
    return await _resolve_note_impl(
        persona_id=context.persona_id,
        note_id=note_id,
        resolution=resolution,
    )
