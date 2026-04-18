"""commit_abstract_memory tool — sink for in-conversation abstractions."""

from __future__ import annotations

import logging

from langchain.tools import tool
from langgraph.runtime import get_runtime

from app.agent.context import AgentContext
from app.agent.tools._common import tool_error
from app.data.ids import new_id
from app.data.queries import (
    get_fragment_by_id,
    insert_abstract_memory,
    insert_memory_edge,
)
from app.data.session import get_session
from app.memory.conflict import detect_conflict
from app.memory.vectorize_memory import enqueue_abstract_vectorize

logger = logging.getLogger(__name__)


async def _commit_abstract_impl(
    *,
    persona_id: str,
    subject: str,
    content: str,
    supported_by_fact_ids: list[str] | None,
    reasoning: str | None,
) -> dict:
    subject = (subject or "").strip()
    content = (content or "").strip()
    if not subject or not content:
        return {"error": "subject 和 content 不能为空"}

    # Validate fact ids exist before mutating anything
    if supported_by_fact_ids:
        async with get_session() as s:
            for fid in supported_by_fact_ids:
                f = await get_fragment_by_id(s, fid)
                if f is None:
                    return {"error": f"fact id {fid} 不存在"}

    hint = await detect_conflict(
        persona_id=persona_id, subject=subject, content=content,
    )

    aid = new_id("a")
    async with get_session() as s:
        await insert_abstract_memory(
            s, id=aid, persona_id=persona_id,
            subject=subject, content=content,
            created_by="chiwei",
        )
        for fid in supported_by_fact_ids or []:
            await insert_memory_edge(
                s, id=new_id("e"), persona_id=persona_id,
                from_id=fid, from_type="fact",
                to_id=aid, to_type="abstract",
                edge_type="supports", created_by="chiwei",
                reason=reasoning,
            )

    await enqueue_abstract_vectorize(aid)

    return {"id": aid, "conflict_hint": hint}


@tool
@tool_error("抽象记忆保存失败")
async def commit_abstract_memory(
    subject: str,
    content: str,
    supported_by_fact_ids: list[str] | None = None,
    reasoning: str | None = None,
) -> dict:
    """沉淀一条抽象认识到长期记忆。

    当你在对话里对某个人/话题/自己有了一个新的认识（不是单一事实而是"认识"），
    把它用简洁的一段话写下来。subject 是这条认识是关于什么的（可以是人名、"self"、
    某个话题）。如果你有具体事实作为依据，传 supported_by_fact_ids。

    如果你写入的内容和已有抽象高度相似，返回里会有 conflict_hint，告诉你旧抽象是啥，
    你可以选择：忽略、稍后自己改写、或者再补充更精确的表达。

    Args:
        subject: 这条认识是关于什么的（自由字符串）
        content: 认识本身（简洁，一段话）
        supported_by_fact_ids: 可选，支撑这条认识的 fragment id 列表
        reasoning: 可选，你写下这条认识的原因（帮助未来 review）
    """
    context = get_runtime(AgentContext).context
    return await _commit_abstract_impl(
        persona_id=context.persona_id,
        subject=subject,
        content=content,
        supported_by_fact_ids=supported_by_fact_ids,
        reasoning=reasoning,
    )
