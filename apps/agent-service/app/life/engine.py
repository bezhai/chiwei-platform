"""Life Engine -- every-minute tick that decides what Chiwei is doing.

Each tick: load latest state -> check skip_until -> LLM decides next activity
+ mood -> reviewer checks plausibility -> persist new state row (append-only).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.data import queries as Q
from app.data.session import get_session

_LIFE_TICK_CFG = AgentConfig("life_engine_tick", "offline-model", "life-tick")
_TICK_REVIEWER_CFG = AgentConfig(
    "life_tick_reviewer", "offline-model", "life-tick-reviewer"
)

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

MAX_TICK_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


async def _build_activity_context(persona_id: str, now: datetime) -> tuple[str, str]:
    """Aggregate today's activity timeline, return (duration_text, timeline_text).

    Every state entry is shown with full content — no truncation.
    Consecutive same-type segments are merged but show time range and duration.
    """
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as s:
        rows = await Q.find_today_activity_states(s, persona_id, today_start)

    if not rows:
        return "", "(today just started)"

    # Merge consecutive segments with the same activity_type
    # Keep: start/end time, and the LAST state (most recent, full content)
    segments: list[dict] = []
    for row in rows:
        if segments and segments[-1]["type"] == row.activity_type:
            segments[-1]["end"] = row.created_at
            segments[-1]["desc"] = row.current_state
        else:
            segments.append(
                {
                    "type": row.activity_type,
                    "start": row.created_at,
                    "end": row.created_at,
                    "desc": row.current_state,
                }
            )

    cur = segments[-1]
    minutes = int((now - cur["start"]).total_seconds() / 60)
    start_cst = cur["start"].astimezone(CST)
    duration_text = (
        f"{cur['type']}（从 {start_cst.strftime('%H:%M')} 开始，{minutes} 分钟了）"
    )

    lines = []
    for seg in segments:
        s = seg["start"].astimezone(CST).strftime("%H:%M")
        e = seg["end"].astimezone(CST).strftime("%H:%M")
        dur = int((seg["end"] - seg["start"]).total_seconds() / 60)
        if dur > 0:
            lines.append(f"{s}~{e} {seg['type']}（{dur}分钟）：{seg['desc']}")
        else:
            lines.append(f"{s} {seg['type']}：{seg['desc']}")
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
# Reviewer
# ---------------------------------------------------------------------------


async def _review_tick(
    tick_output: dict,
    prev_activity: str,
    duration_minutes: int,
    schedule_text: str,
    current_time: str,
) -> str | None:
    """Review tick output for plausibility. Returns feedback string if rejected, None if OK."""
    result = await Agent(_TICK_REVIEWER_CFG).run(
        messages=[HumanMessage(content="审核状态更新")],
        prompt_vars={
            "current_time": current_time,
            "prev_activity": prev_activity,
            "duration_minutes": str(duration_minutes),
            "new_activity": tick_output["activity_type"],
            "new_state": tick_output["current_state"],
            "schedule": schedule_text,
        },
    )
    raw = extract_text(result.content).strip()

    if raw.upper().startswith("PASS"):
        return None
    return raw


# ---------------------------------------------------------------------------
# Life Engine
# ---------------------------------------------------------------------------


async def tick(persona_id: str, *, dry_run: bool = False) -> dict | None:
    """One heartbeat: check skip -> LLM decision -> reviewer -> persist.

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

    new, schedule_text, duration_minutes = await _think(prev_state, now, persona_id)

    # Reviewer loop
    for attempt in range(MAX_TICK_ATTEMPTS):
        feedback = await _review_tick(
            tick_output=new,
            prev_activity=prev_state.get("activity_type", ""),
            duration_minutes=duration_minutes,
            schedule_text=schedule_text,
            current_time=now.strftime("%H:%M"),
        )
        if feedback is None:
            break

        logger.info(
            "[%s] tick reviewer rejected (attempt %d): %s",
            persona_id,
            attempt + 1,
            feedback[:100],
        )
        # Retry with feedback
        new, _, _ = await _think(
            prev_state, now, persona_id,
            reviewer_feedback=feedback,
        )

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
    *,
    reviewer_feedback: str = "",
) -> tuple[dict, str, int]:
    """Call LLM to decide the next life state.

    Returns (parsed_state, schedule_text, duration_minutes).
    """
    from app.memory._persona import load_persona

    pc = await load_persona(persona_id)
    persona_name = pc.display_name
    persona_lite = pc.persona_lite

    today = now.strftime("%Y-%m-%d")
    async with get_session() as s:
        schedule = await Q.find_plan_for_period(s, "daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天还没有安排）"

    duration_text, timeline_text = await _build_activity_context(persona_id, now)

    # Calculate current activity duration for reviewer
    duration_minutes = 0
    if "分钟了" in duration_text:
        try:
            duration_minutes = int(
                duration_text.split("，")[1].replace("分钟了）", "")
            )
        except (IndexError, ValueError):
            pass

    async with get_session() as s:
        today_frags = await Q.find_today_fragments(
            s, persona_id, grains=["conversation"]
        )
    frag_text = (
        "\n".join(f.content for f in today_frags[-5:])
        if today_frags
        else "（还没跟人聊过）"
    )

    prompt_vars = {
        "persona_name": persona_name,
        "persona_lite": persona_lite,
        "current_time": now.strftime("%H:%M"),
        "current_state": prev_state["current_state"],
        "activity_duration": duration_text,
        "response_mood": prev_state["response_mood"],
        "schedule": schedule_text,
        "activity_timeline": timeline_text,
        "recent_experiences": frag_text,
    }

    content = "更新生活状态"
    if reviewer_feedback:
        content = f"更新生活状态\n\n上一次的输出被审核打回了，原因：{reviewer_feedback}\n请重新生成。"

    result = await Agent(_LIFE_TICK_CFG).run(
        messages=[HumanMessage(content=content)],
        prompt_vars=prompt_vars,
    )
    raw = extract_text(result.content)
    return parse_tick_response(raw, prev_state, now), schedule_text, duration_minutes
