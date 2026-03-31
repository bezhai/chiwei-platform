"""test_gemini_client.py — GeminiClient 单元测试"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _fake_model_info(**overrides):
    info = {
        "api_key": "fake-key",
        "base_url": "",
        "model_name": "gemini-2.0-flash-exp",
        "client_type": "google",
        "is_active": True,
    }
    info.update(overrides)
    return info


def _make_response(image_bytes: bytes, mime_type: str = "image/png"):
    """构造 google-genai SDK 风格的 response mock"""
    inline_data = MagicMock()
    inline_data.mime_type = mime_type
    inline_data.data = image_bytes

    part = MagicMock()
    part.inline_data = inline_data
    part.text = None

    candidate = MagicMock()
    candidate.content.parts = [part]

    response = MagicMock()
    response.candidates = [candidate]
    return response


class TestGenerateImage:
    """generate_image 正常路径"""

    async def test_returns_base64_data_uri(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        fake_image = b"\x89PNG_FAKE"
        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = _make_response(
            fake_image
        )

        with patch(
            "app.agents.infra.model_builder.ModelBuilder.get_basic_model_params",
            new=AsyncMock(return_value=_fake_model_info()),
        ):
            client._client = mock_genai_client
            client.model_name = "gemini-2.0-flash-exp"

            result = await client.generate_image("a cat", "1K")

        assert len(result) == 1
        expected_b64 = base64.b64encode(fake_image).decode()
        assert result[0] == f"data:image/png;base64,{expected_b64}"

    async def test_with_reference_images(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        fake_image = b"\x89PNG_FAKE"
        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = _make_response(
            fake_image
        )

        with patch(
            "app.agents.infra.model_builder.ModelBuilder.get_basic_model_params",
            new=AsyncMock(return_value=_fake_model_info()),
        ):
            client._client = mock_genai_client
            client.model_name = "gemini-2.0-flash-exp"

            result = await client.generate_image(
                "a cat in this style",
                "2K",
                reference_images=["https://example.com/ref.png"],
            )

        assert len(result) == 1
        # 验证调用时包含了 reference image
        call_args = mock_genai_client.models.generate_content.call_args
        contents = call_args.kwargs.get("contents") or call_args[1].get("contents")
        assert len(contents) > 1  # prompt + reference image

    async def test_no_candidates_raises(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        mock_genai_client = MagicMock()
        response = MagicMock()
        response.candidates = []
        mock_genai_client.models.generate_content.return_value = response

        client._client = mock_genai_client
        client.model_name = "gemini-2.0-flash-exp"

        with pytest.raises(RuntimeError, match="未返回"):
            await client.generate_image("a cat", "1K")

    async def test_no_images_in_response_raises(self):
        from app.agents.clients.gemini_client import GeminiClient

        client = GeminiClient("test-gemini")

        # part 有 inline_data 但 data 为空
        part = MagicMock()
        part.inline_data = MagicMock()
        part.inline_data.data = None

        candidate = MagicMock()
        candidate.content.parts = [part]

        response = MagicMock()
        response.candidates = [candidate]

        mock_genai_client = MagicMock()
        mock_genai_client.models.generate_content.return_value = response

        client._client = mock_genai_client
        client.model_name = "gemini-2.0-flash-exp"

        with pytest.raises(RuntimeError, match="未在响应中找到图片"):
            await client.generate_image("a cat", "1K")


class TestParseSize:
    """_parse_size 尺寸映射"""

    def test_1k_default(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_size("1K") == ("1:1", "1K")

    def test_2k(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_size("2K") == ("1:1", "2K")

    def test_4k(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_size("4K") == ("1:1", "4K")

    def test_pixel_16_9(self):
        from app.agents.clients.gemini_client import GeminiClient

        ratio, size = GeminiClient._parse_size("1920x1080")
        assert ratio == "16:9"
        assert size == "2K"

    def test_square_pixel(self):
        from app.agents.clients.gemini_client import GeminiClient

        ratio, size = GeminiClient._parse_size("2048x2048")
        assert ratio == "1:1"
        assert size == "2K"

    def test_large_pixel_maps_to_4k(self):
        from app.agents.clients.gemini_client import GeminiClient

        ratio, size = GeminiClient._parse_size("4096x4096")
        assert size == "4K"

    def test_invalid_falls_back(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_size("invalid") == ("1:1", "1K")

    def test_empty_string(self):
        from app.agents.clients.gemini_client import GeminiClient

        assert GeminiClient._parse_size("") == ("1:1", "1K")
