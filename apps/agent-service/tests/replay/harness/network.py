"""Every HTTP request a replayed process makes, at the transport, and what answers it.

Interception points: ``httpx.AsyncHTTPTransport.handle_async_request`` (every async client the code
builds: ``HTTPClient``, ``image_client``, the OpenAI SDK the image model goes through) and
``httpx.HTTPTransport.handle_request`` (sync clients). They are the last step before a request
goes out on the network, so everything above runs for real: lane and auth headers, retries, the
capabilities' request building and response parsing, the tools' handling of what comes back.

Each served host has a handler, registered by the boundary that serves it (:mod:`objects`,
:mod:`web`, :mod:`image_model`, :mod:`sandbox`, :mod:`weather`). **A request to any other host
never leaves the process**: it is refused. So is a request to a served host that the scenario gave
no answer for. Refusing records the request and raises :class:`UnservedRequest`, a
``BaseException``: the code's own ``except Exception`` (``tool_error`` around every tool,
``search_web``'s handler, ``image_client``'s envelope) cannot turn a missing answer into a quiet
failure. Something above can still swallow a ``BaseException`` (messaging failing a delivery,
``gather(return_exceptions=True)`` in a tick), so :meth:`Replay.check
<tests.replay.harness.replay.Replay.check>` fails on every refused request, before it compares or
records anything.

One request is not refused: the lane router's registry poll (``inner_shared.lane_router``, a
daemon thread that polls every 30 seconds from the moment ``app.infra.lane`` is imported). It
gets ``httpx.ConnectError``, which is what it gets today on any machine without a registry, and
leaves the router's table empty (every service resolves to ``http://<name>``). It runs on its own
timer, not in any step.

**Answers** (:class:`Answers`): what a scenario says a service answers, by request key. An
answer can be asked for any number of times. One that nobody asked for fails the replay too: the
scenario's picture of the round is wrong.

**Provider settings.** Where each external provider lives comes from ``app.infra.config.settings``,
built from the environment at import. :func:`point_providers_at` swaps it for a copy whose
provider hosts are the replay's own (each boundary module's ``SETTINGS``), in every ``app``
module that bound it, so a developer's environment cannot change where a replay sends anything.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import httpx

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


class UnservedRequest(BaseException):
    """A request the replay does not answer. A ``BaseException``: the code's own
    ``except Exception`` must not turn a missing answer into a quiet failure."""


class Answers:
    """What a scenario said one service answers, by request key, and which were asked for."""

    def __init__(self, network: Network, service: str) -> None:
        self._network = network
        self._service = service
        self._given: dict[Any, Any] = {}
        self._asked: set[Any] = set()

    def give(self, key: Any, answer: Any) -> None:
        self._given[key] = answer

    def take(self, key: Any, request: httpx.Request) -> Any:
        """The answer for ``key``; refuses ``request`` when the scenario gave none."""
        if key not in self._given:
            self._network.refuse(
                request, f"the scenario gave {self._service} no answer for {key!r}"
            )
        self._asked.add(key)
        return self._given[key]

    def unused(self) -> list[str]:
        return [
            f"{self._service} {key!r}" for key in self._given if key not in self._asked
        ]


class Network:
    def __init__(self, effects) -> None:
        self.effects = effects
        self._hosts: dict[str, Handler] = {}
        self._answers: list[Answers] = []
        # Every request refused during the scenario, as "<METHOD> <url>: <why>".
        self.refused: list[str] = []

    def serve(self, host: str, handler: Handler) -> None:
        self._hosts[host] = handler

    def answers(self, service: str) -> Answers:
        found = Answers(self, service)
        self._answers.append(found)
        return found

    def unused(self) -> list[str]:
        return [entry for answers in self._answers for entry in answers.unused()]

    def refuse(self, request: httpx.Request, why: str) -> NoReturn:
        what = f"{request.method} {request.url}: {why}"
        self.refused.append(what)
        raise UnservedRequest(f"replay: {what}")

    def install(self, monkeypatch) -> None:
        from app.infra.lane import lane_router

        registry = httpx.URL(lane_router._registry_url).host
        network = self

        async def handle_async(transport, request: httpx.Request) -> httpx.Response:
            network.effects.alive()
            handler = network._hosts.get(request.url.host)
            if handler is None:
                network.refuse(request, "no such host in a replay")
            return await handler(request)

        def handle_sync(transport, request: httpx.Request) -> httpx.Response:
            if request.url.host == registry:
                raise httpx.ConnectError(
                    "replay: the lane registry is not reachable", request=request
                )
            network.effects.alive()
            network.refuse(request, "no such host in a replay")

        monkeypatch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", handle_async
        )
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_sync)


def point_providers_at(monkeypatch, overrides: dict[str, Any]) -> Callable[[], None]:
    """Swap ``app.infra.config.settings`` for a copy with ``overrides``, in every ``app`` module
    that bound it at import. Returns the function that puts the original back in modules first
    imported during the scenario (the monkeypatch undoes the rest)."""
    import app.infra.config as config

    original = config.settings
    replaced = dataclasses.replace(original, **overrides)
    for module in _app_modules():
        if getattr(module, "settings", None) is original:
            monkeypatch.setattr(module, "settings", replaced)

    def restore() -> None:
        for module in _app_modules():
            if getattr(module, "settings", None) is replaced:
                module.settings = original

    return restore


def _app_modules() -> list[Any]:
    return [
        module
        for name, module in list(sys.modules.items())
        if module is not None and (name == "app" or name.startswith("app."))
    ]


def json_response(
    request: httpx.Request, body: Any, status: int = 200
) -> httpx.Response:
    return httpx.Response(status, json=body, request=request)
