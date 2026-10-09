"""``app.main``'s lifespan with the real plugin host and agent-service's real plugins.

Only the two schema calls are replaced (they need a database), and there is no broker
(``RABBITMQ_URL`` empty), so no consumer or inbox opens. Everything else is what a pod runs: the
host from ``app.deployment.APPS``, every plugin's setup, the routes on the app, the skill reload
task, and the clocks phase with the lane gating.

Neither the surface test (``tests/apps``) nor the replay starts clocks, so this file is where the
clocks are seen to start through the lifespan: in a coe lane with
``DATAFLOW_ENABLE_TIME_SOURCES=1`` each of living's five clocks ticks into its node, and without
it none does. The nodes are replaced by recorders and the intervals shortened; which clocks
living registers, with which seconds and nodes, is pinned by
``tests/plugins/test_agent_service_equivalence.py``.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

LIVING_CLOCKS = (
    "LifeMomentTick",
    "PhoneNudgeTick",
    "LandingTick",
    "DayPageTick",
    "PersonaReviewTick",
)


@pytest.fixture
def pod(monkeypatch, tmp_path) -> list[str]:
    """agent-service in a coe lane without a broker; the schema calls are written down instead
    of made. Returns the list they are written to."""
    from inner_shared.dynamic_config import dynamic_config

    import app.host.host as host_module
    from app.skills.registry import SkillRegistry

    calls: list[str] = []

    def note(name: str):
        async def call() -> None:
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
    """living's clocks, each ticking every 20 ms into a recorder instead of its node. Returns
    what each recorder received, by clock name."""
    import app.plugins.living as living

    seen: dict[str, list[object]] = {}

    def recorder(name: str):
        async def node(data) -> None:
            seen.setdefault(name, []).append(data)

        return node

    monkeypatch.setattr(
        living,
        "CLOCKS",
        tuple((data_type, 0.02, recorder(data_type.__name__)) for data_type, _, _ in living.CLOCKS),
    )
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


async def test_with_time_sources_enabled_each_of_the_five_clocks_ticks(
    pod, ticks, monkeypatch, caplog
):
    from app.main import lifespan

    monkeypatch.setenv("DATAFLOW_ENABLE_TIME_SOURCES", "1")
    caplog.set_level(logging.INFO, logger="app.host.clock")

    async with lifespan(FastAPI()):
        async with asyncio.timeout(5):
            while set(ticks) != set(LIVING_CLOCKS):
                await asyncio.sleep(0.01)

    for name, received in ticks.items():
        assert {type(data).__name__ for data in received} == {name}
        assert all(data.ts for data in received)
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
