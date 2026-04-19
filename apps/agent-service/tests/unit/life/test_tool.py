"""Test commit_life_state tool — v4 §9.5 hard validations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.tool import commit_life_state_impl

CST = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(CST)


@pytest.mark.asyncio
async def test_validates_nonempty_fields():
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="",
        current_state="walking",
        response_mood="calm",
        state_end_at=_now() + timedelta(minutes=30),
        skip_until=None,
        reasoning=None,
        now=_now(),
        prev_state=None,
    )
    assert r.ok is False
    assert "activity_type" in r.error


@pytest.mark.asyncio
async def test_rejects_past_state_end_at():
    now = _now()
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="transit", current_state="walking",
        response_mood="calm",
        state_end_at=now - timedelta(minutes=1),
        skip_until=None, reasoning=None,
        now=now, prev_state=None,
    )
    assert r.ok is False
    assert "state_end_at" in r.error


@pytest.mark.asyncio
async def test_rejects_skip_until_outside_range():
    now = _now()
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="transit", current_state="walking", response_mood="calm",
        state_end_at=now + timedelta(minutes=30),
        skip_until=now + timedelta(minutes=45),
        reasoning=None, now=now, prev_state=None,
    )
    assert r.ok is False


@pytest.mark.asyncio
async def test_prev_not_expired_only_allows_refresh_not_new_activity():
    now = _now()
    prev = MagicMock(
        activity_type="study", state_end_at=now + timedelta(minutes=30),
    )
    r = await commit_life_state_impl(
        persona_id="chiwei",
        activity_type="transit",
        current_state="walking", response_mood="calm",
        state_end_at=now + timedelta(minutes=30),
        skip_until=None, reasoning=None,
        now=now, prev_state=prev,
    )
    assert r.ok is False
    assert "refresh" in r.error.lower() or "prev" in r.error.lower()


@pytest.mark.asyncio
async def test_prev_not_expired_allows_in_segment_refresh():
    now = _now()
    prev_end = now + timedelta(minutes=30)
    prev = MagicMock(activity_type="study", state_end_at=prev_end)
    with patch("app.life.tool.insert_life_state", new=AsyncMock(return_value=42)) as ins:
        r = await commit_life_state_impl(
            persona_id="chiwei",
            activity_type="study",
            current_state="reading more focused",
            response_mood="calm",
            state_end_at=prev_end,
            skip_until=now + timedelta(minutes=10),
            reasoning=None, now=now, prev_state=prev,
        )
    assert r.ok is True
    assert r.is_refresh is True
    assert r.life_state_id == 42
    ins.assert_awaited_once()


@pytest.mark.asyncio
async def test_prev_expired_allows_new_activity():
    now = _now()
    prev = MagicMock(
        activity_type="study",
        state_end_at=now - timedelta(minutes=5),
    )
    with patch("app.life.tool.insert_life_state", new=AsyncMock(return_value=7)) as ins:
        r = await commit_life_state_impl(
            persona_id="chiwei",
            activity_type="transit",
            current_state="walking home",
            response_mood="calm",
            state_end_at=now + timedelta(minutes=30),
            skip_until=None, reasoning=None,
            now=now, prev_state=prev,
        )
    assert r.ok is True
    assert r.is_refresh is False
    assert r.life_state_id == 7
    ins.assert_awaited_once()


@pytest.mark.asyncio
async def test_prev_no_state_end_at_allows_new_activity():
    """Legacy rows with state_end_at=NULL shouldn't block new activities."""
    now = _now()
    prev = MagicMock(activity_type="study", state_end_at=None)
    with patch("app.life.tool.insert_life_state", new=AsyncMock(return_value=1)) as ins:
        r = await commit_life_state_impl(
            persona_id="chiwei",
            activity_type="transit",
            current_state="walking", response_mood="calm",
            state_end_at=now + timedelta(minutes=30),
            skip_until=None, reasoning=None,
            now=now, prev_state=prev,
        )
    assert r.ok is True
    assert r.is_refresh is False
    ins.assert_awaited_once()
