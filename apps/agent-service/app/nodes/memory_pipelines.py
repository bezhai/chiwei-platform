"""Memory pipeline @node consumers (afterthought).

Single-flight per (chat, persona) via the runtime ``single_flight`` capability
(SETNX uuid token + Lua compare-and-delete; if LLM stalls past TTL and a new
fire grabs the lock, the old finally sees a different token and leaves the
new lock alone).

Lock contention raises ``DebounceReschedule(SameTrigger)`` — the runtime
debounce handler catches it and runs ``_do_reschedule`` with its own
trigger_id, so a fresh delayed fire takes phase2's place after the lock
releases.

drift（上下文变化 → voice 再生成）管线随 voice 子系统拆除删除。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.data.ids import new_id
from app.data.queries import (
    find_group_name,
    find_messages_in_range,
    find_username,
    insert_fragment,
)
from app.domain.agent_tool_events import AbstractMemoryCommitted
from app.domain.memory_request import MemoryAbstractRequest, MemoryFragmentRequest
from app.domain.memory_triggers import AfterthoughtTrigger
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
async def afterthought_check(trigger: AfterthoughtTrigger) -> None:
    """Single-flight conversation fragment generation per (chat, persona).

    Lock contention raises DebounceReschedule(SameTrigger) — the debounce
    handler catches and runs _do_reschedule with its own trigger_id, so a
    fresh delayed fire takes phase2's place after the lock releases.
    """
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
