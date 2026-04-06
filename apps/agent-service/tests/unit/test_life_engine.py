import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.life_engine import LifeEngine, _extract_text

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


def test_extract_text_string():
    assert _extract_text("hello") == "hello"


def test_extract_text_list():
    content = [{"text": "hello "}, {"text": "world"}]
    assert _extract_text(content) == "hello world"


def test_extract_text_none():
    assert _extract_text(None) == ""


def test_parse_tick_response_valid():
    engine = LifeEngine()
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    raw = json.dumps({
        "current_state": "在刷手机",
        "activity_type": "browsing",
        "response_mood": "好奇",
        "skip_minutes": 15,
    })
    result = engine._parse_tick_response(raw, "发呆", "无聊", now)
    assert result["activity_type"] == "browsing"
    assert result["current_state"] == "在刷手机"
    assert result["skip_until"] == now + timedelta(minutes=15)


def test_parse_tick_response_invalid_activity():
    engine = LifeEngine()
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    raw = json.dumps({"current_state": "x", "activity_type": "flying", "response_mood": "y"})
    result = engine._parse_tick_response(raw, "发呆", "无聊", now)
    assert result["activity_type"] == "idle"


def test_parse_tick_response_malformed():
    engine = LifeEngine()
    now = datetime(2026, 4, 7, 10, 0, tzinfo=CST)
    result = engine._parse_tick_response("not json", "发呆", "无聊", now)
    assert result["activity_type"] == "idle"
    assert result["current_state"] == "发呆"


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
    """无 skip → 调用 LLM think"""
    engine = LifeEngine()
    row = _make_row(activity_type="idle", skip_until=None)

    new_state = {
        "current_state": "去刷手机了",
        "activity_type": "browsing",
        "response_mood": "好奇",
        "skip_until": None,
    }

    with (
        patch("app.services.life_engine._load_state", new_callable=AsyncMock, return_value=row),
        patch("app.services.life_engine._save_state", new_callable=AsyncMock) as mock_save,
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state),
        patch("app.services.life_engine.LifeEngine._on_state_change", new_callable=AsyncMock) as mock_change,
    ):
        await engine.tick("akao-001")
        mock_save.assert_called_once()
        mock_change.assert_called_once()


@pytest.mark.asyncio
async def test_tick_expired_skip_triggers_think():
    """skip_until 已过期 → 调用 LLM think"""
    engine = LifeEngine()
    past = datetime.now(CST) - timedelta(minutes=5)
    row = _make_row(activity_type="busy", skip_until=past)

    new_state = {
        "current_state": "无聊了",
        "activity_type": "idle",
        "response_mood": "有点空虚",
        "skip_until": None,
    }

    with (
        patch("app.services.life_engine._load_state", new_callable=AsyncMock, return_value=row),
        patch("app.services.life_engine._save_state", new_callable=AsyncMock) as mock_save,
        patch("app.services.life_engine.LifeEngine._think", new_callable=AsyncMock, return_value=new_state),
        patch("app.services.life_engine.LifeEngine._on_state_change", new_callable=AsyncMock),
    ):
        await engine.tick("akao-001")
        mock_save.assert_called_once()


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
        patch("app.services.life_engine.LifeEngine._on_state_change", new_callable=AsyncMock),
    ):
        await engine.tick("akao-001")
        mock_save.assert_called_once()
