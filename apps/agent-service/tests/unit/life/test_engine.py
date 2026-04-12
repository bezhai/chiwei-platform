"""Tests for app.life.engine — Life Engine tick and parsing."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.engine import extract_text, parse_tick_response, parse_wake_me_at, tick

CST = timezone(timedelta(hours=8))

MODULE = "app.life.engine"


def _make_row(**kwargs):
    """Create a mock LifeEngineState row."""
    row = MagicMock()
    row.current_state = kwargs.get("current_state", "发呆")
    row.activity_type = kwargs.get("activity_type", "idle")
    row.response_mood = kwargs.get("response_mood", "无聊")
    row.skip_until = kwargs.get("skip_until", None)
    row.created_at = kwargs.get("created_at", datetime.now(CST))
    return row


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------


def test_extract_text_string():
    assert extract_text("hello") == "hello"


def test_extract_text_list():
    content = [{"text": "hello "}, {"text": "world"}]
    assert extract_text(content) == "hello world"


def test_extract_text_none():
    assert extract_text(None) == ""


# ---------------------------------------------------------------------------
# parse_wake_me_at
# ---------------------------------------------------------------------------


def test_parse_wake_me_at_valid():
    now = datetime(2026, 4, 7, 1, 0, tzinfo=CST)
    result = parse_wake_me_at("07:30", now)
    assert result == datetime(2026, 4, 7, 7, 30, tzinfo=CST)


def test_parse_wake_me_at_next_day():
    """23:00 with wake_me_at 07:00 -> next day 07:00."""
    now = datetime(2026, 4, 7, 23, 0, tzinfo=CST)
    result = parse_wake_me_at("07:00", now)
    assert result == datetime(2026, 4, 8, 7, 0, tzinfo=CST)


def test_parse_wake_me_at_null():
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    assert parse_wake_me_at(None, now) is None
    assert parse_wake_me_at("null", now) is None


def test_parse_wake_me_at_invalid():
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    assert parse_wake_me_at("not_time", now) is None


# ---------------------------------------------------------------------------
# parse_tick_response — valid
# ---------------------------------------------------------------------------


def test_parse_tick_response_valid():
    now = datetime(2026, 4, 7, 1, 0, tzinfo=CST)
    prev = {"current_state": "发呆", "activity_type": "idle", "response_mood": "无聊"}
    raw = json.dumps(
        {
            "current_state": "钻被窝了",
            "activity_type": "sleeping",
            "response_mood": "困死了",
            "wake_me_at": "07:30",
        }
    )
    result = parse_tick_response(raw, prev, now)
    assert result["current_state"] == "钻被窝了"
    assert result["activity_type"] == "sleeping"
    assert result["skip_until"] == datetime(2026, 4, 7, 7, 30, tzinfo=CST)


def test_parse_tick_response_browsing():
    now = datetime(2026, 4, 7, 15, 0, tzinfo=CST)
    prev = {"current_state": "发呆", "activity_type": "idle", "response_mood": "无聊"}
    raw = json.dumps(
        {
            "current_state": "刷手机翻群消息",
            "activity_type": "browsing",
            "response_mood": "无聊",
            "wake_me_at": None,
        }
    )
    result = parse_tick_response(raw, prev, now)
    assert result["activity_type"] == "browsing"
    assert result["skip_until"] is None


# ---------------------------------------------------------------------------
# parse_tick_response — malformed: should preserve previous state (bug-fix)
# ---------------------------------------------------------------------------


def test_parse_tick_response_malformed_preserves_prev_state():
    """On parse failure, return previous full state without losing fields."""
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    prev = {
        "current_state": "看书",
        "activity_type": "studying",
        "response_mood": "专注",
    }
    result = parse_tick_response("not json at all", prev, now)
    assert result["current_state"] == "看书"
    assert result["activity_type"] == "studying"
    assert result["response_mood"] == "专注"
    assert result["skip_until"] is None
    assert result["reasoning"] is None


def test_parse_tick_response_empty_json():
    """Empty JSON object uses previous state for missing fields."""
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    prev = {
        "current_state": "看书",
        "activity_type": "studying",
        "response_mood": "专注",
    }
    result = parse_tick_response("{}", prev, now)
    assert result["current_state"] == "看书"
    assert result["activity_type"] == "studying"
    assert result["response_mood"] == "专注"


# ---------------------------------------------------------------------------
# tick — skip_until in the future
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_skips_when_skip_until_future():
    """skip_until in the future -> no LLM call."""
    future = datetime.now(CST) + timedelta(hours=1)
    row = _make_row(skip_until=future)

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch(
            f"{MODULE}.Q.find_latest_life_state",
            new_callable=AsyncMock,
            return_value=row,
        ):
            with patch(f"{MODULE}._think", new_callable=AsyncMock) as mock_think:
                result = await tick("akao-001")
                mock_think.assert_not_called()
                assert result is None


# ---------------------------------------------------------------------------
# tick — no skip, calls think and saves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_calls_think_when_no_skip():
    """No skip -> call LLM think, save state."""
    row = _make_row(skip_until=None)

    new_state = {
        "current_state": "刷手机",
        "activity_type": "browsing",
        "response_mood": "无聊",
        "skip_until": None,
    }

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                f"{MODULE}.Q.find_latest_life_state",
                new_callable=AsyncMock,
                return_value=row,
            ),
            patch(f"{MODULE}.Q.insert_life_state", new_callable=AsyncMock) as mock_save,
            patch(f"{MODULE}._think", new_callable=AsyncMock, return_value=new_state),
        ):
            result = await tick("akao-001")
            mock_save.assert_called_once()
            assert result["activity_type"] == "browsing"


# ---------------------------------------------------------------------------
# tick — no row uses default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_no_row_uses_default():
    """No DB record -> use default state."""
    new_state = {
        "current_state": "睡着了",
        "activity_type": "sleeping",
        "response_mood": "zzz",
        "skip_until": None,
    }

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                f"{MODULE}.Q.find_latest_life_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(f"{MODULE}.Q.insert_life_state", new_callable=AsyncMock) as mock_save,
            patch(f"{MODULE}._think", new_callable=AsyncMock, return_value=new_state),
        ):
            result = await tick("akao-001")
            mock_save.assert_called_once()
            assert result["activity_type"] == "sleeping"


# ---------------------------------------------------------------------------
# tick — dry_run does not save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_dry_run_does_not_save():
    """dry_run=True returns result but does not persist."""
    row = _make_row(skip_until=None)
    new_state = {
        "current_state": "看书",
        "activity_type": "studying",
        "response_mood": "专注",
        "skip_until": None,
    }

    with patch(f"{MODULE}.get_session") as mock_gs:
        mock_session = AsyncMock()
        mock_gs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_gs.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                f"{MODULE}.Q.find_latest_life_state",
                new_callable=AsyncMock,
                return_value=row,
            ),
            patch(f"{MODULE}.Q.insert_life_state", new_callable=AsyncMock) as mock_save,
            patch(f"{MODULE}._think", new_callable=AsyncMock, return_value=new_state),
        ):
            result = await tick("akao-001", dry_run=True)
            mock_save.assert_not_called()
            assert result["activity_type"] == "studying"
