"""Glimpse -- browsing observation for group chats.

Flow: load incremental messages -> LLM observes (with last observation +
proactive history) -> optionally create fragment + submit proactive chat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.data import queries as Q
from app.data.models import ExperienceFragment
from app.data.session import get_session
from app.infra.config import settings
from app.life.proactive import (
    get_recent_proactive_records,
    get_unseen_messages,
    submit_proactive_chat,
)
from app.memory._persona import load_persona
from app.memory._timeline import format_timeline

_GLIMPSE_CFG = AgentConfig("glimpse_observe", "offline-model", "glimpse-observe")

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# Engineering cap on proactive messages per hour (LLM self-regulation is primary)
HOURLY_PROACTIVE_LIMIT = 2

TARGET_CHAT_IDS = [
    "oc_a44255e98af05f1359aeb29eeb503536",
    "oc_54713c53ff0b46cb9579d3695e16cbf8",
]


# ---------------------------------------------------------------------------
# Result enum
# ---------------------------------------------------------------------------


class GlimpseResult(StrEnum):
    """Possible outcomes of a glimpse run."""

    SKIPPED_NO_GROUP = "skipped:no_group"
    SKIPPED_NO_MESSAGES = "skipped:no_messages"
    SKIPPED_EMPTY_TIMELINE = "skipped:empty_timeline"
    SKIPPED_NOT_INTERESTING = "skipped:not_interesting"
    FRAGMENT_CREATED = "fragment_created"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_cst() -> datetime:
    return datetime.now(CST)


def list_target_groups() -> list[str]:
    """Return all groups to monitor."""
    return TARGET_CHAT_IDS


async def _get_group_name(chat_id: str) -> str:
    try:
        async with get_session() as s:
            name = await Q.find_group_name(s, chat_id)
        return name or chat_id[:10]
    except Exception:
        return chat_id[:10]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _call_glimpse_llm(
    persona_name: str,
    persona_lite: str,
    group_name: str,
    messages_text: str,
    last_observation: str = "",
    recent_proactive: list[dict] | None = None,
) -> str:
    """Call LLM for browsing observation."""
    proactive_hint = ""
    if recent_proactive:
        n = len(recent_proactive)
        times = "、".join(r["time"] for r in recent_proactive[:5])
        proactive_hint = (
            f"\n- 你今天已经在这个群主动说了 {n} 次话了（{times}），"
            "再多就烦人了，除非有真的让你忍不住的话题"
        )

    result = await Agent(_GLIMPSE_CFG).run(
        messages=[HumanMessage(content="观察群消息")],
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "group_name": group_name,
            "messages": messages_text,
            "last_observation": (
                f"你上次翻这个群的时候，心里想的是：「{last_observation}」\n"
                if last_observation
                else ""
            ),
            "recent_proactive": proactive_hint,
        },
    )
    return extract_text(result.content)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _strict_bool(value: object) -> bool:
    """Parse bool strictly — string "false"/"False" → False, not True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def parse_glimpse_response(raw: str) -> dict:
    """Parse glimpse LLM JSON response."""
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return {
                "interesting": _strict_bool(data.get("interesting", False)),
                "observation": data.get("observation", ""),
                "want_to_speak": _strict_bool(data.get("want_to_speak", False)),
                "speak_reason": data.get("speak_reason", ""),
                "stimulus": data.get("stimulus"),
                "target_message_id": data.get("target_message_id"),
            }
    except (json.JSONDecodeError, ValueError):
        pass
    return {"interesting": False}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_glimpse(persona_id: str, chat_id: str) -> GlimpseResult:
    """Execute one browsing observation cycle for a specific group.

    Returns a ``GlimpseResult`` enum value.
    """
    now = _now_cst()

    # 1. Load glimpse state (incremental since last seen)
    async with get_session() as s:
        state = await Q.find_latest_glimpse_state(s, persona_id, chat_id)
    last_seen = state.last_seen_msg_time if state else 0
    last_observation = state.observation if state else ""

    # 2. Skip conversations bot already participated in
    async with get_session() as s:
        bot_reply_time = await Q.find_last_bot_reply_time(s, chat_id)
    effective_after = max(last_seen, bot_reply_time)

    # 3. Fetch incremental messages
    messages = await get_unseen_messages(chat_id, after=effective_after)
    if not messages:
        logger.debug("[%s] Glimpse: no new messages in %s", persona_id, chat_id)
        return GlimpseResult.SKIPPED_NO_MESSAGES

    # 4. Prepare context
    pc = await load_persona(persona_id)
    persona_name, persona_lite = pc.display_name, pc.persona_lite
    group_name = await _get_group_name(chat_id)
    messages_text = await format_timeline(
        messages, persona_name, tz=CST, max_messages=30, with_ids=True
    )

    if not messages_text.strip():
        return GlimpseResult.SKIPPED_EMPTY_TIMELINE

    # 4b. Today's proactive history (for LLM self-regulation + engineering cap)
    recent_proactive = await get_recent_proactive_records(chat_id)

    # 5. LLM observation
    raw = await _call_glimpse_llm(
        persona_name=persona_name,
        persona_lite=persona_lite,
        group_name=group_name,
        messages_text=messages_text,
        last_observation=last_observation,
        recent_proactive=recent_proactive,
    )
    decision = parse_glimpse_response(raw)

    new_last_seen = messages[-1].create_time

    if not decision.get("interesting"):
        logger.info("[%s] Glimpse: nothing interesting in %s", persona_id, group_name)
        async with get_session() as s:
            await Q.insert_glimpse_state(
                s,
                persona_id=persona_id,
                chat_id=chat_id,
                last_seen_msg_time=new_last_seen,
                observation="",
            )
        return GlimpseResult.SKIPPED_NOT_INTERESTING

    # 6. Create fragment
    observation = decision.get("observation", "")
    if observation:
        first_ts = messages[0].create_time
        last_ts = messages[-1].create_time
        fragment = ExperienceFragment(
            persona_id=persona_id,
            grain="glimpse",
            source_chat_id=chat_id,
            source_type="group",
            time_start=first_ts,
            time_end=last_ts,
            content=observation,
            mentioned_entity_ids=[],
            model=settings.life_engine_model,
        )
        async with get_session() as s:
            await Q.insert_fragment(s, fragment)
        logger.info("[%s] Glimpse fragment: %s...", persona_id, observation[:60])

    # 7. Proactive chat
    speak_reason = decision.get("speak_reason", "")
    state_observation = observation
    if decision.get("want_to_speak"):
        stimulus = decision.get("stimulus", "")
        target = decision.get("target_message_id") or None
        # Engineering cap: count proactive messages in the past hour
        one_hour_ago = now - timedelta(hours=1)
        hour_cutoff = one_hour_ago.strftime("%H:%M")
        recent_hour_count = sum(1 for r in recent_proactive if r["time"] >= hour_cutoff)
        if recent_hour_count >= HOURLY_PROACTIVE_LIMIT:
            state_observation = (
                f"{observation}\n[want_to_speak:throttled] "
                f"reason={speak_reason}, stimulus={stimulus}, "
                f"count={recent_hour_count}/{HOURLY_PROACTIVE_LIMIT}"
            )
            logger.info(
                "[%s] Glimpse want_to_speak throttled: %d>=%d",
                persona_id,
                recent_hour_count,
                HOURLY_PROACTIVE_LIMIT,
            )
        else:
            state_observation = (
                f"{observation}\n[want_to_speak] "
                f"reason={speak_reason}, stimulus={stimulus}, target={target}"
            )
            logger.info(
                "[%s] Glimpse want_to_speak: %s | %s",
                persona_id,
                speak_reason,
                stimulus,
            )
            try:
                await submit_proactive_chat(
                    chat_id=chat_id,
                    persona_id=persona_id,
                    target_message_id=target,
                    stimulus=stimulus,
                )
            except Exception as exc:
                logger.error(
                    "[%s] Glimpse proactive submit failed: %s", persona_id, exc
                )
    elif speak_reason:
        state_observation = f"{observation}\n[no_speak] reason={speak_reason}"

    # 8. Persist glimpse state
    async with get_session() as s:
        await Q.insert_glimpse_state(
            s,
            persona_id=persona_id,
            chat_id=chat_id,
            last_seen_msg_time=new_last_seen,
            observation=state_observation,
        )

    return GlimpseResult.FRAGMENT_CREATED
