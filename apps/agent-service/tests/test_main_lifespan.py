"""``app.main``'s lifespan with the real plugin host and agent-service's real plugins.

Only the two schema calls are replaced (they need a database), and there is no broker
(``RABBITMQ_URL`` empty), so no consumer or inbox opens. Everything else is what a pod runs: the
host from ``app.deployment.APPS``, every plugin's setup, the routes on the app, the skill reload
task, and the clocks phase with the lane gating.

Neither the surface test (``tests/apps``) nor the replay starts clocks, so this file is where the
clocks are seen to start through the lifespan: in a coe lane with
``DATAFLOW_ENABLE_TIME_SOURCES=1`` each of living's three clocks ticks into its node and each of
her two wakes signals her queue, and without it none does. The nodes and the wake sources are
replaced by recorders and the intervals shortened; which clocks and wakes living registers is
pinned by ``tests/plugins/test_time_source_payload_contract.py`` and
``tests/living/test_moment.py``.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

LIVING_CLOCKS = (
    "LandingTick",
    "DayPageTick",
    "PersonaReviewTick",
)
HER_WAKES = ("regular", "nudge")


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
    """living's clocks, each ticking every 20 ms into a recorder instead of its node, and her two
    wakes, each signalling every 20 ms a source that records who it was asked about and runs
    nothing. Returns what each recorder received, by clock or source name."""
    from datetime import timedelta

    import app.plugins.living as living
    from app.host import Coalesce, WakeSource

    seen: dict[str, list[object]] = {}

    def recorder(name: str):
        async def node(data) -> None:
            seen.setdefault(name, []).append(data)

        return node

    def recording(name: str):
        def source(lane: str) -> WakeSource:
            async def due(persona_id, batch, clock) -> None:
                seen.setdefault(name, []).append(persona_id)

            async def run(due) -> None:  # pragma: no cover - due never says yes
                raise AssertionError

            return WakeSource(name, Coalesce.DROP_WHILE_ACTIVE, due, run)

        return source

    monkeypatch.setattr(
        living,
        "CLOCKS",
        tuple((data_type, 0.02, recorder(data_type.__name__)) for data_type, _, _ in living.CLOCKS),
    )
    monkeypatch.setattr(living, "regular", recording("regular"))
    monkeypatch.setattr(living, "nudge", recording("nudge"))
    monkeypatch.setattr(living, "REGULAR_EVERY", timedelta(seconds=0.02))
    monkeypatch.setattr(living, "NUDGE_EVERY", timedelta(seconds=0.02))
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

    for name in LIVING_CLOCKS:
        assert {type(data).__name__ for data in ticks[name]} == {name}
        assert all(data.ts for data in ticks[name])
    for name in HER_WAKES:
        assert set(ticks[name]) <= {"akao", "ayana", "chinagi"}
    # Three clocks and one clock for each of her two wakes, shared by the three sisters.
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
