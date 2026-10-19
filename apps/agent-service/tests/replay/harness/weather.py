"""QWeather (the weather world looks up), at the HTTP boundary.

The replay's settings point ``app.capabilities.weather`` at ``weather.replay`` (:data:`SETTINGS`);
the capability (place lookup, coordinate rounding, the three v1 requests, reading the answers)
and world's three weather tools above it run for real.

The host is served on :mod:`tests.replay.harness.network`. Answers are the provider's own JSON,
as the scenario gives it:

* ``GET /geo/v2/city/lookup``: :meth:`Weather.places` for the ``location`` parameter, answered
  as ``{"code": "200", "location": [...]}``.
* ``GET /weather/v1/{current|hourly|daily}/{lat}/{lon}``: :meth:`Weather.current`,
  :meth:`Weather.hourly`, :meth:`Weather.daily` for those coordinates (two decimals, as the
  capability sends them), answered with the body given.

Anything else is refused (the scenic-spot lookup the capability falls back to when no city
matches included: add it here when a scenario needs it). Each request goes onto the effects
timeline with its query parameters::

    {"http": "GET weather/geo/v2/city/lookup",
     "request": {"location": "...", "number": "5", "lang": "zh"}, "status": 200}
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.replay.harness.network import Network, json_response

HOST = "weather.replay"

SETTINGS = {"qweather_api_host": HOST, "qweather_api_key": "replay-key"}

_CITY = "/geo/v2/city/lookup"
_FORECASTS = ("current", "hourly", "daily")


class Weather:
    def __init__(self, network: Network) -> None:
        self._network = network
        self._answers = network.answers("weather")
        network.serve(HOST, self._serve)

    def places(self, name: str, found: list[dict[str, Any]]) -> None:
        """The cities a lookup of ``name`` finds (GeoAPI ``location`` entries)."""
        self._answers.give(("city", name), {"code": "200", "location": list(found)})

    def current(self, lat: str, lon: str, body: dict[str, Any]) -> None:
        self._answers.give(("current", lat, lon), body)

    def hourly(self, lat: str, lon: str, body: dict[str, Any]) -> None:
        self._answers.give(("hourly", lat, lon), body)

    def daily(self, lat: str, lon: str, body: dict[str, Any]) -> None:
        self._answers.give(("daily", lat, lon), body)

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        key: tuple[str, ...] | None = None
        if path == _CITY:
            key = ("city", params.get("location", ""))
        else:
            parts = path.strip("/").split("/")
            if (
                len(parts) == 5
                and parts[:2] == ["weather", "v1"]
                and parts[2] in _FORECASTS
            ):
                key = (parts[2], parts[3], parts[4])
        if request.method != "GET" or key is None:
            self._network.refuse(request, "weather path not served")
        body = self._answers.take(key, request)
        self._network.effects.append(
            {"http": f"GET weather{path}", "request": params, "status": 200}
        )
        return json_response(request, body)
