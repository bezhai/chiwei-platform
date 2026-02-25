"""test_langfuse.py — Langfuse 惰性初始化 + prompt 缓存测试

场景覆盖：
- 惰性初始化：import 时不创建客户端
- 单例保证：多次调用 get_client() 返回同一实例
- get_prompt 传递 cache_ttl_seconds 给 SDK
"""

from unittest.mock import MagicMock, patch

import pytest

import app.agents.infra.langfuse_client as langfuse_mod

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_langfuse_singleton():
    """每个测试前后重置 Langfuse 单例"""
    original = langfuse_mod._client
    langfuse_mod._client = None
    yield
    langfuse_mod._client = original


class TestLazyInit:
    """惰性初始化"""

    def test_client_is_none_on_import(self):
        """import 模块后 _client 应为 None（已被 autouse fixture 重置）"""
        assert langfuse_mod._client is None

    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_get_client_creates_instance(self, mock_langfuse_cls):
        """首次 get_client() 应创建 Langfuse 实例"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        client = langfuse_mod.get_client()

        assert client is mock_instance
        mock_langfuse_cls.assert_called_once()


class TestSingleton:
    """单例保证"""

    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_multiple_calls_return_same_instance(self, mock_langfuse_cls):
        """多次调用 get_client() 应返回同一实例"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        c1 = langfuse_mod.get_client()
        c2 = langfuse_mod.get_client()

        assert c1 is c2
        assert mock_langfuse_cls.call_count == 1  # 只创建一次


class TestGetPrompt:
    """get_prompt 缓存参数传递"""

    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_default_cache_ttl(self, mock_langfuse_cls):
        """默认 cache_ttl_seconds=300"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label=None,
            cache_ttl_seconds=300,
        )

    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_custom_cache_ttl(self, mock_langfuse_cls):
        """自定义 cache_ttl_seconds"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt", cache_ttl_seconds=60)

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label=None,
            cache_ttl_seconds=60,
        )

    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_label_passthrough(self, mock_langfuse_cls):
        """label 参数正确传递"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt", label="production")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label="production",
            cache_ttl_seconds=300,
        )
