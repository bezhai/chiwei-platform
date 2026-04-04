"""test_proxy_injection.py — use_proxy 标志位的 proxy 注入测试

覆盖场景：
- use_proxy=True + forward_proxy_url 存在 → 注入 openai_proxy
- use_proxy=False → 不注入
- use_proxy=True + forward_proxy_url 为 None → 不注入
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.infra.model_builder import ModelBuilder

pytestmark = pytest.mark.unit


def _make_model_info(*, client_type: str = "openai", use_proxy: bool = False):
    return {
        "model_name": "test-model",
        "api_key": "sk-test",
        "base_url": "https://api.test.com/v1",
        "is_active": True,
        "client_type": client_type,
        "use_proxy": use_proxy,
    }


class TestOpenAIProxyInjection:
    """openai / openai-responses / deepseek 分支的 proxy 注入"""

    @pytest.mark.parametrize("client_type", ["openai", "openai-responses", "deepseek"])
    async def test_proxy_injected_when_use_proxy_true(self, client_type):
        info = _make_model_info(client_type=client_type, use_proxy=True)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            model = await ModelBuilder.build_chat_model("test")

        assert model.openai_proxy == "http://proxy:7890"

    @pytest.mark.parametrize("client_type", ["openai", "openai-responses", "deepseek"])
    async def test_no_proxy_when_use_proxy_false(self, client_type):
        info = _make_model_info(client_type=client_type, use_proxy=False)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            model = await ModelBuilder.build_chat_model("test")

        assert model.openai_proxy is None

    @pytest.mark.parametrize("client_type", ["openai", "openai-responses", "deepseek"])
    async def test_no_proxy_when_proxy_url_none(self, client_type):
        info = _make_model_info(client_type=client_type, use_proxy=True)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
        ):
            mock_settings.forward_proxy_url = None
            model = await ModelBuilder.build_chat_model("test")

        assert model.openai_proxy is None


class TestGoogleProxyInjection:
    """google 分支的 proxy 注入"""

    async def test_proxy_injected_when_use_proxy_true(self):
        info = _make_model_info(client_type="google", use_proxy=True)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
            patch(
                "langchain_google_genai.ChatGoogleGenerativeAI"
            ) as MockGoogle,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            await ModelBuilder.build_chat_model("test")

        call_kwargs = MockGoogle.call_args.kwargs
        assert call_kwargs.get("client_args") == {"proxy": "http://proxy:7890"}

    async def test_no_proxy_when_use_proxy_false(self):
        info = _make_model_info(client_type="google", use_proxy=False)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
            patch(
                "langchain_google_genai.ChatGoogleGenerativeAI"
            ) as MockGoogle,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            await ModelBuilder.build_chat_model("test")

        call_kwargs = MockGoogle.call_args.kwargs
        assert "client_args" not in call_kwargs
