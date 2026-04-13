"""Wild Agents — four persona-blind agents that generate diverse stimuli.

Each agent imagines "what floated past an 18-year-old girl today" from a
different angle. They do NOT know persona identity, interests, or location.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.life._date_utils import WEEKDAY_CN, get_season

logger = logging.getLogger(__name__)

_WILD_INTERNET_CFG = AgentConfig("wild_agent_internet", "offline-model", "wild-internet")
_WILD_CITY_CFG = AgentConfig("wild_agent_city", "offline-model", "wild-city")
_WILD_RABBITHOLE_CFG = AgentConfig("wild_agent_rabbithole", "offline-model", "wild-rabbithole")
_WILD_MOOD_CFG = AgentConfig("wild_agent_mood", "offline-model", "wild-mood")

_LABELS = ["互联网漫游", "城市观察", "兔子洞", "情绪天气"]


async def _run_one(cfg: AgentConfig, prompt_vars: dict) -> str:
    result = await Agent(cfg).run(
        messages=[HumanMessage(content="开始。")],
        prompt_vars=prompt_vars,
    )
    return extract_text(result.content)


async def run_wild_agents(target_date: date, weather: str = "") -> str:
    """Run 4 wild agents in parallel. Returns combined materials text.

    Wild agents don't know persona — only "18岁中国女生" as the base profile.
    """
    season = get_season(target_date.month)
    weekday = WEEKDAY_CN[target_date.weekday()]
    date_str = target_date.isoformat()

    tasks = [
        _run_one(_WILD_INTERNET_CFG, {"date": date_str, "weekday": weekday, "season": season}),
        _run_one(_WILD_CITY_CFG, {"date": date_str, "season": season, "weather": weather}),
        _run_one(_WILD_RABBITHOLE_CFG, {}),
        _run_one(_WILD_MOOD_CFG, {"date": date_str, "season": season}),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = []
    for label, result in zip(_LABELS, results):
        if isinstance(result, Exception):
            logger.warning("Wild agent '%s' failed: %s", label, result)
            continue
        if result:
            sections.append(f"--- {label} ---\n{result}")

    return "\n\n".join(sections)
