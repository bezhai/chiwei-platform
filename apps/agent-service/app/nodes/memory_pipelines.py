"""Memory pipeline @node consumers (drift / afterthought).

Single-flight per (chat, persona) via the runtime ``single_flight`` capability
(SETNX uuid token + Lua compare-and-delete; if LLM stalls past TTL and a new
fire grabs the lock, the old finally sees a different token and leaves the
new lock alone).

Lock contention raises ``DebounceReschedule(SameTrigger)`` — the runtime
debounce handler catches it and runs ``_do_reschedule`` with its own
trigger_id, so a fresh delayed fire takes phase2's place after the lock
releases.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.chat.content_parser import parse_content
from app.data.ids import new_id
from app.data.queries import (
    find_group_name,
    find_messages_in_range,
    find_username,
    insert_fragment,
    resolve_bot_name_for_persona,
)
from app.domain.agent_tool_events import AbstractMemoryCommitted
from app.domain.memory_request import MemoryAbstractRequest, MemoryFragmentRequest
from app.domain.memory_triggers import AfterthoughtTrigger, DriftTrigger
from app.memory._persona import load_persona
from app.memory._timeline import format_timeline
from app.runtime.db import emit_tx, tx
from app.runtime.debounce import DebounceReschedule
from app.runtime.node import node
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_LOOKBACK_HOURS = 2

_AFTERTHOUGHT_CFG = AgentConfig(
    "afterthought_conversation", "offline-model", "afterthought"
)


# ---------------------------------------------------------------------------
# Drift helpers
# ---------------------------------------------------------------------------


async def _run_drift(chat_id: str, persona_id: str) -> None:
    """Event-driven drift — call unified voice generation with recent context."""
    pc = await load_persona(persona_id)
    recent_messages = await _recent_timeline(chat_id, persona_name=pc.display_name)
    recent_replies = await _recent_persona_replies(chat_id, persona_id)

    if not recent_messages:
        logger.info("[%s] No recent messages for %s, skip drift", persona_id, chat_id)
        return

    parts: list[str] = []
    if recent_messages:
        parts.append(f"群里刚才发生的事：\n{recent_messages}")
    if recent_replies:
        parts.append(f"你最近的回复：\n{recent_replies}")
    recent_context = "\n\n".join(parts)

    from app.memory.voice import generate_voice

    await generate_voice(persona_id, recent_context=recent_context, source="drift")


async def _recent_timeline(
    chat_id: str, persona_name: str = "bot", max_messages: int = 50
) -> str:
    """Last 1 hour of messages formatted as timeline."""
    start_dt = datetime.now(_CST) - timedelta(hours=1)
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(_CST).timestamp() * 1000)

    messages = await find_messages_in_range(chat_id, start_ts, end_ts)
    if not messages:
        return ""

    return await format_timeline(
        messages, persona_name, tz=_CST, max_messages=max_messages
    )


async def _recent_persona_replies(
    chat_id: str, persona_id: str, max_replies: int = 10
) -> str:
    """Recent bot replies for drift diagnosis (matched by bot_name)."""
    now = datetime.now(_CST)
    start_ts = int((now - timedelta(hours=2)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    async with tx():
        messages = await find_messages_in_range(chat_id, start_ts, end_ts)
        if not messages:
            return ""
        bot_name = await resolve_bot_name_for_persona(persona_id, chat_id)

    persona_msgs = [
        m for m in messages if m.role == "assistant" and m.bot_name == bot_name
    ]
    persona_msgs = persona_msgs[-max_replies:]

    lines: list[str] = []
    for i, msg in enumerate(persona_msgs, 1):
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"{i}. {rendered[:200]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Afterthought helpers
# ---------------------------------------------------------------------------


async def _generate_fragment(chat_id: str, persona_id: str) -> None:
    """Generate a conversation-grain experience fragment.

    1. Fetch last 2 hours of messages
    2. Build scene description (group name / p2p partner)
    3. Format timeline
    4. Call LLM to generate fragment content
    5. Persist v4 Fragment and enqueue vectorize
    """
    now = datetime.now(_CST)
    start_ts = int((now - timedelta(hours=_LOOKBACK_HOURS)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    messages = await find_messages_in_range(chat_id, start_ts, end_ts)

    if not messages:
        logger.info(
            "[%s] No messages in last %dh for %s, skip",
            persona_id,
            _LOOKBACK_HOURS,
            chat_id,
        )
        return

    chat_type = messages[0].chat_type if messages else "group"

    pc = await load_persona(persona_id)
    scene = await _build_scene(chat_id, chat_type, messages)
    timeline = await format_timeline(messages, pc.display_name, tz=_CST)
    if not timeline:
        logger.info("[%s] Empty timeline for %s, skip", persona_id, chat_id)
        return

    result = await Agent(_AFTERTHOUGHT_CFG).run(
        prompt_vars={
            "persona_name": pc.display_name,
            "persona_lite": pc.persona_lite,
            "scene": scene,
            "messages": timeline,
        },
        messages=[Message(role=Role.USER, content="生成经历碎片")],
    )
    content = result.text()

    if not content:
        logger.warning(
            "[%s] Afterthought LLM returned empty for %s", persona_id, chat_id
        )
        return

    fid = new_id("f")
    async with tx():
        await insert_fragment(
            id=fid,
            persona_id=persona_id,
            content=content,
            source="afterthought",
            chat_id=chat_id,
        )
        await emit_tx(MemoryFragmentRequest(fragment_id=fid))
    logger.info(
        "[%s] Conversation fragment created for %s: %s...",
        persona_id,
        chat_id,
        content[:60],
    )


async def _build_scene(chat_id: str, chat_type: str, messages: list) -> str:
    """Build scene description for the prompt."""
    if chat_type == "p2p":
        for msg in messages:
            if msg.role == "user" and msg.user_id:
                name = await find_username(msg.user_id)
                if name:
                    return f"和{name}的私聊"
        return "一段私聊"

    try:
        group_name = await find_group_name(chat_id)
        if group_name:
            return f"在「{group_name}」群里"
    except Exception:
        pass
    return "在群里"


# ---------------------------------------------------------------------------
# @node consumers
# ---------------------------------------------------------------------------


@node
async def drift_check(trigger: DriftTrigger) -> None:
    """Single-flight drift detection per (chat, persona).

    Lock contention raises DebounceReschedule(SameTrigger) — the debounce
    handler catches and runs _do_reschedule with its own trigger_id, so a
    fresh delayed fire takes phase2's place after the lock releases.
    """
    try:
        async with single_flight(
            f"phase2:drift:{trigger.chat_id}:{trigger.persona_id}", ttl=600
        ):
            await _run_drift(trigger.chat_id, trigger.persona_id)
    except SingleFlightConflict:
        logger.info(
            "drift_check: phase2 in flight for chat_id=%s persona=%s, raise DebounceReschedule",
            trigger.chat_id, trigger.persona_id,
        )
        raise DebounceReschedule(DriftTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        )) from None


@node
async def afterthought_check(trigger: AfterthoughtTrigger) -> None:
    """Single-flight conversation fragment generation per (chat, persona)."""
    try:
        async with single_flight(
            f"phase2:afterthought:{trigger.chat_id}:{trigger.persona_id}", ttl=900
        ):
            await _generate_fragment(trigger.chat_id, trigger.persona_id)
    except SingleFlightConflict:
        logger.info(
            "afterthought_check: phase2 in flight for chat_id=%s persona=%s, raise DebounceReschedule",
            trigger.chat_id, trigger.persona_id,
        )
        raise DebounceReschedule(AfterthoughtTrigger(
            chat_id=trigger.chat_id, persona_id=trigger.persona_id,
        )) from None


@node
async def on_abstract_committed(e: AbstractMemoryCommitted) -> MemoryAbstractRequest:
    """Translate a tool-event into a vectorize request.

    commit_abstract emits AbstractMemoryCommitted after DB commit; this
    in-process node returns MemoryAbstractRequest so vectorize-worker
    picks it up via Source.mq. Future subscribers (reviewer notification,
    dirty cache invalidation, etc.) attach via wire(AbstractMemoryCommitted)
    instead of patching the tool body.
    """
    return MemoryAbstractRequest(abstract_id=e.abstract_id)
