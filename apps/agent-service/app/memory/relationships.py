"""Relationship memory extraction — two-stage pipeline.

Stage 1: Topic segmentation + relevance filter (group chats only; p2p skips).
Stage 2: Extract ``core_facts`` + ``impression`` updates per user.

Called by afterthought (fire-and-forget) and rebuild (direct).
All LLM calls go through ``Agent``.
"""

from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage

from app.agent.core import Agent
from app.data.queries import (
    find_relationship_memories_batch,
    find_username,
    insert_relationship_memory,
)
from app.data.session import get_session
from app.memory._persona import load_persona
from app.memory._timeline import format_timeline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(content) -> list | None:
    """Parse a JSON array from LLM response content."""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    else:
        content = (content or "").strip()

    if not content or content == "[]":
        return None

    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Stage 1 — topic filter (group chats)
# ---------------------------------------------------------------------------


async def _filter_relevant_messages(
    messages: list,
    persona_name: str,
    persona_lite: str,
) -> list[int]:
    """Topic segmentation + persona-relevance filter.

    Sends all messages (with ``#id``) to LLM; returns message IDs from
    topics the persona participated in.
    """
    timeline = await format_timeline(messages, persona_name, with_ids=True)
    if not timeline:
        return []

    result = await Agent("relationship-filter").run(
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "messages": timeline,
        },
        messages=[HumanMessage(content="分析对话，找出我参与的话题")],
    )

    topics = _parse_json(result.content)
    if not topics:
        logger.info("[%s] Stage 1: no relevant topics found", persona_name)
        return []

    relevant_ids: set[int] = set()
    for topic in topics:
        if isinstance(topic, dict):
            for mid in topic.get("message_ids", []):
                if isinstance(mid, int):
                    relevant_ids.add(mid)

    logger.info(
        "[%s] Stage 1: %d topics, %d relevant messages out of %d",
        persona_name,
        len(topics),
        len(relevant_ids),
        len(messages),
    )
    return list(relevant_ids)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def extract_relationship_updates(
    persona_id: str,
    chat_id: str,
    user_ids: list[str],
    messages: list,
) -> None:
    """Two-stage relationship memory extraction.

    Stage 1: filter relevant messages (group chats only).
    Stage 2: extract core_facts + impression updates.

    Used by both afterthought and rebuild.
    """
    if not user_ids or not messages:
        return

    pc = await load_persona(persona_id)
    persona_name = pc.display_name
    persona_lite = pc.persona_lite

    chat_type = messages[0].chat_type if messages else "group"

    if chat_type == "p2p":
        filtered_messages = messages
        filtered_user_ids = user_ids
    else:
        relevant_ids = await _filter_relevant_messages(
            messages, persona_name, persona_lite
        )
        if not relevant_ids:
            logger.info(
                "[%s] No relevant messages for chat %s, skip extract",
                persona_id,
                chat_id,
            )
            return

        id_set = set(relevant_ids)
        filtered_messages = [m for m in messages if m.id in id_set]
        filtered_user_ids = list(
            {
                m.user_id
                for m in filtered_messages
                if m.role == "user" and m.user_id and m.user_id != "__proactive__"
            }
        )
        if not filtered_user_ids:
            return

    # --- Build context for extraction ---
    filtered_timeline = await format_timeline(filtered_messages, persona_name)

    async with get_session() as s:
        current_memories = await find_relationship_memories_batch(
            s, persona_id, filtered_user_ids
        )

    core_facts_lines: list[str] = []
    impression_lines: list[str] = []
    for uid in filtered_user_ids:
        async with get_session() as s:
            name = await find_username(s, uid) or uid[:6]
        mem = current_memories.get(uid)
        if mem:
            core_facts, impression = mem
            core_facts_lines.append(f"- {name}({uid}): {core_facts or '（无）'}")
            impression_lines.append(f"- {name}({uid}): {impression or '（无）'}")
        else:
            core_facts_lines.append(f"- {name}({uid}): （第一次互动）")
            impression_lines.append(f"- {name}({uid}): （第一次互动）")

    result = await Agent("relationship-extract").run(
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "messages": filtered_timeline,
            "current_core_facts": "\n".join(core_facts_lines),
            "current_impression": "\n".join(impression_lines),
        },
        messages=[HumanMessage(content="根据对话更新关系记忆")],
    )

    updates = _parse_json(result.content)
    if not updates:
        logger.info("[%s] No relationship updates for chat %s", persona_id, chat_id)
        return

    for item in updates:
        if not isinstance(item, dict):
            continue
        uid = item.get("user_id", "")
        core_facts = item.get("core_facts", "")
        impression = item.get("impression", "")
        if uid and (core_facts or impression):
            async with get_session() as s:
                fallback_name = await find_username(s, uid) or uid[:6]
            name = item.get("user_name", "") or fallback_name
            async with get_session() as s:
                await insert_relationship_memory(
                    s,
                    persona_id=persona_id,
                    user_id=uid,
                    core_facts=core_facts,
                    impression=impression,
                    source="afterthought",
                )
            logger.info(
                "[%s] Relationship updated for %s: facts=%s... impression=%s...",
                persona_id,
                name,
                core_facts[:30],
                impression[:30],
            )
