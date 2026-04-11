"""test_image_gen.py -- Image generation tests.

Covers:
  - _parse_gemini_size: pixel sizes, shorthand, fallback
  - generate_image dispatch: ark, openai, google
  - _generate_image_ark: mock AsyncArk, verify params including reference_images
  - _generate_image_openai: mock AsyncOpenAI, verify proxy handling
  - _generate_image_gemini: mock genai.Client, verify empty response raises RuntimeError
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent import image_gen as mod
from app.agent.image_gen import (
    _parse_gemini_size,
    generate_image,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_INFO = {
    "model_name": "image-gen-model",
    "api_key": "sk-test",
    "base_url": "https://api.test.com/v1",
    "client_type": "openai",
    "is_active": True,
    "use_proxy": False,
}


def _fake_info(**overrides: object) -> dict:
    return {**_FAKE_INFO, **overrides}


# ---------------------------------------------------------------------------
# _parse_gemini_size
# ---------------------------------------------------------------------------


class TestParseGeminiSize:
    def test_pixel_size_1k(self):
        assert _parse_gemini_size("512x512") == ("1:1", "1K")

    def test_pixel_size_2k(self):
        assert _parse_gemini_size("2048x1024") == ("2:1", "2K")

    def test_pixel_size_4k(self):
        assert _parse_gemini_size("4096x2048") == ("2:1", "4K")

    def test_shorthand_2k(self):
        assert _parse_gemini_size("2K") == ("1:1", "2K")

    def test_unknown_fallback(self):
        assert _parse_gemini_size("foo") == ("1:1", "1K")

    def test_non_square(self):
        ar, sz = _parse_gemini_size("1920x1080")
        assert ar == "16:9"
        assert sz == "2K"


# ---------------------------------------------------------------------------
# generate_image dispatch
# ---------------------------------------------------------------------------


class TestGenerateImageDispatch:
    async def test_dispatch_ark(self):
        with (
            patch(
                "app.agent.image_gen.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(client_type="ark"),
            ),
            patch.object(
                mod,
                "_generate_image_ark",
                new_callable=AsyncMock,
                return_value=["data:image/jpeg;base64,abc"],
            ) as mock_ark,
        ):
            result = await generate_image("img-model", prompt="a cat", size="1024x1024")

        assert result == ["data:image/jpeg;base64,abc"]
        mock_ark.assert_called_once()

    async def test_dispatch_openai(self):
        with (
            patch(
                "app.agent.image_gen.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(client_type="openai"),
            ),
            patch.object(
                mod,
                "_generate_image_openai",
                new_callable=AsyncMock,
                return_value=["data:image/jpeg;base64,def"],
            ) as mock_openai,
        ):
            result = await generate_image("img-model", prompt="a dog", size="2K")

        assert result == ["data:image/jpeg;base64,def"]
        mock_openai.assert_called_once()

    async def test_dispatch_google(self):
        with (
            patch(
                "app.agent.image_gen.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(client_type="google"),
            ),
            patch.object(
                mod,
                "_generate_image_gemini",
                new_callable=AsyncMock,
                return_value=["data:image/png;base64,ghi"],
            ) as mock_gemini,
        ):
            result = await generate_image("img-model", prompt="a bird", size="4K")

        assert result == ["data:image/png;base64,ghi"]
        mock_gemini.assert_called_once()

    async def test_default_dispatches_to_openai(self):
        """Unknown client_type falls back to OpenAI-compatible."""
        with (
            patch(
                "app.agent.image_gen.resolve_model_info",
                new_callable=AsyncMock,
                return_value=_fake_info(client_type="some-other"),
            ),
            patch.object(
                mod,
                "_generate_image_openai",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_openai,
        ):
            await generate_image("img-model", prompt="test", size="1K")

        mock_openai.assert_called_once()


# ---------------------------------------------------------------------------
# _generate_image_ark
# ---------------------------------------------------------------------------


class TestGenerateImageArk:
    async def test_basic_generation(self):
        mock_client = AsyncMock()
        mock_img = SimpleNamespace(b64_json="abc123")
        mock_client.images.generate = AsyncMock(
            return_value=SimpleNamespace(data=[mock_img])
        )
        mock_client.close = AsyncMock()

        with patch.object(mod, "_create_ark_client", return_value=mock_client):
            result = await mod._generate_image_ark(
                _fake_info(client_type="ark"),
                "a cat",
                "1024x1024",
                None,
            )

        assert result == ["data:image/jpeg;base64,abc123"]
        call_kwargs = mock_client.images.generate.call_args.kwargs
        assert call_kwargs["prompt"] == "a cat"
        assert call_kwargs["image"] is None
        mock_client.close.assert_called_once()

    async def test_with_reference_images(self):
        mock_client = AsyncMock()
        mock_img = SimpleNamespace(b64_json="ref_result")
        mock_client.images.generate = AsyncMock(
            return_value=SimpleNamespace(data=[mock_img])
        )
        mock_client.close = AsyncMock()

        refs = ["https://example.com/ref1.jpg", "https://example.com/ref2.jpg"]
        with patch.object(mod, "_create_ark_client", return_value=mock_client):
            result = await mod._generate_image_ark(
                _fake_info(client_type="ark"),
                "a cat like this",
                "2048x2048",
                refs,
            )

        assert result == ["data:image/jpeg;base64,ref_result"]
        call_kwargs = mock_client.images.generate.call_args.kwargs
        assert call_kwargs["image"] == refs


# ---------------------------------------------------------------------------
# _generate_image_openai
# ---------------------------------------------------------------------------


class TestGenerateImageOpenai:
    async def test_basic_generation(self):
        mock_img = SimpleNamespace(b64_json="oai_result")
        mock_resp = SimpleNamespace(data=[mock_img])

        mock_client_instance = AsyncMock()
        mock_client_instance.images.generate = AsyncMock(return_value=mock_resp)
        mock_client_instance.close = AsyncMock()

        with patch("openai.AsyncOpenAI", return_value=mock_client_instance):
            result = await mod._generate_image_openai(
                _fake_info(),
                "a dog",
                "1024x1024",
                None,
            )

        assert result == ["data:image/jpeg;base64,oai_result"]
        mock_client_instance.close.assert_called_once()

    async def test_proxy_handling(self):
        mock_img = SimpleNamespace(b64_json="proxy_result")
        mock_resp = SimpleNamespace(data=[mock_img])

        mock_client_instance = AsyncMock()
        mock_client_instance.images.generate = AsyncMock(return_value=mock_resp)
        mock_client_instance.close = AsyncMock()

        mock_http_client = AsyncMock()
        mock_http_client.aclose = AsyncMock()

        mock_settings = MagicMock()
        mock_settings.forward_proxy_url = "http://proxy:8080"

        with (
            patch("openai.AsyncOpenAI", return_value=mock_client_instance) as mock_cls,
            patch("app.infra.config.settings", mock_settings),
            patch("httpx.AsyncClient", return_value=mock_http_client) as mock_httpx,
        ):
            result = await mod._generate_image_openai(
                _fake_info(use_proxy=True),
                "a dog",
                "1024x1024",
                None,
            )

        assert result == ["data:image/jpeg;base64,proxy_result"]
        mock_httpx.assert_called_once_with(proxy="http://proxy:8080")
        # Verify http_client was passed to AsyncOpenAI
        init_kwargs = mock_cls.call_args.kwargs
        assert init_kwargs["http_client"] is mock_http_client
        # Both clients closed
        mock_client_instance.close.assert_called_once()
        mock_http_client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# _generate_image_gemini
# ---------------------------------------------------------------------------


class TestGenerateImageGemini:
    def _make_gemini_mocks(self, response):
        """Build mock genai module + client returning *response*."""
        mock_aio_models = AsyncMock()
        mock_aio_models.generate_content = AsyncMock(return_value=response)

        mock_aio = MagicMock()
        mock_aio.models = mock_aio_models

        mock_client = MagicMock()
        mock_client.aio = mock_aio

        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client
        return mock_genai

    async def test_empty_response_raises(self):
        mock_response = SimpleNamespace(candidates=[])
        mock_genai = self._make_gemini_mocks(mock_response)
        mock_types = MagicMock()

        mock_settings = MagicMock()
        mock_settings.forward_proxy_url = ""

        with (
            patch.dict("sys.modules", {
                "google.genai": mock_genai,
                "google.genai.types": mock_types,
            }),
            patch("app.infra.config.settings", mock_settings),
        ):
            with pytest.raises(RuntimeError, match="no candidates"):
                await mod._generate_image_gemini(
                    _fake_info(client_type="google"),
                    "a bird",
                    "1024x1024",
                    None,
                )

    async def test_no_image_parts_raises(self):
        mock_part = SimpleNamespace(inline_data=None)
        mock_candidate = SimpleNamespace(
            content=SimpleNamespace(parts=[mock_part])
        )
        mock_response = SimpleNamespace(candidates=[mock_candidate])
        mock_genai = self._make_gemini_mocks(mock_response)
        mock_types = MagicMock()

        mock_settings = MagicMock()
        mock_settings.forward_proxy_url = ""

        with (
            patch.dict("sys.modules", {
                "google.genai": mock_genai,
                "google.genai.types": mock_types,
            }),
            patch("app.infra.config.settings", mock_settings),
        ):
            with pytest.raises(RuntimeError, match="no image data"):
                await mod._generate_image_gemini(
                    _fake_info(client_type="google"),
                    "a bird",
                    "1024x1024",
                    None,
                )
