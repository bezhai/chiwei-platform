"""``app.main``'s lifespan: the app named by ``APP_NAME`` starts through its plugin host with
every phase, and the host stops when the app shuts down.

The host itself stands in here (:class:`_FakeHost`); what a real host does with these flags is
``tests/host``'s, and the real lifespan with the real host is ``tests/test_main_lifespan.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI


class _FakeHost:
    """Writes down which app's host the lifespan asked for, and each start and stop."""

    made: list[_FakeHost] = []
    plugins: tuple = ()

    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        self.calls: list[tuple] = []

    @classmethod
    def for_app(cls, app_name: str) -> _FakeHost:
        host = cls(app_name)
        cls.made.append(host)
        return host

    async def start(self, *, http, schema: bool, mq: bool, clocks: bool, tasks: bool) -> None:
        self.calls.append(
            ("start", {"http": http, "schema": schema, "mq": mq, "clocks": clocks, "tasks": tasks})
        )

    async def stop(self) -> None:
        self.calls.append(("stop",))


@pytest.fixture
def fake_host(monkeypatch) -> type[_FakeHost]:
    monkeypatch.setattr(_FakeHost, "made", [])
    monkeypatch.setattr("app.main.Host", _FakeHost)
    return _FakeHost


def _broker(monkeypatch, url: str | None) -> None:
    monkeypatch.setattr("app.main.settings", SimpleNamespace(rabbitmq_url=url))


async def test_the_app_named_by_app_name_starts_through_its_host_with_every_phase(
    fake_host, monkeypatch
):
    from app.main import lifespan

    monkeypatch.setenv("APP_NAME", "world")
    _broker(monkeypatch, "amqp://test")
    app = FastAPI()

    async with lifespan(app):
        [host] = fake_host.made
        assert host.app_name == "world"
        assert host.calls == [
            ("start", {"http": app, "schema": True, "mq": True, "clocks": True, "tasks": True})
        ]

    assert host.calls[1:] == [("stop",)]


async def test_without_a_broker_the_host_starts_without_mq(fake_host, monkeypatch):
    from app.main import lifespan

    monkeypatch.setenv("APP_NAME", "agent-service")
    _broker(monkeypatch, "")

    async with lifespan(FastAPI()):
        pass

    [host] = fake_host.made
    [(_, flags), stop] = host.calls
    assert flags["mq"] is False
    assert (flags["schema"], flags["clocks"], flags["tasks"]) == (True, True, True)
    assert stop == ("stop",)


async def test_without_app_name_the_process_is_agent_service(fake_host, monkeypatch):
    from app.main import lifespan

    monkeypatch.delenv("APP_NAME", raising=False)
    _broker(monkeypatch, None)

    async with lifespan(FastAPI()):
        pass

    [host] = fake_host.made
    assert host.app_name == "agent-service"
