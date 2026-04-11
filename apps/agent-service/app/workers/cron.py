"""Cron task definitions — thin wrappers that delegate to domain modules.

Every cron function is 3-5 lines: import domain function, call for_each_persona.
Business logic lives in ``app.memory.*``, ``app.life.*``.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

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
# Dreams (daily + weekly compression)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_generate_dreams(ctx) -> None:
    from app.memory.dreams import run_daily_dreams

    await run_daily_dreams()


@cron_error_handler()
@prod_only
async def cron_generate_weekly_dreams(ctx) -> None:
    from app.memory.dreams import run_weekly_dreams

    await run_weekly_dreams()


# ---------------------------------------------------------------------------
# Schedules (monthly / weekly / daily plans)
# ---------------------------------------------------------------------------


@cron_error_handler()
@prod_only
async def cron_generate_monthly_plan(ctx) -> None:
    from app.life.schedule import generate_monthly_plan

    await for_each_persona(generate_monthly_plan, label="monthly-plan")


@cron_error_handler()
@prod_only
async def cron_generate_weekly_plan(ctx) -> None:
    from app.life.schedule import generate_weekly_plan

    tomorrow = date.today() + timedelta(days=1)

    async def _gen(persona_id: str) -> None:
        await generate_weekly_plan(persona_id, target_date=tomorrow)

    await for_each_persona(_gen, label="weekly-plan")


@cron_error_handler()
@prod_only
async def cron_generate_daily_plan(ctx) -> None:
    from app.life.schedule import generate_daily_plan

    await for_each_persona(generate_daily_plan, label="daily-plan")


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
    from app.data.queries import list_all_persona_ids
    from app.data.session import get_session
    from app.life.glimpse import run_glimpse

    async with get_session() as s:
        persona_ids = await list_all_persona_ids(s)

    for persona_id in persona_ids:
        try:
            from app.data import queries as Q

            async with get_session() as s:
                state = await Q.find_latest_life_state(s, persona_id)
            if not state or state.activity_type != "browsing":
                continue
            await run_glimpse(persona_id)
        except Exception:
            logger.exception("[%s] Glimpse cron failed", persona_id)
