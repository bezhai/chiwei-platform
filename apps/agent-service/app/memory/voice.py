"""Unified voice generation — inner monologue + reply style in one LLM call.

Replaces the old ``voice_generator.py``.  Output is the full ``<voice>``
section injected into the system prompt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig
from app.data.queries import (
    find_latest_life_state,
    find_plan_for_period,
    find_today_fragments,
    insert_reply_style,
)
from app.data.session import get_session
from app.memory._persona import load_persona

_VOICE_CFG = AgentConfig("voice_generator", "offline-model", "voice-generator")

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


async def generate_voice(
    persona_id: str,
    recent_context: str = "",
    source: str = "cron",
) -> str | None:
    """Generate full voice content (inner monologue + style examples)."""
    pc = await load_persona(persona_id)
    # fallback: persona_id == display_name means persona not found
    if pc.display_name == persona_id and not pc.persona_lite:
        return None

    async with get_session() as s:
        le_state = await find_latest_life_state(s, persona_id)
    current_state = le_state.current_state if le_state else "（状态未知）"
    response_mood = le_state.response_mood if le_state else ""

    now = datetime.now(_CST)
    today = now.strftime("%Y-%m-%d")

    async with get_session() as s:
        schedule = await find_plan_for_period(s, "daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天没有安排）"

    async with get_session() as s:
        frags = await find_today_fragments(s, persona_id, grains=["conversation"])
    frag_text = (
        "\n".join(f.content[:100] for f in frags[-3:])
        if frags
        else "（今天还没跟人聊过）"
    )

    recent_ctx_block = ""
    if recent_context:
        recent_ctx_block = f"最近的对话和你的回复：\n{recent_context}"

    result = await Agent(_VOICE_CFG).run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "current_state": current_state,
            "response_mood": response_mood,
            "schedule_segment": schedule_text,
            "recent_fragments": frag_text,
            "recent_context": recent_ctx_block,
            "current_time": now.strftime("%H:%M"),
        },
        messages=[HumanMessage(content="生成当前状态的内心独白和语气示例")],
    )

    content = result.content or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()

    if not content:
        logger.warning("[%s] Voice generation returned empty", persona_id)
        return None

    async with get_session() as s:
        await insert_reply_style(
            s, persona_id=persona_id, style_text=content, source=source
        )
    logger.info("[%s] Voice generated (%s): %s...", persona_id, source, content[:60])
    return content
