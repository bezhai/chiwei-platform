"""Tests for app.agent.tools.external_sources — weather / anime / holiday.

Each tool is a deterministic external query: it hits the official API, parses
the response into **structured data** (a dict the framework JSON-serialises to
the agent), and degrades to ``{"ok": False, "reason": "..."}`` on any error.
Success returns ``{"ok": True, ...fields...}``; the reason on failure never
leaks the key or a key-bearing url.

The agent is what turns these structured facts into prose — the tools only
return accurate structured data and never fabricate.

Network is stubbed with ``httpx.MockTransport`` (the project's established
pattern, no extra dep) so the parse path runs under real httpx semantics.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _stub_async_client(handler: Callable[[httpx.Request], httpx.Response]):
    """Patch httpx.AsyncClient so every instance uses a MockTransport handler.

    The tools build their own ``httpx.AsyncClient(...)`` (possibly with a
    ``proxy=`` kwarg). We intercept construction, drop transport-incompatible
    kwargs, and inject the mock transport — this also lets a test assert that
    the proxy kwarg was passed.
    """
    captured: dict[str, object] = {}

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        captured["kwargs"] = dict(kwargs)
        kwargs.pop("proxy", None)
        kwargs.pop("proxies", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    with patch(
        "app.agent.tools.external_sources.httpx.AsyncClient", side_effect=_factory
    ):
        yield captured


def _raising_handler(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _assert_no_key_anywhere(payload: dict, key: str) -> None:
    """No field value (including a ``reason``) ever carries the raw key."""
    for value in payload.values():
        assert key not in str(value), f"key leaked in {payload!r}"


# ===========================================================================
# Weather — query_weather
# ===========================================================================

_QWEATHER_OK = {
    "code": "200",
    "updateTime": "2026-06-08T10:00+08:00",
    "now": {
        "obsTime": "2026-06-08T09:50+08:00",
        "temp": "24",
        "feelsLike": "26",
        "icon": "305",
        "text": "小雨",
        "wind360": "180",
        "windDir": "南风",
        "windScale": "2",
        "windSpeed": "8",
        "humidity": "80",
        "precip": "0.2",
        "pressure": "1004",
        "vis": "16",
    },
}


class TestQueryWeather:
    @pytest.mark.asyncio
    async def test_parses_real_response_into_structured_fields(self):
        from app.agent.tools.external_sources import query_weather

        def handler(req: httpx.Request) -> httpx.Response:
            assert "devapi.qweather.com" in str(req.url)
            return httpx.Response(200, json=_QWEATHER_OK)

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(handler):
            s.qweather_api_key = "secret-key-123"
            result = await query_weather.invoke({})

        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["city"] == "广州"
        assert result["weather"] == "小雨"
        assert str(result["temp"]) == "24"
        assert str(result["feels_like"]) == "26"
        assert str(result["humidity"]) == "80"
        assert result["wind"] == "南风2级"
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_auth_header_carries_key_not_url(self):
        from app.agent.tools.external_sources import query_weather

        seen: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["header"] = req.headers.get("X-QW-Api-Key", "")
            seen["url"] = str(req.url)
            return httpx.Response(200, json=_QWEATHER_OK)

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(handler):
            s.qweather_api_key = "secret-key-123"
            await query_weather.invoke({})

        # Key travels in the header, never in the query string.
        assert seen["header"] == "secret-key-123"
        assert "secret-key-123" not in seen["url"]

    @pytest.mark.asyncio
    async def test_missing_key_returns_ok_false_no_leak(self):
        from app.agent.tools.external_sources import query_weather

        with patch("app.agent.tools.external_sources.settings") as s:
            s.qweather_api_key = None
            result = await query_weather.invoke({})

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["reason"]
        assert "None" not in result["reason"]

    @pytest.mark.asyncio
    async def test_network_failure_returns_ok_false_no_leak(self):
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_raising_handler(httpx.ConnectError("boom"))):
            s.qweather_api_key = "secret-key-123"
            result = await query_weather.invoke({})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_api_error_code_returns_ok_false(self):
        from app.agent.tools.external_sources import query_weather

        def handler(_req: httpx.Request) -> httpx.Response:
            # 403 with QWeather's app-level error code body.
            return httpx.Response(403, json={"code": "403"})

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(handler):
            s.qweather_api_key = "secret-key-123"
            result = await query_weather.invoke({})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_malformed_body_returns_ok_false(self):
        from app.agent.tools.external_sources import query_weather

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json at all")

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(handler):
            s.qweather_api_key = "secret-key-123"
            result = await query_weather.invoke({})

        assert result["ok"] is False


# ===========================================================================
# Anime — query_anime_calendar
# ===========================================================================

# Minimal slice of the real /calendar payload: a Sunday weekday with two items,
# name_cn HTML-escaped to prove unescape happens.
_BANGUMI_OK = [
    {
        "weekday": {"en": "Sun", "cn": "星期日", "ja": "日曜日", "id": 7},
        "items": [
            {"id": 1, "name": "Re:Zero", "name_cn": "Re:Zero &mdash; 第三季"},
            {"id": 2, "name": "Foo", "name_cn": "测试&amp;番剧"},
        ],
    },
    {
        "weekday": {"en": "Mon", "cn": "星期一", "ja": "月曜日", "id": 1},
        "items": [{"id": 3, "name": "Bar", "name_cn": "周一番"}],
    },
]


class TestQueryAnimeCalendar:
    @pytest.mark.asyncio
    async def test_parses_today_weekday_and_unescapes_into_list(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(req: httpx.Request) -> httpx.Response:
            assert "api.bgm.tv/calendar" in str(req.url)
            return httpx.Response(200, json=_BANGUMI_OK)

        # Pin "today" to a Sunday (weekday id 7).
        class _FakeNow:
            @staticmethod
            def isoweekday() -> int:
                return 7

        with patch.object(
            external_sources, "now_cst", return_value=_FakeNow()
        ), _stub_async_client(handler):
            result = await query_anime_calendar.invoke({})

        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["weekday"] == "星期日"
        names = result["anime"]
        assert isinstance(names, list)
        # Picks Sunday's items, not Monday's.
        assert "Re:Zero — 第三季" in names
        assert "周一番" not in names
        # HTML entities are unescaped.
        assert "测试&番剧" in names
        assert all("&mdash;" not in n and "&amp;" not in n for n in names)

    @pytest.mark.asyncio
    async def test_no_anime_today_is_ok_with_empty_list(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            # Sunday block present but no items.
            return httpx.Response(
                200,
                json=[
                    {
                        "weekday": {"cn": "星期日", "id": 7},
                        "items": [],
                    }
                ],
            )

        class _FakeNow:
            @staticmethod
            def isoweekday() -> int:
                return 7

        with patch.object(
            external_sources, "now_cst", return_value=_FakeNow()
        ), _stub_async_client(handler):
            result = await query_anime_calendar.invoke({})

        # No anime today is a successful query (just an empty list), not a failure.
        assert result["ok"] is True
        assert result["anime"] == []

    @pytest.mark.asyncio
    async def test_goes_through_forward_proxy(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_BANGUMI_OK)

        class _FakeNow:
            @staticmethod
            def isoweekday() -> int:
                return 7

        # ``settings`` is a frozen dataclass — replace the whole object rather
        # than mutating a field.
        with patch.object(
            external_sources, "now_cst", return_value=_FakeNow()
        ), patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(handler) as captured:
            s.forward_proxy_url = "http://proxy:8080"
            await query_anime_calendar.invoke({})

        assert captured["kwargs"].get("proxy") == "http://proxy:8080"

    @pytest.mark.asyncio
    async def test_network_failure_returns_ok_false(self):
        from app.agent.tools.external_sources import query_anime_calendar

        with _stub_async_client(_raising_handler(httpx.ConnectError("down"))):
            result = await query_anime_calendar.invoke({})

        assert result["ok"] is False
        assert result["reason"]

    @pytest.mark.asyncio
    async def test_malformed_body_returns_ok_false(self):
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>nope</html>")

        with _stub_async_client(handler):
            result = await query_anime_calendar.invoke({})

        assert result["ok"] is False


# ===========================================================================
# Holiday — query_holiday
# ===========================================================================


class TestQueryHoliday:
    @pytest.mark.asyncio
    async def test_ordinary_weekend(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(req: httpx.Request) -> httpx.Response:
            assert "timor.tech/api/holiday/info/" in str(req.url)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "type": {"type": 1, "name": "周日", "week": 7},
                    "holiday": None,
                },
            )

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-07"
            result = await query_holiday.invoke({})

        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["date"] == "2026-06-07"
        assert result["weekday"] == "周日"
        assert result["kind"] == "周末休息"
        assert result.get("holiday_name") is None

    @pytest.mark.asyncio
    async def test_legal_holiday(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "type": {"type": 2, "name": "周一", "week": 1},
                    "holiday": {
                        "holiday": True,
                        "name": "端午节",
                        "wage": 3,
                        "date": "2026-06-08",
                    },
                },
            )

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-08"
            result = await query_holiday.invoke({})

        assert result["ok"] is True
        assert result["kind"] == "法定节假日"
        assert result["holiday_name"] == "端午节"

    @pytest.mark.asyncio
    async def test_makeup_workday(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "type": {"type": 3, "name": "周六", "week": 6},
                    "holiday": {
                        "holiday": False,
                        "name": "端午节后调休",
                        "after": True,
                        "date": "2026-06-13",
                    },
                },
            )

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-13"
            result = await query_holiday.invoke({})

        assert result["ok"] is True
        assert result["kind"] == "周末调休补班"

    @pytest.mark.asyncio
    async def test_ordinary_workday(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "type": {"type": 0, "name": "周一", "week": 1},
                    "holiday": None,
                },
            )

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-15"
            result = await query_holiday.invoke({})

        assert result["ok"] is True
        assert result["kind"] == "工作日"

    @pytest.mark.asyncio
    async def test_network_failure_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(_raising_handler(httpx.ConnectError("x"))):
            now.return_value.strftime.return_value = "2026-06-08"
            result = await query_holiday.invoke({})

        assert result["ok"] is False
        assert result["reason"]

    @pytest.mark.asyncio
    async def test_api_error_code_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": -1})

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-08"
            result = await query_holiday.invoke({})

        assert result["ok"] is False


# ===========================================================================
# Tool wiring sanity — they are real @tool objects with descriptions
# ===========================================================================


class TestToolDefinitions:
    def test_all_three_are_tools_with_descriptions(self):
        from app.agent.tools.external_sources import (
            query_anime_calendar,
            query_holiday,
            query_weather,
        )

        for t in (query_weather, query_anime_calendar, query_holiday):
            assert t.name
            assert t.definition.description  # docstring → description
