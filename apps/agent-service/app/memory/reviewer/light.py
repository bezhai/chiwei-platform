"""Light reviewer — short window, P0 operations only.

Runs every 30min (day) / 1h (night). Processes recent fragments + abstracts,
applies clarity adjustments, notes hints, time-passed rewrites via reviewer tools.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.data.queries import (
    get_active_notes,
    list_abstracts_window,
    list_fragments_window,
)
from app.data.session import get_session
from app.memory.reviewer.tools import make_reviewer_tools

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_LIGHT_CFG = AgentConfig(
    "memory_reviewer_light", "offline-model", "memory-reviewer-light"
)


def _fmt_fragment(f) -> str:
    return f"- [{f.id}] {f.content[:200]}"


def _fmt_abstract(a) -> str:
    return f"- [{a.id} subject={a.subject}] {a.content[:200]}"


def _fmt_note(n) -> str:
    when = n.when_at.isoformat() if n.when_at else "-"
    return f"- [{n.id}] {n.content[:120]} (when={when})"


async def _run_reviewer_agent(
    *,
    persona_id: str,
    now: datetime,
    fragments_text: str,
    abstracts_text: str,
    notes_text: str,
) -> None:
    await Agent(_LIGHT_CFG, tools=make_reviewer_tools()).run(
        messages=[HumanMessage(content="执行轻档记忆 review")],
        prompt_vars={
            "persona_id": persona_id,
            "now": now.isoformat(),
            "recent_fragments": fragments_text,
            "recent_abstracts": abstracts_text,
            "active_notes": notes_text,
        },
        context=AgentContext(persona_id=persona_id),
    )


async def run_light_review(*, persona_id: str, window_minutes: int) -> None:
    now = datetime.now(CST)
    since = now - timedelta(minutes=window_minutes)

    async with get_session() as s:
        fragments = await list_fragments_window(s, persona_id=persona_id, since=since)
        abstracts = await list_abstracts_window(s, persona_id=persona_id, since=since)
        notes = await get_active_notes(s, persona_id=persona_id)

    if not fragments and not abstracts and not notes:
        logger.info("[%s] light review: empty window, skip", persona_id)
        return

    logger.info(
        "[%s] light review: %d fragments, %d abstracts, %d notes",
        persona_id,
        len(fragments),
        len(abstracts),
        len(notes),
    )

    await _run_reviewer_agent(
        persona_id=persona_id,
        now=now,
        fragments_text="\n".join(_fmt_fragment(f) for f in fragments) or "（无）",
        abstracts_text="\n".join(_fmt_abstract(a) for a in abstracts) or "（无）",
        notes_text="\n".join(_fmt_note(n) for n in notes) or "（无）",
    )
