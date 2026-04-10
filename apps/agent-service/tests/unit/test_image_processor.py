"""test_image_processor.py — ImageProcessor 单元测试

场景覆盖：
- upload_and_register: mock image_client, 验证 TOS URL + filename 返回, registry.register 调用
- upload_and_register without registry: 只上传不注册, filename 为 None
- upload_and_register failure: 上传异常 → fallback 到原始 URL, filename 为 None
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.tools.image.processor import ImageProcessor

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_registry():
    """Mock ImageRegistry with register method."""
    registry = AsyncMock()
    registry.register = AsyncMock(return_value="1.png")
    return registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUploadAndRegister:
    """upload_and_register tests."""

    async def test_upload_and_register_returns_tos_url(self, mock_registry):
        """上传成功 → 返回 (TOS URL, filename)，registry.register 被调用."""
        tos_url = "https://tos.example.com/image.png"

        with patch(
            "app.agents.tools.image.processor.image_client"
        ) as mock_client:
            mock_client.upload_to_tos = AsyncMock(return_value=tos_url)

            result_url, result_fn = await ImageProcessor.upload_and_register(
                source_type="url",
                data="https://example.com/photo.jpg",
                registry=mock_registry,
            )

        assert result_url == tos_url
        assert result_fn == "1.png"
        mock_client.upload_to_tos.assert_awaited_once_with(
            "url", "https://example.com/photo.jpg"
        )
        mock_registry.register.assert_awaited_once_with(tos_url)

    async def test_upload_without_registry(self):
        """无 registry → 只上传不注册, filename 为 None."""
        tos_url = "https://tos.example.com/image.png"

        with patch(
            "app.agents.tools.image.processor.image_client"
        ) as mock_client:
            mock_client.upload_to_tos = AsyncMock(return_value=tos_url)

            result_url, result_fn = await ImageProcessor.upload_and_register(
                source_type="url",
                data="https://example.com/photo.jpg",
                registry=None,
            )

        assert result_url == tos_url
        assert result_fn is None
        mock_client.upload_to_tos.assert_awaited_once()

    async def test_upload_failure_returns_original_url(self, mock_registry):
        """上传异常 → fallback (原始 URL, None)."""
        original_url = "https://example.com/photo.jpg"

        with patch(
            "app.agents.tools.image.processor.image_client"
        ) as mock_client:
            mock_client.upload_to_tos = AsyncMock(
                side_effect=Exception("TOS upload failed")
            )

            result_url, result_fn = await ImageProcessor.upload_and_register(
                source_type="url",
                data=original_url,
                registry=mock_registry,
            )

        assert result_url == original_url
        assert result_fn is None
        mock_registry.register.assert_not_awaited()

    async def test_upload_returns_none_falls_back(self, mock_registry):
        """upload_to_tos 返回 None → fallback (原始数据, None)."""
        original_url = "https://example.com/photo.jpg"

        with patch(
            "app.agents.tools.image.processor.image_client"
        ) as mock_client:
            mock_client.upload_to_tos = AsyncMock(return_value=None)

            result_url, result_fn = await ImageProcessor.upload_and_register(
                source_type="url",
                data=original_url,
                registry=mock_registry,
            )

        assert result_url == original_url
        assert result_fn is None
        mock_registry.register.assert_not_awaited()

    async def test_upload_base64_and_register(self, mock_registry):
        """base64 source_type → upload + register 正常工作."""
        tos_url = "https://tos.example.com/generated.png"
        b64_data = "iVBORw0KGgoAAAANSUhEUg..."

        with patch(
            "app.agents.tools.image.processor.image_client"
        ) as mock_client:
            mock_client.upload_to_tos = AsyncMock(return_value=tos_url)

            result_url, result_fn = await ImageProcessor.upload_and_register(
                source_type="base64",
                data=b64_data,
                registry=mock_registry,
            )

        assert result_url == tos_url
        assert result_fn == "1.png"
        mock_client.upload_to_tos.assert_awaited_once_with("base64", b64_data)
        mock_registry.register.assert_awaited_once_with(tos_url)
