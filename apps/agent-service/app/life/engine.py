"""Life Engine -- every-minute tick that decides what Chiwei is doing.

Each tick: load latest state -> check skip_until -> LLM decides next activity
+ mood -> persist new state row (append-only).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent
from app.data import queries as Q
from app.data.session import get_session

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


async def _build_activity_context(persona_id: str, now: datetime) -> tuple[str, str]:
    """Aggregate today's activity timeline, return (duration_text, timeline_text)."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as s:
        rows = await Q.find_today_activity_states(s, persona_id, today_start)

    if not rows:
        return "", "(today just started)"

    # Merge consecutive segments with the same activity_type
    segments: list[dict] = []
    for row in rows:
        if segments and segments[-1]["type"] == row.activity_type:
            segments[-1]["end"] = row.created_at
        else:
            segments.append(
                {
                    "type": row.activity_type,
                    "start": row.created_at,
                    "end": row.created_at,
                    "desc": row.current_state[:30],
                }
            )

    cur = segments[-1]
    minutes = int((now - cur["start"]).total_seconds() / 60)
    start_cst = cur["start"].astimezone(CST)
    duration_text = (
        f"{cur['type']}（从 {start_cst.strftime('%H:%M')} 开始，{minutes} 分钟了）"
    )

    lines = [
        f"{seg['start'].astimezone(CST).strftime('%H:%M')} {seg['desc']}"
        for seg in segments
    ]
    return duration_text, "\n".join(lines)


# ---------------------------------------------------------------------------
# Wake-me-at parser
# ---------------------------------------------------------------------------


def parse_wake_me_at(value: str | None, now: datetime) -> datetime | None:
    """Parse ``wake_me_at`` ``HH:MM`` into an aware datetime. None if absent."""
    if not value or value == "null":
        return None
    try:
        parts = value.strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        wake = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if wake <= now:
            wake += timedelta(days=1)
        return wake
    except (ValueError, IndexError):
        logger.warning("Invalid wake_me_at: %s", value)
        return None


# ---------------------------------------------------------------------------
# Response text extractor
# ---------------------------------------------------------------------------


def extract_text(content: object) -> str:
    """Extract plain text from an LLM response content (str or list[dict])."""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()


# ---------------------------------------------------------------------------
# Tick response parser -- returns previous state on failure (bug-fix)
# ---------------------------------------------------------------------------


def parse_tick_response(
    raw: str,
    prev_state: dict,
    now: datetime,
) -> dict:
    """Parse the JSON from LLM tick response.

    On failure, return *prev_state* verbatim so no fields are lost
    (the old code would drop ``activity_type`` on fallback).
    """
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return {
                "current_state": data.get("current_state", prev_state["current_state"]),
                "activity_type": data.get(
                    "activity_type", prev_state.get("activity_type", "")
                ),
                "response_mood": data.get("response_mood", prev_state["response_mood"]),
                "reasoning": data.get("reasoning"),
                "skip_until": parse_wake_me_at(data.get("wake_me_at"), now),
            }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("Failed to parse tick response: %s, raw=%s", exc, raw[:200])

    # Bug-fix: return the previous full state instead of a lossy fallback
    return {**prev_state, "reasoning": None, "skip_until": None}


# ---------------------------------------------------------------------------
# Life Engine
# ---------------------------------------------------------------------------


async def tick(persona_id: str, *, dry_run: bool = False) -> dict | None:
    """One heartbeat: check skip -> LLM decision -> persist -> side-effects.

    ``dry_run=True`` calls the LLM but does not write to DB.
    """
    async with get_session() as s:
        row = await Q.find_latest_life_state(s, persona_id)

    now = datetime.now(CST)

    if row:
        prev_state = {
            "current_state": row.current_state,
            "activity_type": row.activity_type or "",
            "response_mood": row.response_mood,
        }
        skip_until = row.skip_until
    else:
        prev_state = {
            "current_state": "（新的一天）",
            "activity_type": "",
            "response_mood": "",
        }
        skip_until = None

    # Skip check (dry_run ignores skip)
    if not dry_run and skip_until and now < skip_until:
        return None

    new = await _think(prev_state, now, persona_id)

    if dry_run:
        return new

    async with get_session() as s:
        await Q.insert_life_state(
            s,
            persona_id=persona_id,
            current_state=new["current_state"],
            activity_type=new["activity_type"],
            response_mood=new["response_mood"],
            skip_until=new["skip_until"],
            reasoning=new.get("reasoning"),
        )

    logger.info(
        "[%s] tick: %s (%s) skip_until=%s",
        persona_id,
        new["activity_type"],
        new["current_state"][:50],
        new["skip_until"],
    )
    return new


async def _think(
    prev_state: dict,
    now: datetime,
    persona_id: str,
) -> dict:
    """Call LLM to decide the next life state."""
    async with get_session() as s:
        persona = await Q.find_persona(s, persona_id)

    persona_name = persona.display_name if persona else persona_id
    persona_lite = persona.persona_lite if persona else ""

    today = now.strftime("%Y-%m-%d")
    async with get_session() as s:
        schedule = await Q.find_plan_for_period(s, "daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天还没有安排）"

    duration_text, timeline_text = await _build_activity_context(persona_id, now)

    async with get_session() as s:
        today_frags = await Q.find_today_fragments(
            s, persona_id, grains=["conversation"]
        )
    frag_text = (
        "\n".join(f.content[:100] for f in today_frags[-5:])
        if today_frags
        else "（还没跟人聊过）"
    )

    result = await Agent("life-tick").run(
        messages=[HumanMessage(content="更新生活状态")],
        prompt_vars={
            "persona_name": persona_name,
            "persona_lite": persona_lite,
            "current_time": now.strftime("%H:%M"),
            "current_state": prev_state["current_state"],
            "activity_duration": duration_text,
            "response_mood": prev_state["response_mood"],
            "schedule": schedule_text,
            "activity_timeline": timeline_text,
            "recent_experiences": frag_text,
        },
    )
    raw = extract_text(result.content)
    return parse_tick_response(raw, prev_state, now)
