"""Heavy reviewer — daily global consolidation.

Replaces the previous daily dream pipeline. Runs at 03:00 CST per persona.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.data.queries import (
    list_abstracts_window,
    list_fragments_window,
    list_recent_life_states,
    list_recent_schedule_revisions,
)
from app.data.session import get_session
from app.memory.reviewer.tools import make_reviewer_tools
from app.workers.common import for_each_persona

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_HEAVY_CFG = AgentConfig("memory_reviewer_heavy", "offline-model", "memory-reviewer-heavy")


async def _run_agent(
    *,
    persona_id: str,
    now: datetime,
    fragments_text: str,
    abstracts_text: str,
    life_states_text: str,
    schedule_text: str,
) -> None:
    await Agent(_HEAVY_CFG, tools=make_reviewer_tools()).run(
        messages=[HumanMessage(content="执行重档 review（睡前整理）")],
        prompt_vars={
            "persona_id": persona_id,
            "now": now.isoformat(),
            "day_fragments": fragments_text or "（无）",
            "day_abstracts": abstracts_text or "（无）",
            "day_life_states": life_states_text or "（无）",
            "day_schedules": schedule_text or "（无）",
        },
        context=AgentContext(persona_id=persona_id),
    )


async def run_heavy_review_for_persona(persona_id: str) -> None:
    now = datetime.now(CST)
    since = now - timedelta(days=1)

    async with get_session() as s:
        fragments = await list_fragments_window(s, persona_id=persona_id, since=since)
        abstracts = await list_abstracts_window(s, persona_id=persona_id, since=since)
        life_states = await list_recent_life_states(s, persona_id=persona_id, since=since)
        schedules = await list_recent_schedule_revisions(s, persona_id=persona_id, since=since)

    if not fragments and not abstracts and not life_states and not schedules:
        logger.info("[%s] heavy review: empty day, skip", persona_id)
        return

    def fmt_frag(f):
        return f"- [{f.id}] {f.content[:200]}"

    def fmt_abs(a):
        return f"- [{a.id} subject={a.subject}] {a.content[:200]}"

    def fmt_life(l):
        return (
            f"- {l.created_at.isoformat()} [{l.activity_type}] "
            f"{l.current_state[:80]} mood={l.response_mood}"
        )

    def fmt_sched(sr):
        return (
            f"- {sr.created_at.isoformat()} [{sr.created_by}] reason={sr.reason[:80]}"
        )

    logger.info(
        "[%s] heavy review: %d fragments, %d abstracts, %d states, %d schedules",
        persona_id,
        len(fragments),
        len(abstracts),
        len(life_states),
        len(schedules),
    )

    await _run_agent(
        persona_id=persona_id,
        now=now,
        fragments_text="\n".join(fmt_frag(f) for f in fragments),
        abstracts_text="\n".join(fmt_abs(a) for a in abstracts),
        life_states_text="\n".join(fmt_life(l) for l in life_states),
        schedule_text="\n".join(fmt_sched(sr) for sr in schedules),
    )


async def run_heavy_review() -> None:
    """Cron entry: run heavy review for all personas."""
    await for_each_persona(run_heavy_review_for_persona, label="memory_reviewer_heavy")
