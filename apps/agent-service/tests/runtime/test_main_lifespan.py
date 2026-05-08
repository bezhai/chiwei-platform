"""main.py lifespan invokes migrate + start_source_loops in the right order."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_migrates_then_starts_sources():
    """migrate_schema must run BEFORE start_consumers (durable consumer
    needs the table to exist) and start_source_loops must run AFTER
    register_http_sources."""
    call_order: list[str] = []

    async def _migrate(self):
        call_order.append("migrate_schema")

    async def _start_consumers(*_a, **_kw):
        call_order.append("start_consumers")

    async def _start_source_loops(self):
        call_order.append("start_source_loops")

    async def _stop_source_loops(self):
        call_order.append("stop_source_loops")

    # Patch setup_logging in case app.main hasn't been imported yet
    # (writing to /logs requires perms not present in the test env).
    # Do NOT pop app.main from sys.modules — re-importing it leaks state
    # into other modules' caches and breaks downstream tests.
    with patch("inner_shared.logger.setup_logging", MagicMock()), \
         patch("app.runtime.engine.Runtime.migrate_schema", _migrate), \
         patch("app.runtime.durable.start_consumers", AsyncMock(side_effect=_start_consumers)), \
         patch("app.runtime.engine.Runtime.start_source_loops", _start_source_loops), \
         patch("app.runtime.engine.Runtime.stop_source_loops", _stop_source_loops), \
         patch("app.infra.qdrant.init_collections", AsyncMock()), \
         patch("app.runtime.bootstrap.declare_durable_topology", AsyncMock()), \
         patch("app.runtime.debounce.start_debounce_consumers", AsyncMock()), \
         patch("app.runtime.debounce.stop_debounce_consumers", AsyncMock()), \
         patch("app.runtime.durable.stop_consumers", AsyncMock()), \
         patch("app.skills.registry.SkillRegistry.load_all"), \
         patch("app.skills.registry.skill_reload_loop", AsyncMock()), \
         patch("app.runtime.http_source.register_http_sources"), \
         patch("app.main.settings", MagicMock(rabbitmq_url="amqp://test")):
        from fastapi import FastAPI

        from app.main import lifespan

        app = FastAPI()
        async with lifespan(app):
            pass

    assert call_order.index("migrate_schema") < call_order.index("start_consumers")
    assert call_order.index("start_source_loops") > call_order.index("start_consumers")
    assert "stop_source_loops" in call_order  # teardown ran
