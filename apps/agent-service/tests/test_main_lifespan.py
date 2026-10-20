"""``app.main``'s lifespan with the real plugin host and agent-service's real plugins.

Only the two schema calls are replaced (they need a database), and there is no broker
(``RABBITMQ_URL`` empty), so no consumer or inbox opens. Everything else is what a pod runs: the
host from ``app.deployment.APPS``, every plugin's setup, the routes on the app, the skill reload
task, and the clocks phase with the lane gating.

Neither the surface test (``tests/apps``) nor the replay starts clocks, so this file is where the
clocks are seen to start through the lifespan: in a coe lane with
``DATAFLOW_ENABLE_TIME_SOURCES=1`` living's landing clock ticks and each of her four wakes
signals her queues, and without it none does. What the clock runs and the wake sources are
replaced by recorders and the intervals shortened; which clocks and wakes living registers is
pinned by ``tests/living/test_landing.py``, ``tests/living/test_moment.py``,
``tests/living/test_day_page.py`` and ``tests/living/test_persona_review.py``.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

LIVING_CLOCKS = ("landing",)
HER_WAKES = ("regular", "nudge", "day-page", "persona-review")


@pytest.fixture
def pod(monkeypatch, tmp_path) -> list[str]:
    """agent-service in a coe lane without a broker; the schema calls are written down instead
    of made. Returns the list they are written to."""
    from inner_shared.dynamic_config import dynamic_config

    import app.host.host as host_module
    from app.skills.registry import SkillRegistry

    calls: list[str] = []

    def note(name: str):
        async def call(*_args, **_kwargs) -> None:
            calls.append(name)

        return call

    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("APP_NAME", "agent-service")
    monkeypatch.setenv("LANE", "coe-lifespan")
    monkeypatch.setenv("SKILLS_DIR", str(skills))
    monkeypatch.delenv("DATAFLOW_ENABLE_TIME_SOURCES", raising=False)
    monkeypatch.setattr("app.main.settings", SimpleNamespace(rabbitmq_url=""))
    monkeypatch.setattr(host_module, "ensure_business_schema", note("ensure_business_schema"))
    monkeypatch.setattr(host_module, "migrate_schema", note("migrate_schema"))
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    # The host points Dynamic Config at the deployment lane; put back what other tests had.
    monkeypatch.setattr(dynamic_config, "_lane_provider", dynamic_config._lane_provider)
    return calls


@pytest.fixture
def ticks(monkeypatch) -> dict[str, list[object]]:
    """living's landing clock, ticking every 20 ms into a recorder of the lane it reconciles, and
    her wakes, each signalling every 20 ms a source that records who it was asked about and runs
    nothing. Returns what each recorder received, by clock or source name."""
    from datetime import timedelta

    import app.plugins.living as living
    from app.host import Coalesce, WakeSource

    seen: dict[str, list[object]] = {}

    async def reconciles(*, lane: str) -> None:
        seen.setdefault("landing", []).append(lane)

    def recording(name: str):
        def source(lane: str) -> WakeSource:
            async def due(persona_id, batch, clock) -> None:
                seen.setdefault(name, []).append(persona_id)

            async def run(due) -> None:  # pragma: no cover - due never says yes
                raise AssertionError

            return WakeSource(name, Coalesce.DROP_WHILE_ACTIVE, due, run)

        return source

    monkeypatch.setattr(living, "reconcile_what_she_said", reconciles)
    monkeypatch.setattr(living, "LANDING_EVERY", timedelta(seconds=0.02))
    monkeypatch.setattr(living, "regular", recording("regular"))
    monkeypatch.setattr(living, "nudge", recording("nudge"))
    monkeypatch.setattr(living, "day_page", recording("day-page"))
    monkeypatch.setattr(living, "persona_review", recording("persona-review"))
    monkeypatch.setattr(living, "REGULAR_EVERY", timedelta(seconds=0.02))
    monkeypatch.setattr(living, "NUDGE_EVERY", timedelta(seconds=0.02))
    monkeypatch.setattr(living, "DAY_PAGE_EVERY", timedelta(seconds=0.02))
    monkeypatch.setattr(living, "PERSONA_REVIEW_EVERY", timedelta(seconds=0.02))
    return seen


def _admin_paths(app: FastAPI) -> set[str]:
    return {r.path for r in app.routes if r.path.startswith("/admin/")}


async def test_startup_builds_the_business_schema_then_migrates_and_shutdown_takes_routes_back(
    pod,
):
    from app.main import lifespan

    app = FastAPI()
    async with lifespan(app):
        assert pod == ["ensure_business_schema", "migrate_schema"]
        assert "/admin/messaging/send" in _admin_paths(app)
        assert "/admin/dlq/inspect" in _admin_paths(app)

    assert _admin_paths(app) == set()


async def test_with_time_sources_enabled_each_clock_ticks_and_each_wake_signals_her(
    pod, ticks, monkeypatch, caplog
):
    from app.main import lifespan

    monkeypatch.setenv("DATAFLOW_ENABLE_TIME_SOURCES", "1")
    caplog.set_level(logging.INFO, logger="app.host.clock")

    async with lifespan(FastAPI()):
        async with asyncio.timeout(5):
            while set(ticks) != {*LIVING_CLOCKS, *HER_WAKES}:
                await asyncio.sleep(0.01)

    assert set(ticks["landing"]) == {"coe-lifespan"}
    for name in HER_WAKES:
        assert set(ticks[name]) <= {"akao", "ayana", "chinagi"}
    # One clock and one clock for each of her four wakes, shared by the three sisters.
    assert "runtime: app=agent-service started 5 clock(s)" in caplog.messages

    # Stopped with the app: no tick after shutdown.
    counts = {name: len(received) for name, received in ticks.items()}
    await asyncio.sleep(0.1)
    assert {name: len(received) for name, received in ticks.items()} == counts


async def test_in_a_coe_lane_without_the_override_no_clock_ticks(pod, ticks, caplog):
    from app.main import lifespan

    caplog.set_level(logging.INFO, logger="app.host.clock")

    async with lifespan(FastAPI()):
        await asyncio.sleep(0.1)

    assert ticks == {}
    assert any(
        "skipped 5 interval source(s) in lane=coe-lifespan" in m for m in caplog.messages
    )
