"""Life Engine — every-minute tick that decides what Chiwei is doing.

Each tick: load prev state → LLM decides via commit_life_state tool
(with §9.5 hard validations). Append-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain.tools import tool
from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig
from app.data import queries as Q
from app.data.session import get_session
from app.life.tool import CommitResult, commit_life_state_impl

_LIFE_TICK_CFG = AgentConfig("life_engine_tick", "offline-model", "life-tick")

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

MAX_TICK_ATTEMPTS = 2


# --- Activity context builder (unchanged from prev version) ---
async def _build_activity_context(persona_id: str, now: datetime) -> tuple[str, str]:
    """Aggregate today's activity timeline. Return (duration_text, timeline_text)."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session() as s:
        rows = await Q.find_today_activity_states(s, persona_id, today_start)
    if not rows:
        return "", "(today just started)"

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
        s_str = seg["start"].astimezone(CST).strftime("%H:%M")
        e_str = seg["end"].astimezone(CST).strftime("%H:%M")
        dur = int((seg["end"] - seg["start"]).total_seconds() / 60)
        if dur > 0:
            lines.append(f"{s_str}~{e_str} {seg['type']}（{dur}分钟）：{seg['desc']}")
        else:
            lines.append(f"{s_str} {seg['type']}：{seg['desc']}")
    return duration_text, "\n".join(lines)


# --- Tool factory ---
def _make_commit_tool(
    persona_id: str, now: datetime, prev_state: Any | None, captured: dict
):
    """Build a langchain @tool that the LLM calls to commit its decision.

    `captured` is a mutable dict owned by the caller; the tool writes the result
    under the "result" key. We don't use a module-level singleton because tick
    may run concurrently for different personas.
    """

    @tool
    async def commit_life_state(
        activity_type: str,
        current_state: str,
        response_mood: str,
        state_end_at: str,
        skip_until: str | None = None,
        reasoning: str | None = None,
    ) -> str:
        """Commit your current life state to memory."""
        try:
            end = datetime.fromisoformat(state_end_at)
            skip = datetime.fromisoformat(skip_until) if skip_until else None
        except (TypeError, ValueError) as e:
            return f"时间格式错误：{e}"
        result = await commit_life_state_impl(
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
        captured["result"] = result
        if not result.ok:
            return f"校验失败：{result.error}"
        return f"状态已提交。id={result.life_state_id} is_refresh={result.is_refresh}"

    return commit_life_state


# --- Think (tool-based) ---
async def _think(
    prev_state_row: Any | None,
    now: datetime,
    persona_id: str,
    *,
    tool_miss_feedback: str = "",
) -> CommitResult | None:
    """Call the LLM with the commit_life_state tool bound. Returns CommitResult.

    Returns None if the LLM didn't call the tool.
    """
    from app.memory._persona import load_persona

    pc = await load_persona(persona_id)

    today = now.strftime("%Y-%m-%d")
    async with get_session() as s:
        schedule = await Q.find_plan_for_period(s, "daily", today, today, persona_id)
    schedule_text = schedule.content if schedule else "（今天还没有安排）"

    duration_text, timeline_text = await _build_activity_context(persona_id, now)

    prev_current_state = prev_state_row.current_state if prev_state_row else "（新的一天）"
    prev_activity_type = (
        prev_state_row.activity_type if prev_state_row and prev_state_row.activity_type else ""
    )
    prev_response_mood = prev_state_row.response_mood if prev_state_row else ""
    prev_state_end_at = (
        prev_state_row.state_end_at.isoformat()
        if prev_state_row and prev_state_row.state_end_at
        else ""
    )

    async with get_session() as s:
        today_frags = await Q.list_today_fragments(
            s, persona_id, sources=["afterthought"]
        )
    frag_text = (
        "\n".join(f.content for f in today_frags[-5:])
        if today_frags
        else "（还没跟人聊过）"
    )

    prompt_vars = {
        "persona_name": pc.display_name,
        "persona_lite": pc.persona_lite,
        "current_time": now.strftime("%H:%M"),
        "current_state": prev_current_state,
        "activity_type": prev_activity_type,
        "activity_duration": duration_text,
        "response_mood": prev_response_mood,
        "schedule": schedule_text,
        "activity_timeline": timeline_text,
        "recent_experiences": frag_text,
        "prev_state_end_at": prev_state_end_at,
    }

    content = "更新生活状态"
    if tool_miss_feedback:
        content = f"更新生活状态\n\n{tool_miss_feedback}"

    captured: dict = {}
    tool_instance = _make_commit_tool(persona_id, now, prev_state_row, captured)

    await Agent(_LIFE_TICK_CFG, tools=[tool_instance]).run(
        messages=[HumanMessage(content=content)],
        prompt_vars=prompt_vars,
    )
    return captured.get("result")


# --- Public tick entry ---
async def tick(
    persona_id: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> CommitResult | None:
    """One heartbeat. Returns CommitResult on success, None on tool-miss / skip.

    `dry_run` is currently a no-op at the engine layer — the tool persists on
    its own. Callers can still pass it for compatibility; it's accepted but not
    honoured (historical behavior was dry=don't-write, but tool-based flow has
    no separate persist step).
    `force=True` ignores skip_until.
    """
    async with get_session() as s:
        row = await Q.find_latest_life_state(s, persona_id)

    now = datetime.now(CST)

    if not force and row:
        if row.skip_until and now < row.skip_until:
            return None
        if row.state_end_at and now < row.state_end_at:
            return None

    for attempt in range(MAX_TICK_ATTEMPTS):
        feedback = "" if attempt == 0 else "上一次你没有调用 commit_life_state tool，请务必通过它提交结果。"
        result = await _think(row, now, persona_id, tool_miss_feedback=feedback)
        if result is not None:
            break
        logger.info(
            "[%s] tick: LLM did not call tool (attempt %d)", persona_id, attempt + 1
        )
    else:
        return None

    if not result.ok:
        logger.warning(
            "[%s] tick: commit failed validation: %s", persona_id, result.error
        )
        return result

    logger.info(
        "[%s] tick committed: id=%s is_refresh=%s",
        persona_id, result.life_state_id, result.is_refresh,
    )
    return result
