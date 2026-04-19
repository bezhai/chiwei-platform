"""Life Engine v4 — commit_life_state tool + §9.5 hard validations.

This tool is called by the Life Engine's LLM via a langchain tool binding. It's
NOT exposed to the chat agent — only internal to the life pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.data.queries import insert_life_state
from app.data.session import get_session

logger = logging.getLogger(__name__)


@dataclass
class CommitResult:
    ok: bool
    error: str = ""
    is_refresh: bool = False
    life_state_id: int | None = None


async def commit_life_state_impl(
    *,
    persona_id: str,
    activity_type: str,
    current_state: str,
    response_mood: str,
    state_end_at: datetime,
    skip_until: datetime | None,
    reasoning: str | None,
    now: datetime,
    prev_state: Any | None,
) -> CommitResult:
    # §9.5 Validation 1 — required fields non-empty
    if not activity_type.strip():
        return CommitResult(ok=False, error="activity_type 不能为空")
    if not current_state.strip():
        return CommitResult(ok=False, error="current_state 不能为空")
    if not response_mood.strip():
        return CommitResult(ok=False, error="response_mood 不能为空")

    # §9.5 Validation 2 — state_end_at > now
    if state_end_at <= now:
        return CommitResult(ok=False, error="state_end_at 必须大于 now")

    # §9.5 Validation 3 — skip_until in (now, state_end_at) when set
    if skip_until is not None:
        if not (now < skip_until < state_end_at):
            return CommitResult(
                ok=False,
                error="skip_until 必须满足 now < skip_until < state_end_at",
            )

    # §9.5 Validation 4 — prev relationship
    is_refresh = False
    if prev_state is not None and prev_state.state_end_at is not None:
        if now < prev_state.state_end_at:
            # still inside prev segment — only in-segment refresh allowed
            is_refresh = True
            if activity_type != prev_state.activity_type:
                return CommitResult(
                    ok=False,
                    error=(
                        f"prev state 仍未到期（{prev_state.state_end_at}），"
                        f"只允许段内 refresh，activity_type 必须等于 prev "
                        f"({prev_state.activity_type})"
                    ),
                )
            if state_end_at != prev_state.state_end_at:
                return CommitResult(
                    ok=False,
                    error="prev state 仍未到期，段内 refresh 不允许改 state_end_at",
                )

    # §9.5 Validation 5 — state_end_at is an LLM self-discipline commitment;
    # tool layer does not mechanically enforce it beyond the above checks.

    async with get_session() as s:
        life_state_id = await insert_life_state(
            s,
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            reasoning=reasoning,
            skip_until=skip_until,
            state_end_at=state_end_at,
        )

    return CommitResult(ok=True, is_refresh=is_refresh, life_state_id=life_state_id)
