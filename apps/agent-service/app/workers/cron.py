"""Cron task definitions — thin wrappers that delegate to domain modules.

Every cron function is 3-5 lines: import domain function, call for_each_persona.
Business logic lives in ``app.memory.*``, ``app.life.*``.
"""

from __future__ import annotations

import logging
from app.workers.common import cron_error_handler, for_each_persona, prod_only

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Voice (inner monologue + reply style)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_generate_voice(ctx) -> None:
    from app.memory.voice import generate_voice

    await for_each_persona(generate_voice, label="voice")


# ---------------------------------------------------------------------------
# Heavy reviewer (daily consolidation, replaces dream compression)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_generate_dreams(ctx) -> None:
    from app.memory.reviewer.heavy import run_heavy_review

    await run_heavy_review()


# ---------------------------------------------------------------------------
# Schedules (daily plan via Agent Team pipeline)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_generate_daily_plan(ctx) -> None:
    from app.life.schedule import generate_all_daily_plans

    await generate_all_daily_plans()


# ---------------------------------------------------------------------------
# Life Engine (every-minute tick)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_life_engine_tick(ctx) -> None:
    from app.life.engine import tick

    await for_each_persona(tick, label="life-tick")


# ---------------------------------------------------------------------------
# Glimpse (browsing observation, only for browsing personas)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_glimpse(ctx) -> None:
    import random

    from app.data import queries as Q
    from app.data.queries import list_all_persona_ids
    from app.data.session import get_session
    from app.life.glimpse import list_target_groups, run_glimpse

    # Probability of triggering glimpse when persona is NOT browsing
    # (simulates "pulling out phone for a quick glance")
    GLANCE_PROBABILITY = 0.15

    async with get_session() as s:
        persona_ids = await list_all_persona_ids(s)

    groups = list_target_groups()

    for persona_id in persona_ids:
        try:
            async with get_session() as s:
                state = await Q.find_latest_life_state(s, persona_id)

            activity = state.activity_type if state else ""

            if activity == "sleeping":
                continue

            if activity != "browsing" and random.random() >= GLANCE_PROBABILITY:
                continue

            for chat_id in groups:
                await run_glimpse(persona_id, chat_id)
        except Exception:
            logger.exception("[%s] Glimpse cron failed", persona_id)
