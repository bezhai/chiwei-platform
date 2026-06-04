"""Tests for ``app.infra.redis.get_redis`` connection-pool construction.

Regression: the pool port was hardcoded to 6379, ignoring ``REDIS_PORT``.
Prod redis happens to run on 6379 so it never surfaced, but coe lanes
(ConfigBundle injects ``REDIS_PORT=6380``) got connection-refused. These
tests pin the pool port to ``settings.redis_port`` so the port follows env.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest


async def _build_pool_with_env(monkeypatch, env: dict[str, str]):
    """Build the redis pool under ``env`` and return its connection kwargs.

    ``settings`` is a frozen+slots singleton bound at import; to make env
    changes visible we rebuild a fresh ``Settings()`` under the patched env
    and swap it onto the redis module, then reset the ``_redis`` singleton.
    ``ConnectionPool`` is lazy (no socket on construction), so we can inspect
    ``connection_kwargs`` without a live redis.
    """
    import app.infra.config as config_mod
    import app.infra.redis as redis_mod

    importlib.reload(redis_mod)  # clear the module-level ``_redis`` singleton
    with patch.dict("os.environ", env, clear=True):
        fresh_settings = config_mod.Settings()
    monkeypatch.setattr(redis_mod, "settings", fresh_settings)

    client = await redis_mod.get_redis()
    try:
        return client.connection_pool.connection_kwargs
    finally:
        importlib.reload(redis_mod)  # don't leak the singleton to other tests


@pytest.mark.asyncio
async def test_pool_uses_redis_port_from_settings(monkeypatch):
    """Non-default port (coe lane case) must reach the connection pool."""
    kwargs = await _build_pool_with_env(
        monkeypatch, {"REDIS_HOST": "10.37.6.235", "REDIS_PORT": "6380"}
    )
    assert kwargs["port"] == 6380


@pytest.mark.asyncio
async def test_pool_default_port_is_6379(monkeypatch):
    """Prod behaviour preserved: default port stays 6379 when unset."""
    kwargs = await _build_pool_with_env(monkeypatch, {"REDIS_HOST": "redis.prod"})
    assert kwargs["port"] == 6379
