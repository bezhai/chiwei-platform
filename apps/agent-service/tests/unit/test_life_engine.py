import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.life_engine import LifeEngine, _extract_text, _parse_wake_me_at

CST = timezone(timedelta(hours=8))


def _make_row(**kwargs):
    """创建模拟 LifeEngineState row"""
    row = MagicMock()
    row.current_state = kwargs.get("current_state", "发呆")
    row.activity_type = kwargs.get("activity_type", "idle")
    row.response_mood = kwargs.get("response_mood", "无聊")
    row.skip_until = kwargs.get("skip_until", None)
    row.updated_at = kwargs.get("updated_at", datetime.now(CST))
    return row


# ── _extract_text ──

def test_extract_text_string():
    assert _extract_text("hello") == "hello"


def test_extract_text_list():
    content = [{"text": "hello "}, {"text": "world"}]
    assert _extract_text(content) == "hello world"


def test_extract_text_none():
    assert _extract_text(None) == ""


# ── _parse_wake_me_at ──

def test_parse_wake_me_at_valid():
    now = datetime(2026, 4, 7, 1, 0, tzinfo=CST)
    result = _parse_wake_me_at("07:30", now)
    assert result == datetime(2026, 4, 7, 7, 30, tzinfo=CST)


def test_parse_wake_me_at_next_day():
    """23:00 填 07:00 → 明天 07:00"""
    now = datetime(2026, 4, 7, 23, 0, tzinfo=CST)
    result = _parse_wake_me_at("07:00", now)
    assert result == datetime(2026, 4, 8, 7, 0, tzinfo=CST)


def test_parse_wake_me_at_null():
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    assert _parse_wake_me_at(None, now) is None
    assert _parse_wake_me_at("null", now) is None


def test_parse_wake_me_at_invalid():
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    assert _parse_wake_me_at("not_time", now) is None


# ── _parse_tick_response ──

def test_parse_tick_response_valid():
    engine = LifeEngine()
    now = datetime(2026, 4, 7, 1, 0, tzinfo=CST)
    raw = json.dumps({
        "current_state": "钻被窝了",
        "activity_type": "sleeping",
        "response_mood": "困死了",
        "wake_me_at": "07:30",
    })
    result = engine._parse_tick_response(raw, "发呆", "无聊", now)
    assert result["current_state"] == "钻被窝了"
    assert result["activity_type"] == "sleeping"
    assert result["skip_until"] == datetime(2026, 4, 7, 7, 30, tzinfo=CST)


def test_parse_tick_response_browsing():
    engine = LifeEngine()
    now = datetime(2026, 4, 7, 15, 0, tzinfo=CST)
    raw = json.dumps({
        "current_state": "刷手机翻群消息",
        "activity_type": "browsing",
        "response_mood": "无聊",
        "wake_me_at": None,
    })
    result = engine._parse_tick_response(raw, "发呆", "无聊", now)
    assert result["activity_type"] == "browsing"
    assert result["skip_until"] is None


def test_parse_tick_response_malformed():
    engine = LifeEngine()
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    result = engine._parse_tick_response("not json", "发呆", "无聊", now)
    assert result["current_state"] == "发呆"
    assert result["skip_until"] is None


# ── tick ──

@pytest.mark.asyncio
async def test_tick_skips_when_skip_until_future():
    """skip_until 在未来 → 不调用 LLM"""
    engine = LifeEngine()
    future = datetime.now(CST) + timedelta(hours=1)
    row = _make_row(skip_until=future)

    with (
        patch("app.services.life_engine._load_state", new_callable=AsyncMock, return_value=row),
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock) as mock_think,
    ):
        await engine.tick("akao-001")
        mock_think.assert_not_called()


@pytest.mark.asyncio
async def test_tick_calls_think_when_no_skip():
    """无 skip → 调用 LLM think，保存状态（glimpse 已移至独立 cron，tick 不再触发）"""
    engine = LifeEngine()
    row = _make_row(skip_until=None)

    new_state = {
        "current_state": "刷手机",
        "activity_type": "browsing",
        "response_mood": "无聊",
        "skip_until": None,
    }

    with (
        patch("app.services.life_engine._load_state", new_callable=AsyncMock, return_value=row),
        patch("app.services.life_engine._save_state", new_callable=AsyncMock) as mock_save,
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state) as mock_think,
    ):
        result = await engine.tick("akao-001")
        mock_think.assert_called_once()
        mock_save.assert_called_once()
        assert result["activity_type"] == "browsing"


@pytest.mark.asyncio
async def test_tick_no_glimpse_when_not_browsing():
    """非 browsing → 不触发 glimpse"""
    engine = LifeEngine()
    row = _make_row(skip_until=None)

    new_state = {
        "current_state": "睡着了",
        "activity_type": "sleeping",
        "response_mood": "zzz",
        "skip_until": datetime.now(CST) + timedelta(hours=6),
    }

    with (
        patch("app.services.life_engine._load_state", new_callable=AsyncMock, return_value=row),
        patch("app.services.life_engine._save_state", new_callable=AsyncMock),
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state),
        patch("app.services.glimpse.run_glimpse", new_callable=AsyncMock) as mock_glimpse,
    ):
        await engine.tick("akao-001")
        mock_glimpse.assert_not_called()


@pytest.mark.asyncio
async def test_tick_no_row_uses_default():
    """DB 无记录 → 使用默认状态"""
    engine = LifeEngine()

    new_state = {
        "current_state": "睡着了",
        "activity_type": "sleeping",
        "response_mood": "zzz",
        "skip_until": None,
    }

    with (
        patch("app.services.life_engine._load_state", new_callable=AsyncMock, return_value=None),
        patch("app.services.life_engine._save_state", new_callable=AsyncMock) as mock_save,
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state),
    ):
        await engine.tick("akao-001")
        mock_save.assert_called_once()
