"""runtime_entry.main(): APP_NAME injection contract."""

from __future__ import annotations

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
