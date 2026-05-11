"""Test agent-service main.py lifespan bootstrap."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI


def test_main_imports_ensure_business_schema():
    """断言 main.py 模块级导入了 ensure_business_schema"""
    import app.main

    assert hasattr(app.main, "ensure_business_schema")


@pytest.mark.asyncio
async def test_lifespan_calls_ensure_business_schema():
    """main.py lifespan startup 期必须调用 ensure_business_schema"""
    # Import lifespan directly (main.py module is already loaded)
    from app.main import lifespan

    # Create a proper mock task for asyncio operations
    async def noop():
        pass

    mock_task = asyncio.create_task(noop())

    # Mock all the external dependencies in startup
    with patch("app.main.init_collections", new=AsyncMock()):
        with patch("app.main.ensure_business_schema", new=AsyncMock()) as mock_ensure:
            with patch("app.runtime.bootstrap.load_dataflow_graph"):
                with patch("app.runtime.engine.Runtime") as MockRuntime:
                    mock_runtime = AsyncMock()
                    MockRuntime.return_value = mock_runtime
                    mock_runtime.migrate_schema = AsyncMock()
                    mock_runtime.start_source_loops = AsyncMock()
                    mock_runtime.stop_source_loops = AsyncMock()

                    with patch("app.infra.rabbitmq.KNOWN_APPS_FOR_DELAYED_TRIGGER", []):
                        with patch("app.infra.config.settings") as mock_settings:
                            mock_settings.rabbitmq_url = None

                            with patch("app.skills.registry.SkillRegistry.load_all"):
                                with patch("app.runtime.outbox_dispatcher.dispatcher_loop", new=AsyncMock()):
                                    with patch("asyncio.create_task") as mock_create_task:
                                        # Return a real task that can be properly cancelled and awaited
                                        mock_create_task.return_value = mock_task

                                        app = FastAPI()
                                        try:
                                            async with lifespan(app):
                                                pass
                                        finally:
                                            # Clean up the mock task
                                            if not mock_task.done():
                                                mock_task.cancel()
                                                try:
                                                    await mock_task
                                                except asyncio.CancelledError:
                                                    pass

                                        # Assert ensure_business_schema was called during startup
                                        mock_ensure.assert_awaited_once()
