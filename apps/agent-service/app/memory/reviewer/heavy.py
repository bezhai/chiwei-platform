"""Heavy reviewer — daily global consolidation.

Replaces the previous daily dream pipeline. Runs at 03:00 CST per persona.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.data.queries import (
    list_abstracts_window,
    list_fragments_window,
)
from app.domain.life_state import find_life_state
from app.memory.reviewer.tools import make_reviewer_tools
from app.runtime.db import tx
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_HEAVY_CFG = AgentConfig("memory_reviewer_heavy", "offline-model", "memory-reviewer-heavy")


async def _run_agent(
    *,
    persona_id: str,
    now: datetime,
    fragments_text: str,
    abstracts_text: str,
    life_state_text: str,
) -> None:
    await Agent(_HEAVY_CFG, tools=make_reviewer_tools()).run(
        messages=[Message(role=Role.USER, content="执行重档 review（睡前整理）")],
        prompt_vars={
            "persona_id": persona_id,
            "now": now.isoformat(),
            "day_fragments": fragments_text or "（无）",
            "day_abstracts": abstracts_text or "（无）",
            "current_life_state": life_state_text or "（无）",
        },
        context=AgentContext(persona_id=persona_id),
    )


async def run_heavy_review_for_persona(persona_id: str) -> None:
    now = datetime.now(CST)
    since = now - timedelta(days=1)

    # 她此刻的主观快照（新 LifeState，单条最新）。lane 口径与 world/life
    # 写入端一致：current_deployment_lane() or "prod"。日程已不再生成，重档
    # review 不再读 schedule。
    lane = current_deployment_lane() or "prod"

    async with tx():
        fragments = await list_fragments_window(persona_id=persona_id, since=since)
        abstracts = await list_abstracts_window(persona_id=persona_id, since=since)
    snapshot = await find_life_state(lane=lane, persona_id=persona_id)

    if not fragments and not abstracts and not snapshot:
        logger.info("[%s] heavy review: empty day, skip", persona_id)
        return

    def fmt_frag(f):
        return f"- [{f.id}] {f.content[:200]}"

    def fmt_abs(a):
        return f"- [{a.id} subject={a.subject}] {a.content[:200]}"

    def fmt_life(ls):
        return (
            f"{ls.observed_at} [{ls.activity_type}] "
            f"{ls.current_state[:200]} mood={ls.response_mood}"
        )

    logger.info(
        "[%s] heavy review: %d fragments, %d abstracts, snapshot=%s",
        persona_id,
        len(fragments),
        len(abstracts),
        bool(snapshot),
    )

    await _run_agent(
        persona_id=persona_id,
        now=now,
        fragments_text="\n".join(fmt_frag(f) for f in fragments),
        abstracts_text="\n".join(fmt_abs(a) for a in abstracts),
        life_state_text=fmt_life(snapshot) if snapshot else "",
    )

