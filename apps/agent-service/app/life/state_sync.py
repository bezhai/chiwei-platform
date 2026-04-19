"""State-only refresh — lighter Life Engine replay triggered by schedule changes.

Called by the state_sync arq job after an update_schedule event.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain.tools import tool
from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig
from app.data.queries import find_latest_life_state
from app.data.session import get_session
from app.life.tool import CommitResult, commit_life_state_impl

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_REFRESH_CFG = AgentConfig(
    "life_engine_state_refresh", "offline-model", "life-state-refresh"
)


def _make_commit_tool(
    persona_id: str, now: datetime, prev_state: Any | None, captured: dict
):
    @tool
    async def commit_life_state(
        activity_type: str,
        current_state: str,
        response_mood: str,
        state_end_at: str,
        skip_until: str | None = None,
        reasoning: str | None = None,
    ) -> str:
        """Commit refreshed life state."""
        try:
            end = datetime.fromisoformat(state_end_at)
            skip = datetime.fromisoformat(skip_until) if skip_until else None
        except (TypeError, ValueError) as e:
            return f"时间格式错误：{e}"
        r = await commit_life_state_impl(
            persona_id=persona_id,
            activity_type=activity_type,
            current_state=current_state,
            response_mood=response_mood,
            state_end_at=end,
            skip_until=skip,
            reasoning=reasoning,
            now=now,
            prev_state=prev_state,
        )
        captured["result"] = r
        if not r.ok:
            return f"校验失败：{r.error}"
        return f"已刷新。is_refresh={r.is_refresh}"

    return commit_life_state


async def _run_refresh_agent(
    *,
    persona_id: str,
    prev_state: Any,
    new_schedule_content: str,
    now: datetime,
) -> CommitResult | None:
    """Run the refresh agent; return CommitResult or None (tool not called)."""
    captured: dict = {}
    tool_instance = _make_commit_tool(persona_id, now, prev_state, captured)

    prompt_vars = {
        "prev_activity": prev_state.activity_type or "",
        "prev_current_state": prev_state.current_state,
        "prev_state_end_at": (
            prev_state.state_end_at.isoformat() if prev_state.state_end_at else ""
        ),
        "new_schedule": new_schedule_content,
        "now": now.isoformat(),
    }

    await Agent(_REFRESH_CFG, tools=[tool_instance]).run(
        messages=[HumanMessage(content="按新 schedule 重新评估状态")],
        prompt_vars=prompt_vars,
    )
    return captured.get("result")


async def state_only_refresh(
    *,
    persona_id: str,
    new_schedule_content: str,
    now: datetime | None = None,
) -> CommitResult | None:
    """Re-evaluate current state given a new schedule.

    Returns the CommitResult if the state was refreshed/switched, else None
    (no prev state / LLM decided unchanged / validation failed).
    """
    now = now or datetime.now(CST)

    async with get_session() as s:
        prev = await find_latest_life_state(s, persona_id)
    if prev is None:
        logger.info("[%s] no prev state, skip refresh", persona_id)
        return None

    return await _run_refresh_agent(
        persona_id=persona_id,
        prev_state=prev,
        new_schedule_content=new_schedule_content,
        now=now,
    )
