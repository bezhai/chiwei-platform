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
         patch("app.runtime.bootstrap.declare_durable_topology", AsyncMock()), \
         patch("app.runtime.debounce.start_debounce_consumers", AsyncMock()), \
         patch("app.runtime.debounce.stop_debounce_consumers", AsyncMock()), \
         patch("app.runtime.durable.stop_consumers", AsyncMock()), \
         patch("app.messaging.lifecycle.start_messaging", AsyncMock()), \
         patch("app.messaging.lifecycle.stop_messaging", AsyncMock()), \
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


@pytest.mark.asyncio
async def test_lifespan_boots_the_app_named_by_app_name_and_runs_messaging(monkeypatch):
    """FastAPI 入口按 PaaS 注入的 APP_NAME 启动那个 App：只加载它的接线、只起它的消费者；
    有 broker 时启停通信机制（收件箱、定时送达）。"""
    monkeypatch.setenv("APP_NAME", "world")
    order: list[str] = []
    prepared: list[str] = []
    runtime_apps: list[str] = []

    async def _prepare(app_name, *, declare_topology=False):
        prepared.append(app_name)

    async def _migrate(self):
        runtime_apps.append(self.app_name)

    async def _start_consumers(*_a, app_name=None, **_kw):
        order.append(f"start_consumers:{app_name}")

    async def _start_messaging():
        order.append("start_messaging")

    async def _stop_messaging():
        order.append("stop_messaging")

    async def _stop_consumers():
        order.append("stop_consumers")

    with patch("inner_shared.logger.setup_logging", MagicMock()), \
         patch("app.runtime.bootstrap.prepare_for_run", _prepare), \
         patch("app.runtime.engine.Runtime.migrate_schema", _migrate), \
         patch("app.runtime.durable.start_consumers", AsyncMock(side_effect=_start_consumers)), \
         patch("app.runtime.engine.Runtime.start_source_loops", AsyncMock()), \
         patch("app.runtime.engine.Runtime.stop_source_loops", AsyncMock()), \
         patch("app.runtime.debounce.start_debounce_consumers", AsyncMock()), \
         patch("app.runtime.debounce.stop_debounce_consumers", AsyncMock()), \
         patch("app.runtime.durable.stop_consumers", AsyncMock(side_effect=_stop_consumers)), \
         patch("app.messaging.lifecycle.start_messaging", _start_messaging), \
         patch("app.messaging.lifecycle.stop_messaging", _stop_messaging), \
         patch("app.infra.rabbitmq.mq.close", AsyncMock()), \
         patch("app.skills.registry.SkillRegistry.load_all"), \
         patch("app.skills.registry.skill_reload_loop", AsyncMock()), \
         patch("app.runtime.http_source.register_http_sources"), \
         patch("app.main.settings", MagicMock(rabbitmq_url="amqp://test")):
        from fastapi import FastAPI

        from app.main import lifespan

        async with lifespan(FastAPI()):
            pass

    assert prepared == ["world"]
    assert runtime_apps == ["world"]
    assert order[:2] == ["start_consumers:world", "start_messaging"]
    assert order.index("stop_messaging") < order.index("stop_consumers")
