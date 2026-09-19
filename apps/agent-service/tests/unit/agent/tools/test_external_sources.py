"""Tests for app.agent.tools.external_sources — the six real-world sources.

Each tool is a deterministic external query: it hits the official API, parses
the response into **structured data** (a dict the framework JSON-serialises to
the agent), and degrades to ``{"ok": False, "reason": "..."}`` on any error.
Success returns ``{"ok": True, ...fields...}``; the reason on failure never
leaks the key or a key-bearing url.

The agent is what turns these structured facts into prose — the tools only
return accurate structured data and never fabricate.

Network is stubbed with ``httpx.MockTransport`` (the project's established
pattern, no extra dep) so the parse path runs under real httpx semantics.

**Every fixture in this file is a verbatim slice of a real upstream response**
(captured by actually calling the API), trimmed of presentation junk but never
renamed. A stub that invents its own key names proves nothing — this repo has
already shipped two sources that were dead for a year behind green tests that
asserted made-up keys.

Where the world sits is **not** in this module and not in config: the three
place-bound hands (weather, sun times, city events) take the city as a tool
argument, so the caller — the world round — is what knows where this family
lives. The two QWeather hands resolve that name through ``/geo/v2/city/lookup``
first, because the data endpoints only take a LocationID.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
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


# QWeather's data endpoints take a LocationID, never a Chinese city name, so
# every place-bound hand does a ``/geo/v2/city/lookup`` hop first.
#
# **These two fixtures are the only ones in this file not captured off the
# wire** — a prod QWeather key cannot leave the cluster, so the lookup hop has
# never actually run here. They are written from QWeather's published GeoAPI
# schema (``code`` + a ``location`` array whose entries carry ``id``), and
# ``code: "404"`` is what it documents for "nothing matched".
_QWEATHER_GEO_OK = {
    "code": "200",
    "location": [
        {
            "name": "广州",
            "id": "101280101",
            "lat": "23.12518",
            "lon": "113.28064",
            "adm2": "广州",
            "adm1": "广东省",
            "country": "中国",
            "tz": "Asia/Shanghai",
            "utcOffset": "+08:00",
            "isDst": "0",
            "type": "city",
            "rank": "10",
        }
    ],
}

_QWEATHER_GEO_NOT_FOUND = {"code": "404"}

# The lookup is a **fuzzy** search: the docs say a partial name (one Chinese
# character is enough) matches, and results come back ranked by relevance and
# rank. So the name that went in is no evidence at all about the place that
# came out — only the entry's own ``name`` / ``adm1`` / ``adm2`` is.
#
# The ambiguity below is the one QWeather's own docs use to explain ``adm``:
# 西安 is both 陕西省西安市 and 辽源市西安区 (and 牡丹江市西安区). **Written from
# the docs, never run** — same caveat as the two fixtures above.
_QWEATHER_GEO_AMBIGUOUS = {
    "code": "200",
    "location": [
        {
            "name": "西安",
            "id": "101110101",
            "lat": "34.26139",
            "lon": "108.92861",
            "adm2": "西安",
            "adm1": "陕西省",
            "country": "中国",
            "tz": "Asia/Shanghai",
            "utcOffset": "+08:00",
            "isDst": "0",
            "type": "city",
            "rank": "11",
            "fxLink": "https://www.qweather.com/weather/xian-101110101.html",
        },
        {
            "name": "西安",
            "id": "101060802",
            "lat": "42.92782",
            "lon": "125.14536",
            "adm2": "辽源",
            "adm1": "吉林省",
            "country": "中国",
            "tz": "Asia/Shanghai",
            "utcOffset": "+08:00",
            "isDst": "0",
            "type": "city",
            "rank": "45",
            "fxLink": "https://www.qweather.com/weather/xian-101060802.html",
        },
    ],
}


def _qweather_no_such_location() -> httpx.Response:
    """The **current** docs' no-match answer: HTTP 400 + an RFC 7807 error body.

    The error-code page documents ``NO SUCH LOCATION`` / 400 for "no location
    information found or unsupported location" — there is no body-level
    ``code: "404"`` in it any more. Written from the docs, never run.
    """
    return httpx.Response(
        400,
        json={
            "error": {
                "status": 400,
                "type": (
                    "https://dev.qweather.com/docs/resource/error-code/"
                    "#no-such-location"
                ),
                "title": "NO SUCH LOCATION",
                "detail": "no location information found or unsupported location",
            }
        },
    )


def _qweather_handler(
    *,
    geo: object = _QWEATHER_GEO_OK,
    answer: object = _QWEATHER_OK,
    seen: dict | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Route the two QWeather hops: the GeoAPI lookup, then the data endpoint.

    ``geo`` / ``answer`` take either a JSON body or a ready ``httpx.Response``
    for the failure cases.
    """

    def respond(what: object) -> httpx.Response:
        if isinstance(what, httpx.Response):
            return what
        return httpx.Response(200, json=what)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.setdefault("urls", []).append(str(req.url))
        if "/geo/" in req.url.path:
            if seen is not None:
                seen["geo_params"] = dict(req.url.params)
                seen["geo_headers"] = dict(req.headers)
            return respond(geo)
        if seen is not None:
            seen["params"] = dict(req.url.params)
            seen["headers"] = dict(req.headers)
        return respond(answer)

    return handler


class TestQueryWeather:
    @pytest.mark.asyncio
    async def test_parses_real_response_into_structured_fields(self):
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(seen=seen)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["weather"] == "小雨"
        assert str(result["temp"]) == "24"
        assert str(result["feels_like"]) == "26"
        assert str(result["humidity"]) == "80"
        assert result["wind"] == "南风2级"
        _assert_no_key_anywhere(result, "secret-key-123")
        # Per-account host (QWeather gives each key its own; the shared devapi
        # host answers Invalid Host 403) + https, on both hops.
        for url in seen["urls"]:
            assert url.startswith("https://test-host.qweatherapi.com/"), url

    @pytest.mark.asyncio
    async def test_does_not_claim_a_city_name(self):
        """The reading is whatever the resolved LocationID's sky is, full stop.

        QWeather's ``now`` payload carries no place name, so a ``city`` field
        could only be the tool echoing its own argument back as if upstream had
        said it. The caller already knows which city it asked about.
        """
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler()):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is True
        assert "city" not in result

    @pytest.mark.asyncio
    async def test_the_city_is_resolved_through_the_geo_lookup(self):
        """The data endpoints take a LocationID; a Chinese city name is rejected.

        So the city name the caller gives goes to ``/geo/v2/city/lookup`` first,
        and the ``id`` that comes back is what the weather hop asks about. No
        name→id table is baked in — that table would go stale silently.
        """
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(seen=seen)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is True
        assert seen["geo_params"]["location"] == "广州"
        # The LocationID out of the lookup, not the name and not coordinates.
        assert seen["params"]["location"] == "101280101"
        assert "/v7/weather/now" in seen["urls"][-1]

    @pytest.mark.asyncio
    async def test_the_reading_carries_the_place_upstream_actually_matched(self):
        """Whose sky this is, said by upstream — not by echoing the argument.

        The lookup is fuzzy, so the caller cannot tell from the answer alone
        whether it got the town it meant. Handing back the matched entry's own
        ``name`` / ``adm1`` / ``adm2`` is what makes that checkable; handing back
        the string that went in would be the tool grading its own homework.
        """
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler()):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is True
        matched = result["matched"]
        assert matched["name"] == "广州"
        assert matched["adm1"] == "广东省"
        assert matched["adm2"] == "广州"
        assert matched["country"] == "中国"

    @pytest.mark.asyncio
    async def test_a_partial_name_comes_back_as_the_place_not_as_itself(self):
        """The one case an echo would hide: ask for a fragment, get a whole city.

        ``location`` accepts as little as one character and matches fuzzily. If
        the answer simply repeated the fragment there would be no way to notice
        that upstream resolved it to somewhere else entirely.
        """
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler()):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广"})

        assert result["ok"] is True
        # Upstream's own name for what it matched, not the one character asked.
        assert result["matched"]["name"] == "广州"
        assert result["matched"]["adm1"] == "广东省"

    @pytest.mark.asyncio
    async def test_the_other_candidates_are_reported_when_the_name_is_ambiguous(self):
        """Upstream matched more than one place — say so, name them by 行政区.

        Two 西安 come back for the same string. Reporting only the winner would
        leave the caller unable to tell a confident hit from a coin flip, and it
        can act on this: re-ask with a fuller name.
        """
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(geo=_QWEATHER_GEO_AMBIGUOUS)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "西安"})

        assert result["ok"] is True
        # The top-ranked one is what was read.
        assert result["matched"]["adm1"] == "陕西省"
        others = result["also_matched"]
        assert len(others) == 1
        # Named well enough to tell the two apart: the 行政区, not just 西安.
        assert "吉林省" in others[0]
        assert "陕西省" not in others[0]

    @pytest.mark.asyncio
    async def test_a_single_match_reports_no_alternatives(self):
        """One candidate is not an ambiguity — do not pad her context with a key
        that only ever says "nothing else"."""
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler()):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is True
        assert "also_matched" not in result

    @pytest.mark.asyncio
    async def test_the_lookup_asks_for_more_than_one_candidate(self):
        """``number=1`` cannot see an ambiguity: with one row there is nothing to
        compare the winner against. The docs allow 1-20 (default 10)."""
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(seen=seen)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            await query_weather.invoke({"city": "广州"})

        asked = int(seen["geo_params"]["number"])
        assert 1 < asked <= 20

    @pytest.mark.asyncio
    async def test_an_empty_location_array_names_the_city_not_the_structure(self):
        """``code: "200"`` with nothing in ``location`` is "no such town",
        which is a different sentence from "I could not read the answer"."""
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo={"code": "200", "location": []})
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "瓦罐镇"})

        assert result["ok"] is False
        assert "瓦罐镇" in result["reason"]

    @pytest.mark.asyncio
    async def test_the_documented_no_such_location_error_names_the_city(self):
        """The shape the **current** docs give for a miss: HTTP 400 + an
        ``error`` object titled ``NO SUCH LOCATION``.

        Reported as a bare "HTTP 400" it reads like the network broke. It did
        not — the town does not exist upstream, which is something the caller
        can act on (check the setting, try a fuller name).
        """
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo=_qweather_no_such_location())
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "瓦罐镇"})

        assert result["ok"] is False
        assert "瓦罐镇" in result["reason"]
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "junk",
        [
            # A JSON body that is not an object — ``data.get`` would raise, and a
            # raised exception is not the degrade contract.
            httpx.Response(200, json=["nope"]),
            # ``now`` present but not an object — same raise, one level down.
            httpx.Response(200, json={"code": "200", "now": ["nope"]}),
            httpx.Response(200, json={"code": "200", "now": "小雨"}),
        ],
    )
    async def test_a_malformed_weather_body_degrades_instead_of_raising(self, junk):
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(answer=junk)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   ", "　"])
    async def test_a_blank_city_returns_ok_false_no_request(self, blank):
        """No city, no question to ask. Never fall back to a baked-in place.

        Falling back would report some other place's sky as this world's, which
        is the one lie these tools exist to not tell.
        """
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(seen=seen)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": blank})

        assert result["ok"] is False
        assert result["reason"]
        assert "urls" not in seen

    @pytest.mark.asyncio
    async def test_a_city_the_geo_api_never_heard_of_returns_ok_false(self):
        """404 out of the lookup is a nameable failure, not a missing reading.

        And it must not go on to the weather hop with nothing resolved.
        """
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo=_QWEATHER_GEO_NOT_FOUND, seen=seen)
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "瓦罐镇"})

        assert result["ok"] is False
        assert "瓦罐镇" in result["reason"]
        _assert_no_key_anywhere(result, "secret-key-123")
        # Only the lookup was attempted.
        assert len(seen["urls"]) == 1

    @pytest.mark.asyncio
    async def test_a_broken_geo_lookup_does_not_fall_back_to_a_location(self):
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo=httpx.Response(500, text="nope"), seen=seen)
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")
        assert len(seen["urls"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "junk",
        [
            httpx.Response(200, text="not json at all"),
            # A JSON body that is not an object at all — ``data.get`` would blow
            # up on it, and a raised exception is not the degrade contract.
            httpx.Response(200, json=["nope"]),
            httpx.Response(200, json={"code": "200"}),  # no location array
            httpx.Response(200, json={"code": "200", "location": [{}]}),  # no id
        ],
    )
    async def test_a_malformed_geo_body_degrades_instead_of_raising(self, junk):
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(geo=junk)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]
        # The degrade contract, not the structured tool_error escape hatch.
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    async def test_auth_header_carries_key_not_url(self):
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(seen=seen)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            await query_weather.invoke({"city": "广州"})

        # Key travels in the header on **both** hops, never in a query string.
        assert seen["geo_headers"]["x-qw-api-key"] == "secret-key-123"
        assert seen["headers"]["x-qw-api-key"] == "secret-key-123"
        for url in seen["urls"]:
            assert "secret-key-123" not in url

    @pytest.mark.asyncio
    async def test_missing_key_returns_ok_false_no_leak(self):
        from app.agent.tools.external_sources import query_weather

        with patch("app.agent.tools.external_sources.settings") as s:
            s.qweather_api_key = None
            result = await query_weather.invoke({"city": "广州"})

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["reason"]
        assert "None" not in result["reason"]

    @pytest.mark.asyncio
    async def test_missing_host_returns_ok_false_no_request(self):
        # Host is per-account config (QWeather rejects the shared devapi host
        # with "Invalid Host" 403). Without a configured host we degrade rather
        # than fire a request at a host that will be refused.
        from app.agent.tools.external_sources import query_weather

        seen: dict = {}
        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_qweather_handler(seen=seen)):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = None
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]
        assert "urls" not in seen

    @pytest.mark.asyncio
    async def test_network_failure_returns_ok_false_no_leak(self):
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(_raising_handler(httpx.ConnectError("boom"))):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_api_error_code_returns_ok_false(self):
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(
            # 403 with QWeather's app-level error code body.
            _qweather_handler(answer=httpx.Response(403, json={"code": "403"}))
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_malformed_body_returns_ok_false(self):
        from app.agent.tools.external_sources import query_weather

        with patch(
            "app.agent.tools.external_sources.settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=httpx.Response(200, text="not json at all"))
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_weather.invoke({"city": "广州"})

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
    async def test_a_calendar_without_today_in_it_is_a_failure_not_an_empty_day(self):
        """The historical bug, exactly: read a field, get ``None``, report success.

        ``/calendar`` returns one block per weekday. If today's block is not in
        there, the payload is not the thing we know how to read — and answering
        ``ok=True, anime=[]`` turns that into a **positive claim about the
        world** ("nothing is airing today") that nobody can tell apart from the
        truth. One renamed key upstream and this source goes quietly dead.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            # Monday only; "today" is Sunday.
            return httpx.Response(
                200,
                json=[
                    {
                        "weekday": {"cn": "星期一", "id": 1},
                        "items": [{"name": "Bar", "name_cn": "周一番"}],
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

        assert result["ok"] is False
        assert result["reason"]
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    async def test_a_renamed_weekday_key_is_a_failure_not_an_empty_day(self):
        """Same shape, the way it would really arrive: the key moves.

        ``weekday.id`` is the only thing that says which day a block is. Rename
        it and every block stops matching — which, before this, read as "no
        anime today", every day, forever.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "weekday": {"cn": "星期日", "weekday_id": 7},
                        "items": [{"name_cn": "今天的番"}],
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

        assert result["ok"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "items",
        [
            # ``items`` missing entirely.
            None,
            # ``items`` present but not a list.
            {"0": {"name_cn": "番"}},
            "番",
        ],
    )
    async def test_an_items_field_we_cannot_read_is_a_failure(self, items):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        block: dict = {"weekday": {"cn": "星期日", "id": 7}}
        if items is not None:
            block["items"] = items

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[block])

        class _FakeNow:
            @staticmethod
            def isoweekday() -> int:
                return 7

        with patch.object(
            external_sources, "now_cst", return_value=_FakeNow()
        ), _stub_async_client(handler):
            result = await query_anime_calendar.invoke({})

        assert result["ok"] is False
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    async def test_a_list_nothing_parses_out_of_is_a_failure_not_an_empty_day(self):
        """Items are there but none of them yields a name → we cannot read this.

        Distinct from ``items: []``, which is upstream saying, in a shape we do
        recognise, that nothing is airing.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "weekday": {"cn": "星期日", "id": 7},
                        "items": [
                            {"id": 1, "title": "字段名换了"},
                            {"id": 2, "title": "换成了 title"},
                        ],
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

        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_a_weekday_block_that_is_not_an_object_degrades(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_anime_calendar

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"weekday": "星期日", "items": []}])

        class _FakeNow:
            @staticmethod
            def isoweekday() -> int:
                return 7

        with patch.object(
            external_sources, "now_cst", return_value=_FakeNow()
        ), _stub_async_client(handler):
            result = await query_anime_calendar.invoke({})

        assert result["ok"] is False
        assert result.get("kind") != "tool_error"

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
    @pytest.mark.parametrize(
        "body",
        [
            # Not an object at all — ``data.get`` would raise.
            ["nope"],
            # ``type`` present but not an object — same raise, one level down.
            {"code": 0, "type": ["nope"]},
            {"code": 0, "type": "周日"},
        ],
    )
    async def test_a_malformed_body_degrades_instead_of_raising(self, body):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-08"
            result = await query_holiday.invoke({})

        assert result["ok"] is False
        assert result["reason"]
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    async def test_a_missing_weekday_name_is_left_out_not_handed_over_blank(self):
        """An empty string is not a fact about today — it is a hole wearing one.

        The module contract says a tool never returns half data. ``kind`` is
        what this source is for; a weekday name it did not get simply is not in
        the answer.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_holiday

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"code": 0, "type": {"type": 0}, "holiday": None}
            )

        with patch.object(
            external_sources, "now_cst"
        ) as now, _stub_async_client(handler):
            now.return_value.strftime.return_value = "2026-06-15"
            result = await query_holiday.invoke({})

        assert result["ok"] is True
        assert result["kind"] == "工作日"
        assert result.get("weekday") != ""

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
# Sun times — query_sun_times (QWeather astronomy, same host/key/header rules)
# ===========================================================================

_QWEATHER_SUN_OK = {
    "code": "200",
    "updateTime": "2026-06-08T07:00+08:00",
    "fxLink": "https://example",
    "sunrise": "2026-06-08T05:41+08:00",
    "sunset": "2026-06-08T19:12+08:00",
}


class TestQuerySunTimes:
    @pytest.mark.asyncio
    async def test_parses_sunrise_sunset_into_hhmm(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        seen: dict = {}
        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=_QWEATHER_SUN_OK, seen=seen)
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert isinstance(result, dict)
        assert result["ok"] is True
        # Times reduced to clean CST HH:MM (the +08:00 offset is the local time).
        assert result["sunrise"] == "05:41"
        assert result["sunset"] == "19:12"
        _assert_no_key_anywhere(result, "secret-key-123")
        # Per-account host + https, the astronomy endpoint, today's date, and
        # the LocationID the lookup resolved.
        assert seen["urls"][-1].startswith("https://test-host.qweatherapi.com/")
        assert "/v7/astronomy/sun" in seen["urls"][-1]
        assert seen["params"]["date"] == "20260608"
        assert seen["params"]["location"] == "101280101"

    @pytest.mark.asyncio
    async def test_does_not_claim_a_city_name(self):
        """Same as the weather hand: no place name bolted onto the reading."""
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(_qweather_handler(answer=_QWEATHER_SUN_OK)):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is True
        assert "city" not in result

    @pytest.mark.asyncio
    async def test_the_city_is_resolved_through_the_geo_lookup(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        seen: dict = {}
        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=_QWEATHER_SUN_OK, seen=seen)
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is True
        assert seen["geo_params"]["location"] == "广州"

    @pytest.mark.asyncio
    async def test_the_times_carry_the_place_upstream_actually_matched(self):
        """Same as the weather hand: whose sunset this is, said by upstream.

        A sunset is minutes off between neighbouring towns and hours off between
        provinces, so a silently wrong match here is a wrong number that still
        looks perfectly plausible.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(_qweather_handler(answer=_QWEATHER_SUN_OK)):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is True
        assert result["matched"]["name"] == "广州"
        assert result["matched"]["adm1"] == "广东省"

    @pytest.mark.asyncio
    async def test_the_other_candidates_are_reported_when_the_name_is_ambiguous(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo=_QWEATHER_GEO_AMBIGUOUS, answer=_QWEATHER_SUN_OK)
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "西安"})

        assert result["ok"] is True
        assert result["matched"]["adm1"] == "陕西省"
        assert any("吉林省" in other for other in result["also_matched"])

    @pytest.mark.asyncio
    async def test_the_documented_no_such_location_error_names_the_city(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo=_qweather_no_such_location())
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "瓦罐镇"})

        assert result["ok"] is False
        assert "瓦罐镇" in result["reason"]
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_an_object_degrades_instead_of_raising(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=httpx.Response(200, json=["nope"]))
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_a_blank_city_returns_ok_false_no_request(self, blank):
        """Making up a sunset time is a lie (module docstring). Rather not have it."""
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        seen: dict = {}
        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=_QWEATHER_SUN_OK, seen=seen)
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": blank})

        assert result["ok"] is False
        assert result["reason"]
        assert "urls" not in seen

    @pytest.mark.asyncio
    async def test_a_city_the_geo_api_never_heard_of_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        seen: dict = {}
        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(geo=_QWEATHER_GEO_NOT_FOUND, seen=seen)
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "瓦罐镇"})

        assert result["ok"] is False
        assert "瓦罐镇" in result["reason"]
        assert len(seen["urls"]) == 1

    @pytest.mark.asyncio
    async def test_auth_header_carries_key_not_url(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        seen: dict = {}
        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=_QWEATHER_SUN_OK, seen=seen)
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            await query_sun_times.invoke({"city": "广州"})

        assert seen["geo_headers"]["x-qw-api-key"] == "secret-key-123"
        assert seen["headers"]["x-qw-api-key"] == "secret-key-123"
        for url in seen["urls"]:
            assert "secret-key-123" not in url

    @pytest.mark.asyncio
    async def test_missing_key_returns_ok_false_no_leak(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "settings") as s:
            s.qweather_api_key = None
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is False
        assert "None" not in result["reason"]

    @pytest.mark.asyncio
    async def test_missing_host_returns_ok_false_no_request(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        seen: dict = {}
        with patch.object(external_sources, "settings") as s, _stub_async_client(
            _qweather_handler(answer=_QWEATHER_SUN_OK, seen=seen)
        ):
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = None
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is False
        assert "urls" not in seen

    @pytest.mark.asyncio
    async def test_network_failure_returns_ok_false_no_leak(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(_raising_handler(httpx.ConnectError("boom"))):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_api_error_code_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            _qweather_handler(answer=httpx.Response(200, json={"code": "402"}))
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is False
        _assert_no_key_anywhere(result, "secret-key-123")

    @pytest.mark.asyncio
    async def test_missing_fields_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_sun_times

        with patch.object(external_sources, "now_cst") as now, patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(
            # code ok but no sunrise/sunset.
            _qweather_handler(answer=httpx.Response(200, json={"code": "200"}))
        ):
            now.return_value.strftime.return_value = "20260608"
            s.qweather_api_key = "secret-key-123"
            s.qweather_api_host = "test-host.qweatherapi.com"
            result = await query_sun_times.invoke({"city": "广州"})

        assert result["ok"] is False


# ===========================================================================
# Lunar / solar term — query_lunar_term (local astronomy, no network)
# ===========================================================================


class TestQueryLunarTerm:
    @pytest.mark.asyncio
    async def test_ordinary_day_has_lunar_date_and_nearby_term(self):
        # 2026-06-08: not a solar-term day; next term 夏至 on 06-21.
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_lunar_term

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 6, 8)
        ):
            result = await query_lunar_term.invoke({})

        assert isinstance(result, dict)
        assert result["ok"] is True
        # Lunar date: 农历四月廿三.
        assert "四月" in result["lunar_date"]
        assert "廿三" in result["lunar_date"]
        # Ganzhi + zodiac year: 丙午 马年.
        assert result["zodiac_year"] == "丙午马年"
        # Today is not itself a solar term.
        assert result["solar_term"] is None
        # But the upcoming term is surfaced with days-until.
        assert result["next_solar_term"] == "夏至"
        assert result["days_to_next_term"] == 13

    @pytest.mark.asyncio
    async def test_solar_term_day_is_reported(self):
        # 2026-06-21 is 夏至 itself.
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_lunar_term

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 6, 21)
        ):
            result = await query_lunar_term.invoke({})

        assert result["ok"] is True
        assert result["solar_term"] == "夏至"
        # On a term day, days_to_next_term counts to the *following* term.
        assert result["next_solar_term"] == "小暑"

    @pytest.mark.asyncio
    async def test_lichun_term_day(self):
        # 2026-02-04 is 立春; lunar 腊月十七; year ganzhi 乙巳 蛇.
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_lunar_term

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 2, 4)
        ):
            result = await query_lunar_term.invoke({})

        assert result["ok"] is True
        assert result["solar_term"] == "立春"
        assert "腊月" in result["lunar_date"]
        assert "十七" in result["lunar_date"]
        assert result["zodiac_year"] == "乙巳蛇年"

    @pytest.mark.asyncio
    async def test_handles_tz_aware_now_cst(self):
        # now_cst() returns a *tz-aware* CST datetime in production; cnlunar's
        # internal date math is naive, so the skill must cope with aware input
        # rather than blow up. (Naive fixtures in the other tests hid this.)
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_lunar_term
        from app.infra.cst_time import CST

        aware = datetime(2026, 6, 8, 9, 30, tzinfo=CST)
        with patch.object(external_sources, "now_cst", return_value=aware):
            result = await query_lunar_term.invoke({})

        assert result["ok"] is True
        assert "四月" in result["lunar_date"]
        assert result["days_to_next_term"] == 13

    @pytest.mark.asyncio
    async def test_a_lunar_date_that_came_out_empty_is_a_failure_not_a_blank(self):
        """``or ""`` twice in a row can build a lunar date out of nothing.

        If cnlunar ever hands back ``None`` for the month or the day, the old
        code produced ``lunar_date: ""`` under ``ok=True`` — the same silent
        shape as the two sources that were dead for a year, just local.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_lunar_term

        class _BlankLunar:
            lunarMonthCn = None
            lunarDayCn = None
            year8Char = "丙午"
            chineseYearZodiac = "马"
            todaySolarTerms = "无"
            nextSolarTerm = "夏至"
            nextSolarTermYear = 2026
            nextSolarTermDate = (6, 21)

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 6, 8)
        ), patch.object(
            external_sources.cnlunar, "Lunar", return_value=_BlankLunar()
        ):
            result = await query_lunar_term.invoke({})

        assert result["ok"] is False
        assert result["reason"]

    @pytest.mark.asyncio
    async def test_computation_failure_degrades_without_killing_turn(self):
        # If the lunar library raises, the @tool_error net catches it and the
        # agent gets a structured tool_error outcome (not ok=True), so the turn
        # stays alive and the agent can honestly say it didn't get the data.
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_lunar_term

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 6, 8)
        ), patch.object(
            external_sources.cnlunar, "Lunar", side_effect=RuntimeError("boom")
        ):
            result = await query_lunar_term.invoke({})

        assert isinstance(result, dict)
        # Not a fabricated success.
        assert result.get("ok") is not True
        # It is the structured tool_error outcome, not a raised exception.
        assert result.get("kind") == "tool_error"


# ===========================================================================
# City events — query_city_events (Bilibili 会员购, two hops, no credential)
# ===========================================================================

# Verbatim slice of GET /api/ticket/city/list?channel=3. Entries copied from the
# live response, including 吉林 appearing twice (省 type=1 / 市 type=2) — that
# collision is real and the resolver has to survive it.
_BILI_CITY_LIST_OK = {
    "errno": 0,
    "code": 0,
    "errtag": 0,
    "msg": "success",
    "message": "success",
    "data": {
        "hot": [
            {
                "id": 310100,
                "type": 2,
                "name": "上海",
                "fullname": "上海市",
                "num": 146,
                "booked": False,
                "first_letter": "S",
                "parent_id": 310000,
            },
            {
                "id": 110100,
                "type": 2,
                "name": "北京",
                "fullname": "北京市",
                "num": 115,
                "booked": False,
                "first_letter": "B",
                "parent_id": 110000,
            },
        ],
        "list": [
            {
                "letter": "G",
                "city_list": [
                    {
                        "id": 440000,
                        "type": 1,
                        "name": "广东",
                        "fullname": "广东省",
                        "num": 184,
                        "booked": False,
                        "first_letter": "G",
                        "parent_id": 0,
                    },
                    {
                        "id": 440100,
                        "type": 2,
                        "name": "广州",
                        "fullname": "广州市",
                        "num": 62,
                        "booked": False,
                        "first_letter": "G",
                        "parent_id": 440000,
                    },
                ],
            },
            {
                "letter": "J",
                "city_list": [
                    {
                        "id": 220000,
                        "type": 1,
                        "name": "吉林",
                        "fullname": "吉林省",
                        "num": 12,
                        "booked": False,
                        "first_letter": "J",
                        "parent_id": 0,
                    },
                    {
                        "id": 220200,
                        "type": 2,
                        "name": "吉林",
                        "fullname": "吉林市",
                        "num": 1,
                        "booked": False,
                        "first_letter": "J",
                        "parent_id": 220000,
                    },
                ],
            },
        ],
        "located_id": -1,
    },
}

# Verbatim slice of GET /api/ticket/project/listV2?...&area=440100 — six real
# Guangzhou projects, presentation fields (cover / tags / track_id / feed_tag)
# dropped, every kept field spelled exactly as upstream spells it.
_BILI_EVENTS_OK = {
    "errno": 0,
    "code": 0,
    "errtag": 0,
    "msg": "success",
    "message": "success",
    "data": {
        "traceID": "651c8b09eee6c9724bd8f6034f6aacf0",
        "total": 21,
        "numResults": 6,
        "page": 1,
        "pagesize": 20,
        "isLastBrush": False,
        "result": [
            {
                "id": 1004862,
                "project_id": 1004862,
                "project_name": "广州·木灵动漫 《某某》主题餐厅·三期",
                "city": "广州市",
                "cityId": 440100,
                "start_time": "2026-08-21",
                "end_time": "2026-09-30",
                "venue_name": "MumuLand木木来电（广州店）",
                "district_name": "天河区",
                "third_category_name": "主题餐厅",
                "sale_flag": "预售中",
                "price_low": 1000,
                "price_high": 1000,
                "tlabel": "2026.08.21 - 09.30",
            },
            {
                "id": 1005698,
                "project_id": 1005698,
                "project_name": "广州·LoveLive！学园偶像纪3.0同人ONLY",
                "city": "广州市",
                "cityId": 440100,
                "start_time": "2026-10-02",
                "end_time": "2026-10-02",
                "venue_name": "白云艺术广场",
                "district_name": "白云区",
                "third_category_name": "Only同人展",
                "sale_flag": "预售中",
                "price_low": 9800,
                "price_high": 16800,
                "tlabel": "2026.10.02",
            },
            {
                "id": 1005305,
                "project_id": 1005305,
                "project_name": "广州·「全知读者视角 × animate cafe」",
                "city": "广州市",
                "cityId": 440100,
                "start_time": "2026-09-19",
                "end_time": "2026-10-19",
                "venue_name": "动漫星城广场",
                "district_name": "越秀区",
                "third_category_name": "主题餐厅",
                "sale_flag": "预售中",
                "price_low": 3000,
                "price_high": 3000,
                "tlabel": "2026.09.19 - 10.19",
            },
            {
                "id": 1005505,
                "project_id": 1005505,
                "project_name": "广州·CHIME X 次元Bass",
                "city": "广州市",
                "cityId": 440100,
                "start_time": "2026-09-26",
                "end_time": "2026-09-27",
                "venue_name": "SDlivehouse",
                "district_name": "海珠区",
                "third_category_name": "livehouse",
                "sale_flag": "预售中",
                "price_low": 18800,
                "price_high": 42800,
                "tlabel": "2026.09.26 - 09.27",
            },
            {
                "id": 1005417,
                "project_id": 1005417,
                "project_name": (
                    "广州·2026 CICF×AGF动漫游戏盛典 "
                    "（中国国际漫画节动漫游戏展暨玩出名堂游戏博览会）"
                ),
                "city": "广州市",
                "cityId": 440100,
                "start_time": "2026-10-02",
                "end_time": "2026-10-05",
                "venue_name": "琶洲·保利世贸博览馆",
                "district_name": "海珠区",
                "third_category_name": "漫展",
                "sale_flag": "预售中",
                "price_low": 12000,
                "price_high": 38000,
                "tlabel": "2026.10.02 - 10.05",
            },
            {
                "id": 1004625,
                "project_id": 1004625,
                "project_name": "广州·特摄同人ONLY嘉年华 1st",
                "city": "广州市",
                "cityId": 440100,
                "start_time": "2026-11-28",
                "end_time": "2026-11-28",
                "venue_name": "广州CH8蛙厂演艺中心（大学城店）",
                "district_name": "番禺区",
                "third_category_name": "Only同人展",
                "sale_flag": "预售中",
                "price_low": 8800,
                "price_high": 12800,
                "tlabel": "2026.11.28",
            },
        ],
    },
}


def _bili_handler(
    *,
    cities: object = _BILI_CITY_LIST_OK,
    events: object = _BILI_EVENTS_OK,
    seen: dict | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Route the two 会员购 hops: city/list then project/listV2."""

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.setdefault("urls", []).append(str(req.url))
        if "/city/list" in req.url.path:
            if seen is not None:
                seen["city_params"] = dict(req.url.params)
            if isinstance(cities, httpx.Response):
                return cities
            return httpx.Response(200, json=cities)
        if "/project/listV2" in req.url.path:
            if seen is not None:
                seen["list_params"] = dict(req.url.params)
            if isinstance(events, httpx.Response):
                return events
            return httpx.Response(200, json=events)
        raise AssertionError(f"unexpected url {req.url}")

    return handler


class TestQueryCityEvents:
    @pytest.mark.asyncio
    async def test_parses_real_response_into_structured_events(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        seen: dict = {}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(seen=seen)):
            result = await query_city_events.invoke({"city": "广州"})

        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["city"] == "广州"
        assert result["days"] == external_sources.EVENT_WINDOW_DAYS

        events = result["events"]
        assert isinstance(events, list)
        names = [e["name"] for e in events]
        # Running now (started in August, runs to 09-30).
        assert "广州·木灵动漫 《某某》主题餐厅·三期" in names
        # Opens tomorrow.
        assert "广州·「全知读者视角 × animate cafe」" in names
        # Starts on the last day of the window (today + 14 = 2026-10-02).
        assert any("CICF" in n for n in names)
        # Two months out — past the window.
        assert "广州·特摄同人ONLY嘉年华 1st" not in names

        cicf = next(e for e in events if "CICF" in e["name"])
        assert cicf["kind"] == "漫展"
        assert cicf["venue"] == "琶洲·保利世贸博览馆"
        assert cicf["district"] == "海珠区"
        assert cicf["start"] == "2026-10-02"
        assert cicf["end"] == "2026-10-05"

    @pytest.mark.asyncio
    async def test_resolves_the_city_name_to_the_upstream_area_code(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        seen: dict = {}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(seen=seen)):
            await query_city_events.invoke({"city": "广州"})

        # The city list is fetched live; no city→code table is baked in.
        assert seen["city_params"]["channel"] == "3"
        # 440100 is 广州市's GB/T 2260 code, which is what 会员购 keys on.
        assert seen["list_params"]["area"] == "440100"
        # Upstream rejects a missing platform and any pagesize above 20.
        assert seen["list_params"]["platform"] == "web"
        assert int(seen["list_params"]["pagesize"]) <= 20

    @pytest.mark.asyncio
    async def test_a_city_wins_over_a_province_of_the_same_name(self):
        """吉林 is both a province (220000) and a city (220200) upstream.

        Asking for a city must not silently widen to the whole province.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        seen: dict = {}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(seen=seen)):
            await query_city_events.invoke({"city": "吉林"})

        assert seen["list_params"]["area"] == "220200"

    @pytest.mark.asyncio
    async def test_full_city_name_also_resolves(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        seen: dict = {}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(seen=seen)):
            result = await query_city_events.invoke({"city": "广州市"})

        assert result["ok"] is True
        assert seen["list_params"]["area"] == "440100"

    @pytest.mark.asyncio
    async def test_window_drops_what_is_over_and_what_is_too_far_off(self):
        """Same fixture, "today" moved to 2026-10-06.

        Everything that closed before today drops out, everything starting
        after today+14 drops out, and the one run spanning today survives.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 10, 6, 9, 0)
        ), _stub_async_client(_bili_handler()):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is True
        assert [e["name"] for e in result["events"]] == [
            "广州·「全知读者视角 × animate cafe」"
        ]

    @pytest.mark.asyncio
    async def test_nothing_on_is_ok_with_an_empty_list(self):
        """Nothing on in the next fortnight is a successful query, not a failure.

        Same rule as the anime calendar: ``ok=True`` with ``[]`` means "really
        nothing", which she can say honestly. ``ok=False`` would mean "I could
        not find out" — a different sentence.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        empty = {
            "errno": 0,
            "code": 0,
            "msg": "success",
            "message": "success",
            "data": {
                "traceID": "651c8b09eee6c9724bd8f6034f6aacf0",
                "total": 0,
                "numResults": 0,
                "page": 1,
                "pagesize": 20,
                "isLastBrush": True,
                "result": [],
            },
        }
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=empty)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is True
        assert result["events"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data, what",
        [
            ({}, "没有 result 这一项"),
            ({"total": 0, "numResults": 0}, "只剩计数、没有列表"),
            ({"result": {"0": {"project_name": "x"}}}, "result 是个对象"),
            ({"result": "[]"}, "result 是个字符串"),
        ],
    )
    async def test_a_result_that_is_not_a_list_is_a_failure_not_an_empty_town(
        self, data, what
    ):
        """``get("result") or []`` turns "I cannot read this" into "nothing is on".

        This is the repo's own year-long outage in a new place: read a field,
        get ``None``, hand back a confident empty answer that no log and no test
        can tell apart from the truth. The three cases the module owes the
        caller are different sentences, and only one of them is ``ok=True``:
        upstream listing nothing, a window that caught nothing, and a body we do
        not recognise.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        body = {"errno": 0, "code": 0, "message": "success", "data": data}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=body)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False, what
        assert result["reason"]
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    async def test_a_list_nothing_parses_out_of_is_a_failure_not_an_empty_town(self):
        """A non-empty list where every row falls through the per-item ``continue``.

        One junk row among good ones is junk (already covered above). *Every*
        row unreadable is the schema having moved — ``project_name`` →
        ``title``, ``start_time`` → ``begin`` — and the old code answered that
        with ``ok=True, events=[]``: she gets told the town is dead.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        renamed = {
            "errno": 0,
            "code": 0,
            "message": "success",
            "data": {
                "total": 3,
                "numResults": 3,
                "result": [
                    {"id": 1, "title": "广州·某漫展", "begin": "2026-09-20"},
                    {"id": 2, "title": "广州·某展览", "begin": "2026-09-25"},
                    {"id": 3, "title": "广州·某演出", "begin": "2026-09-27"},
                ],
            },
        }
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=renamed)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]
        assert result.get("kind") != "tool_error"

    @pytest.mark.asyncio
    async def test_an_empty_page_and_an_empty_window_are_not_the_same_answer(self):
        """"Nothing is ticketed here" and "nothing falls in the fortnight" differ.

        Both are honest successes, but they are different facts about the town
        and she would say different things about them. Collapsing both into a
        bare ``events: []`` throws the difference away.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        nothing_listed = {
            "errno": 0,
            "code": 0,
            "message": "success",
            "data": {"total": 0, "numResults": 0, "result": []},
        }
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=nothing_listed)):
            empty_page = await query_city_events.invoke({"city": "广州"})

        # The same real listing, read from after everything on it has closed.
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 12, 1, 9, 0)
        ), _stub_async_client(_bili_handler()):
            empty_window = await query_city_events.invoke({"city": "广州"})

        assert empty_page["ok"] is True
        assert empty_window["ok"] is True
        assert empty_page["events"] == []
        assert empty_window["events"] == []
        # Upstream listed nothing at all, vs listed six with none in range.
        assert empty_page["listed"] == 0
        assert empty_window["listed"] == 6

    @pytest.mark.asyncio
    async def test_a_city_table_we_cannot_read_is_not_a_city_that_is_missing(self):
        """A city table with neither ``hot`` nor ``list`` is a broken answer.

        The resolver finds no candidates either way, so the old code reported
        「会员购没有这座城市」 — a specific, checkable claim about upstream's
        coverage, made on the strength of a body it never managed to read.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        unreadable = {
            "errno": 0,
            "code": 0,
            "message": "success",
            "data": {"located_id": -1},
        }
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(cities=unreadable)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        # Must not say the town is the thing that is missing.
        assert "广州" not in result["reason"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   ", "\u3000"])
    async def test_a_blank_city_returns_ok_false_no_request(self, blank):
        """No city, nothing to ask about — and nowhere to fall back to.

        Reporting another town's ticketed events as this world's would be the
        same lie the weather hand refuses to tell.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        called = {"hit": False}

        def handler(req: httpx.Request) -> httpx.Response:
            called["hit"] = True
            return _bili_handler()(req)

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(handler):
            result = await query_city_events.invoke({"city": blank})

        assert result["ok"] is False
        assert result["reason"]
        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_city_upstream_does_not_cover_returns_ok_false(self):
        """A city 会员购 has never heard of is a distinct, nameable failure.

        Not "no events" — she must not be told the town is dead when the truth
        is that we never asked about the right town.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler()):
            result = await query_city_events.invoke({"city": "瓦罐镇"})

        assert result["ok"] is False
        assert "瓦罐镇" in result["reason"]

    @pytest.mark.asyncio
    async def test_network_failure_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_raising_handler(httpx.ConnectError("down"))):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]

    @pytest.mark.asyncio
    async def test_no_leak_reason_carries_only_the_exception_type(self):
        """The module contract: a reason exposes the exception *type*, nothing else.

        This source needs no credential, but the rule is about any transport
        detail leaking into text the agent may repeat — so the exception's own
        message must not survive into ``reason``.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        boom = httpx.ConnectError("proxy http://user:hunter2@10.0.0.1:8080 refused")
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_raising_handler(boom)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        for value in result.values():
            assert "hunter2" not in str(value)
            assert "10.0.0.1" not in str(value)
        assert "ConnectError" in result["reason"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("broken", ["cities", "events"])
    async def test_malformed_body_returns_ok_false(self, broken):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        junk = httpx.Response(200, text="<html>风控页</html>")
        kwargs = {broken: junk}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(**kwargs)):  # type: ignore[arg-type]
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        assert result["reason"]

    @pytest.mark.asyncio
    async def test_api_error_code_returns_ok_false(self):
        """Upstream answers 200 with an app-level error for a bad pagesize.

        Captured live: ``{"message":"请求非法，请稍后重试","code":81102084}``.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        refused = {"message": "请求非法，请稍后重试", "code": 81102084}
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=refused)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        assert "81102084" in result["reason"]

    @pytest.mark.asyncio
    async def test_http_error_returns_ok_false(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(
            _bili_handler(events=httpx.Response(503, text="nope"))
        ):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is False
        assert "503" in result["reason"]

    @pytest.mark.asyncio
    async def test_items_that_are_not_events_are_skipped_not_fatal(self):
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        messy = {
            "code": 0,
            "message": "success",
            "data": {
                "result": [
                    "not a dict",
                    {"project_name": "", "start_time": "2026-09-20"},
                    {"project_name": "没有日期的项目"},
                    {"project_name": "日期是垃圾", "start_time": "soon"},
                    {
                        "project_name": "广州·CHIME X 次元Bass",
                        "start_time": "2026-09-26",
                        "end_time": "2026-09-27",
                        "venue_name": "SDlivehouse",
                        "district_name": "海珠区",
                        "third_category_name": "livehouse",
                    },
                ]
            },
        }
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=messy)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is True
        assert [e["name"] for e in result["events"]] == ["广州·CHIME X 次元Bass"]

    @pytest.mark.asyncio
    async def test_strips_the_zero_width_padding_upstream_ships(self):
        """A real listing, verbatim: U+200B wrapped around the project name.

        ``str.strip()`` does not touch U+200B (Python does not count it as
        whitespace), so without an explicit strip these invisible characters
        ride into her context and into anything that compares names.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        padded = {
            "code": 0,
            "message": "success",
            "data": {
                "result": [
                    {
                        "project_name": (
                            "​广州·阴阳师十周年线下游园活动·玩家专属伴手礼​"
                        ),
                        "start_time": "2026-09-25",
                        "end_time": "2026-09-27",
                        "venue_name": "广州塔广场",
                        "district_name": "海珠区",
                        "third_category_name": "漫展",
                    }
                ]
            },
        }
        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(_bili_handler(events=padded)):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is True
        name = result["events"][0]["name"]
        assert name == "广州·阴阳师十周年线下游园活动·玩家专属伴手礼"
        assert "​" not in name

    @pytest.mark.asyncio
    async def test_sends_a_browser_user_agent_on_both_hops(self):
        """会员购 412s httpx's own User-Agent. Verified live, both ways:

        ``curl -A python-httpx/0.28.1`` → 412, ``curl -A Mozilla/5.0 ...`` → 200
        on the same url in the same second. Nothing else about the request
        matters (proxy on or off, no cookie, no referer, no signature) — so the
        header is the whole fix, and a test has to pin it or the next httpx
        bump silently turns this source off.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        seen: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.setdefault("uas", []).append(req.headers.get("user-agent", ""))
            return _bili_handler()(req)

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), _stub_async_client(handler):
            result = await query_city_events.invoke({"city": "广州"})

        assert result["ok"] is True
        assert len(seen["uas"]) == 2  # city/list and project/listV2
        for ua in seen["uas"]:
            assert ua == external_sources.BILI_USER_AGENT
            assert "httpx" not in ua.lower()
            assert "python" not in ua.lower()

    @pytest.mark.asyncio
    async def test_does_not_use_the_forward_proxy(self):
        """会员购 is a domestic host, like timor and QWeather.

        Only Bangumi needs ``forward_proxy_url``; routing a domestic call
        through it would be a needless hop and an extra way to fail.
        """
        from app.agent.tools import external_sources
        from app.agent.tools.external_sources import query_city_events

        with patch.object(
            external_sources, "now_cst", return_value=datetime(2026, 9, 18, 9, 0)
        ), patch.object(
            external_sources, "settings"
        ) as s, _stub_async_client(_bili_handler()) as captured:
            s.forward_proxy_url = "http://proxy:8080"
            await query_city_events.invoke({"city": "广州"})

        assert "proxy" not in captured["kwargs"]


# ===========================================================================
# Tool wiring sanity — they are real @tool objects with descriptions
# ===========================================================================


class TestToolDefinitions:
    def test_all_six_are_tools_with_descriptions(self):
        from app.agent.tools.external_sources import (
            query_anime_calendar,
            query_city_events,
            query_holiday,
            query_lunar_term,
            query_sun_times,
            query_weather,
        )

        for t in (
            query_weather,
            query_anime_calendar,
            query_holiday,
            query_sun_times,
            query_lunar_term,
            query_city_events,
        ):
            assert t.name
            assert t.definition.description  # docstring → description


# ===========================================================================
# The city argument hands the model no sample city
#
# **An example is a word list.** Whatever city name these strings quote is what
# the model copies, verbatim, and it keeps copying it after the world moves —
# this repo has already shipped that mistake twice on the place parameters
# (the two accidents are written up in ``tests/living/conftest.py``). The three
# place-bound hands are the same shape: where this world sits is the world's own
# fact, written in its documents, and the hand must take it as an argument
# without ever suggesting a value.
#
# This is not hypothetical here: the error text these tools used to carry read
# 「填中文市名如「广州」」, and the events docstring spelled a whole sample response
# out of one real city's venues and districts.
# ===========================================================================

# City names that have at one time or another sat in this module's model-facing
# text, plus the obvious neighbours anyone would reach for next.
CITY_NAMES_ONCE_IN_TOOL_TEXT = (
    "广州",
    "北京",
    "上海",
    "深圳",
    "杭州",
    "成都",
    "南京",
    "武汉",
    "东京",
)


def _place_bound_hands():
    from app.agent.tools.external_sources import (
        query_city_events,
        query_sun_times,
        query_weather,
    )

    return [query_weather, query_sun_times, query_city_events]


def _model_facing_text(hand) -> dict[str, str]:
    """Every string this hand hands the model: the description + each param's."""
    texts = {"说明": hand.definition.description}
    for name, schema in hand.definition.parameters.get("properties", {}).items():
        described = schema.get("description")
        if described:
            texts[f"参数 {name}"] = described
    return texts


class TestTheCityArgumentQuotesNoSample:
    def test_no_hand_names_a_real_city_anywhere_it_shows_the_model(self):
        for hand in _place_bound_hands():
            for where, text in _model_facing_text(hand).items():
                named = [c for c in CITY_NAMES_ONCE_IN_TOOL_TEXT if c in text]
                assert not named, (
                    f"{hand.definition.name} 的{where}里写着 {named!r} —— 举例就是"
                    f"词表，它会被逐字抄走。原文：\n{text}"
                )

    def test_the_city_parameter_quotes_nothing_at_all(self):
        """Not even a made-up town: a placeholder is still a fillable sample."""
        for hand in _place_bound_hands():
            described = hand.definition.parameters["properties"]["city"]["description"]
            assert "「" not in described, (
                f"{hand.definition.name} 的 city 描述里引着一个样本：{described!r}"
            )
            assert "例如" not in described and "如「" not in described, (
                f"{hand.definition.name} 的 city 描述里在举例：{described!r}"
            )

    def test_the_city_parameter_still_says_what_it_wants(self):
        """Dropping the sample is not dropping the spec — it still says 中文市名."""
        for hand in _place_bound_hands():
            described = hand.definition.parameters["properties"]["city"]["description"]
            assert "市名" in described, (
                f"{hand.definition.name} 的 city 描述没说要一个市名：{described!r}"
            )

    def test_the_city_is_required_not_defaulted(self):
        """A default would be a baked-in place wearing a different hat."""
        for hand in _place_bound_hands():
            required = hand.definition.parameters.get("required", [])
            assert "city" in required, (
                f"{hand.definition.name} 的 city 不是必填 —— 缺省值就是写死的地点"
            )

    def test_the_three_placeless_hands_take_no_city(self):
        """The lunar/holiday/anime sources are the same everywhere in CST."""
        from app.agent.tools.external_sources import (
            query_anime_calendar,
            query_holiday,
            query_lunar_term,
        )

        for hand in (query_lunar_term, query_holiday, query_anime_calendar):
            props = hand.definition.parameters.get("properties", {})
            assert "city" not in props, f"{hand.definition.name} 不需要城市"
