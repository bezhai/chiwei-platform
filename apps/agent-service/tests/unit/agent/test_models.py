"""test_models.py -- Model building tests.

Covers:
  - TTL cache: hit, expiry, penetration protection, clear, DB exception
  - build_chat_model dispatch: azure, google, openai-responses, deepseek, default
  - ModelBuildError on missing/inactive/incomplete config
  - _ReasoningChatOpenAI._normalize_content
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent import models as mod
from app.agent.models import (
    ModelBuildError,
    _get_model_and_provider_info,
    _ReasoningChatOpenAI,
    build_chat_model,
    clear_model_info_cache,
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


def _mock_session(mapping=None, provider=None, fallback_provider=None):
    """Create a mock session context manager that returns mapping/provider."""
    session = AsyncMock()

    # find_model_mapping
    async def fake_find_mapping(_sess, _alias):
        return mapping

    # find_provider_by_name
    async def fake_find_provider(_sess, name):
        if provider and hasattr(provider, "name") and provider.name == name:
            return provider
        if fallback_provider and name == "302.ai":
            return fallback_provider
        if provider:
            return provider
        return None

    return session, fake_find_mapping, fake_find_provider


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


class TestCacheHit:
    async def test_second_call_uses_cache(self):
        info = _make_model_info()
        call_count = 0

        async def fake_lookup(model_id):
            nonlocal call_count
            call_count += 1
            return info

        with patch.object(mod, "_get_model_and_provider_info", side_effect=fake_lookup):
            # Can't easily test internal caching without touching internals,
            # so we test the cache dict directly.
            pass

        # Direct cache test
        mod._model_info_cache["test-model"] = (info, time.monotonic() + 999)
        result = await _get_model_and_provider_info("test-model")
        assert result == info


class TestCacheExpiry:
    async def test_expired_entry_not_returned(self):
        info = _make_model_info()
        # Insert expired entry
        mod._model_info_cache["expired-model"] = (info, time.monotonic() - 1)

        # Expired entry triggers fresh DB lookup → mock DB to confirm bypass
        with patch(
            "app.data.session.get_session",
            side_effect=RuntimeError("no DB"),
        ):
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
# build_chat_model dispatch
# ---------------------------------------------------------------------------


class TestBuildChatModel:
    async def test_raises_on_missing_info(self):
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(ModelBuildError, match="not found"):
                await build_chat_model("missing-model")

    async def test_raises_on_inactive_model(self):
        info = _make_model_info(is_active=False)
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            with pytest.raises(ModelBuildError, match="disabled"):
                await build_chat_model("disabled-model")

    async def test_raises_on_missing_api_key(self):
        info = _make_model_info(api_key="")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            with pytest.raises(ModelBuildError, match="missing fields"):
                await build_chat_model("bad-model")

    async def test_dispatches_azure(self):
        info = _make_model_info(client_type="azure-http")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            from langchain_openai import AzureChatOpenAI

            model = await build_chat_model("azure-model")
            assert isinstance(model, AzureChatOpenAI)

    async def test_dispatches_deepseek(self):
        info = _make_model_info(client_type="deepseek")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            model = await build_chat_model("deepseek-model")
            assert isinstance(model, _ReasoningChatOpenAI)

    async def test_dispatches_openai_responses(self):
        info = _make_model_info(client_type="openai-responses")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            from langchain_openai import ChatOpenAI

            model = await build_chat_model("responses-model")
            assert isinstance(model, ChatOpenAI)
            assert not isinstance(model, _ReasoningChatOpenAI)

    async def test_dispatches_default_openai(self):
        info = _make_model_info(client_type="openai")
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            from langchain_openai import ChatOpenAI

            model = await build_chat_model("openai-model")
            assert isinstance(model, ChatOpenAI)

    async def test_proxy_injected_when_use_proxy(self):
        info = _make_model_info(use_proxy=True)
        with (
            patch.object(
                mod,
                "_get_model_and_provider_info",
                new_callable=AsyncMock,
                return_value=info,
            ),
            patch(
                "app.infra.config.settings",
                MagicMock(forward_proxy_url="http://proxy:8080"),
            ),
        ):
            model = await build_chat_model("proxy-model")
            assert model.openai_proxy == "http://proxy:8080"

    async def test_raises_on_db_error(self):
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connection refused"),
        ):
            with pytest.raises(ModelBuildError, match="lookup failed"):
                await build_chat_model("any-model")

    async def test_kwargs_allowlist_filters_dangerous_fields(self):
        info = _make_model_info()
        with patch.object(
            mod,
            "_get_model_and_provider_info",
            new_callable=AsyncMock,
            return_value=info,
        ):
            model = await build_chat_model(
                "test-model",
                api_key="attacker-key",       # not in allowlist → filtered
                base_url="https://evil.com",  # not in allowlist → filtered
                model="evil-model",           # not in allowlist → filtered
                reasoning_effort="low",       # in allowlist → passes through
            )
            assert model.model_name == "gpt-4o-mini"

    async def test_google_dispatch(self):
        info = _make_model_info(client_type="google")
        with (
            patch.object(
                mod,
                "_get_model_and_provider_info",
                new_callable=AsyncMock,
                return_value=info,
            ),
            patch(
                "app.infra.config.settings",
                MagicMock(forward_proxy_url=None),
            ),
        ):
            model = await build_chat_model("google-model")
            # Just check it's the right type from langchain_google_genai
            assert type(model).__name__ == "ChatGoogleGenerativeAI"


# ---------------------------------------------------------------------------
# _ReasoningChatOpenAI._normalize_content
# ---------------------------------------------------------------------------


class TestNormalizeContent:
    def test_none_returns_empty(self):
        assert _ReasoningChatOpenAI._normalize_content(None) == ""

    def test_string_passthrough(self):
        assert _ReasoningChatOpenAI._normalize_content("hello") == "hello"

    def test_list_of_strings(self):
        assert _ReasoningChatOpenAI._normalize_content(["a", "b"]) == "ab"

    def test_list_of_text_dicts(self):
        result = _ReasoningChatOpenAI._normalize_content(
            [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
        )
        assert result == "foobar"

    def test_mixed_list(self):
        result = _ReasoningChatOpenAI._normalize_content(
            ["plain", {"type": "text", "text": " block"}, {"type": "image_url"}]
        )
        assert result == "plain block"
