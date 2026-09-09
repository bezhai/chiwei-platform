"""test_models.py -- Model info resolution tests.

Covers:
  - TTL cache: hit, expiry, penetration protection, clear, DB exception
  - resolve_model_info: ModelBuildError on missing/inactive/incomplete config,
    success returns the validated info dict

Model *building* (the old langchain ``build_chat_model`` dispatch) is gone — the
neutral ``ModelClient`` (``app.agent.client``) now owns construction and is
tested under ``tests/agent``. ``resolve_model_info`` is the retained core that
``client`` / ``embedding`` / ``image_gen`` all share.
"""

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import models as mod
from app.agent.models import (
    ModelBuildError,
    _get_model_and_provider_info,
    clear_model_info_cache,
    resolve_model_info,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_model_info(
    *,
    model_name: str = "gpt-4o-mini",
    api_key: str = "sk-test",
    base_url: str = "https://api.test.com/v1",
    client_type: str = "openai",
    is_active: bool = True,
    use_proxy: bool = False,
) -> dict:
    return {
        "model_name": model_name,
        "api_key": api_key,
        "base_url": base_url,
        "client_type": client_type,
        "is_active": is_active,
        "use_proxy": use_proxy,
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_model_info_cache()
    yield
    clear_model_info_cache()


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


class TestCacheHit:
    async def test_second_call_uses_cache(self):
        info = _make_model_info()
        # Direct cache test: a fresh entry is returned without a DB hit.
        mod._model_info_cache["test-model"] = (info, time.monotonic() + 999)
        result = await _get_model_and_provider_info("test-model")
        assert result == info


class TestCacheExpiry:
    async def test_expired_entry_not_returned(self):
        info = _make_model_info()
        # Insert expired entry
        mod._model_info_cache["expired-model"] = (info, time.monotonic() - 1)

        # Expired entry triggers fresh DB lookup → mock tx to confirm bypass.
        # Patch tx at the module-level reference inside app.agent.models so
        # the substitution wins over the live runtime.db ref.
        with patch.object(mod, "tx", side_effect=RuntimeError("no DB")):
            with pytest.raises(RuntimeError, match="no DB"):
                await _get_model_and_provider_info("expired-model")


class TestCacheIsolation:
    async def test_cache_returns_copy(self):
        info = _make_model_info()
        mod._model_info_cache["copy-test"] = (info, time.monotonic() + 999)

        result1 = await _get_model_and_provider_info("copy-test")
        result1["model_name"] = "mutated"

        result2 = await _get_model_and_provider_info("copy-test")
        assert result2["model_name"] == "gpt-4o-mini"  # original, not mutated


class TestCacheClear:
    def test_clear_empties_cache(self):
        mod._model_info_cache["foo"] = (_make_model_info(), time.monotonic() + 999)
        assert len(mod._model_info_cache) > 0
        clear_model_info_cache()
        assert len(mod._model_info_cache) == 0


# ---------------------------------------------------------------------------
# resolve_model_info (retained core, shared by client/embedding/image_gen)
# ---------------------------------------------------------------------------


class TestResolveModelInfo:
    async def test_returns_info_on_success(self):
        info = _make_model_info()
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            result = await resolve_model_info("ok-model")
            assert result == info

    async def test_raises_on_missing_info(self):
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(ModelBuildError, match="not found"):
                await resolve_model_info("missing-model")

    async def test_raises_on_inactive_model(self):
        info = _make_model_info(is_active=False)
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            with pytest.raises(ModelBuildError, match="disabled"):
                await resolve_model_info("disabled-model")

    async def test_raises_on_missing_required_field(self):
        info = _make_model_info(api_key="")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            with pytest.raises(ModelBuildError, match="missing fields"):
                await resolve_model_info("bad-model")

    async def test_raises_on_db_error(self):
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connection refused"),
        ):
            with pytest.raises(ModelBuildError, match="lookup failed"):
                await resolve_model_info("any-model")

    async def test_custom_required_fields(self):
        # base_url is absent from required defaults but can be demanded.
        info = _make_model_info(base_url="")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            with pytest.raises(ModelBuildError, match="missing fields"):
                await resolve_model_info(
                    "no-base-url", required_fields=("api_key", "base_url")
                )


# ---------------------------------------------------------------------------
# Provider row -> info dict
# ---------------------------------------------------------------------------


class TestProviderRowReachesTheInfoDict:
    async def test_api_version_travels_with_the_rest_of_the_provider_config(
        self, monkeypatch
    ):
        """A provider that pins its API version must carry it to the adapter.

        Nothing between the row and the adapter reads it, so a dropped field
        fails silently: the client dials the SDK default version instead and
        the gateway answers 404.
        """
        provider = SimpleNamespace(
            api_key="k",
            base_url="https://gw/prefix",
            is_active=True,
            client_type="google",
            use_proxy=False,
            api_version="v1",
        )
        mapping = SimpleNamespace(
            provider_name="gw", real_model_name="gemini-3.7-flash"
        )

        @asynccontextmanager
        async def _no_tx():
            yield None

        monkeypatch.setattr(mod, "tx", _no_tx)
        monkeypatch.setattr(
            "app.data.queries.find_model_mapping", AsyncMock(return_value=mapping)
        )
        monkeypatch.setattr(
            "app.data.queries.find_provider_by_name", AsyncMock(return_value=provider)
        )

        info = await _get_model_and_provider_info("life-model")

        assert info is not None
        assert info["api_version"] == "v1"
        assert info["model_name"] == "gemini-3.7-flash"
        assert info["client_type"] == "google"
