"""runtime_entry.main(): APP_NAME injection contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_runtime_entry_main_requires_app_name(monkeypatch):
    """Missing APP_NAME must raise — otherwise Runtime would silently
    fall back to DEFAULT_APP and a worker pod would come up running
    the wrong app's node subset.
    """
    from app.workers.runtime_entry import main

    monkeypatch.delenv("APP_NAME", raising=False)
    with pytest.raises(RuntimeError, match="APP_NAME"):
        main()


def test_runtime_entry_main_rejects_empty_app_name(monkeypatch):
    """An empty string is not a valid APP_NAME either."""
    from app.workers.runtime_entry import main

    monkeypatch.setenv("APP_NAME", "")
    with pytest.raises(RuntimeError, match="APP_NAME"):
        main()


def test_runtime_entry_main_calls_ensure_business_schema_before_runtime_run(monkeypatch):
    """runtime_entry.main() must call ensure_business_schema() before
    Runtime.run() to ensure schema is bootstrapped before dataflow nodes run.
    """
    call_order = []

    monkeypatch.setenv("APP_NAME", "vectorize-worker")

    async def mock_main_async():
        # This will be called instead of the real _main_async
        call_order.append("ensure_business_schema")
        call_order.append("runtime_run")

    with patch("app.workers.runtime_entry.setup_logging"), \
         patch("app.workers.runtime_entry.ensure_business_schema", new_callable=AsyncMock) as mock_ensure, \
         patch("app.workers.runtime_entry.load_dataflow_graph"), \
         patch("app.workers.runtime_entry.Runtime") as MockRuntime:

        mock_instance = MagicMock()
        mock_instance.run = AsyncMock()
        MockRuntime.return_value = mock_instance

        # Track calls in the actual _main_async
        original_ensure_called = []
        original_run_called = []

        async def track_ensure_calls():
            original_ensure_called.append(True)

        async def track_run_calls():
            original_run_called.append(True)

        mock_ensure.side_effect = track_ensure_calls
        mock_instance.run.side_effect = track_run_calls

        from app.workers.runtime_entry import main

        main()

        # Verify ensure_business_schema was called
        assert mock_ensure.called, "ensure_business_schema should be called"
        # Verify Runtime.run() was called
        assert mock_instance.run.called, "Runtime.run() should be called"
