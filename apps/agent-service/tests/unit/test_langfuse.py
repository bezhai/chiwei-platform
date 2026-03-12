"""test_langfuse.py — Langfuse 惰性初始化 + prompt 缓存 + 泳道路由测试

场景覆盖：
- 惰性初始化：import 时不创建客户端
- 单例保证：多次调用 get_client() 返回同一实例
- get_prompt 传递 cache_ttl_seconds 给 SDK
- 泳道路由：非 prod 泳道尝试 label=lane，失败 fallback production
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

    @patch("app.agents.infra.langfuse_client.get_lane", return_value=None)
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_default_cache_ttl(self, mock_langfuse_cls, _mock_lane):
        """默认 cache_ttl_seconds=60"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label=None,
            cache_ttl_seconds=60,
        )

    @patch("app.agents.infra.langfuse_client.get_lane", return_value=None)
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_custom_cache_ttl(self, mock_langfuse_cls, _mock_lane):
        """自定义 cache_ttl_seconds"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt", cache_ttl_seconds=30)

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label=None,
            cache_ttl_seconds=30,
        )

    @patch("app.agents.infra.langfuse_client.get_lane", return_value=None)
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_label_passthrough(self, mock_langfuse_cls, _mock_lane):
        """显式 label 参数正确传递"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt", label="production")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label="production",
            cache_ttl_seconds=60,
        )


class TestLaneRouting:
    """泳道 prompt 路由"""

    @patch("app.agents.infra.langfuse_client.get_lane", return_value="feat-v1")
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_lane_label_used(self, mock_langfuse_cls, _mock_lane):
        """非 prod 泳道使用 lane 作为 label"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label="feat-v1",
            cache_ttl_seconds=60,
        )

    @patch("app.agents.infra.langfuse_client.get_lane", return_value="feat-v1")
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_lane_fallback_production(self, mock_langfuse_cls, _mock_lane):
        """泳道 label 不存在时 fallback production"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance
        mock_instance.get_prompt.side_effect = [Exception("not found"), MagicMock()]

        result = langfuse_mod.get_prompt("test-prompt")

        assert result is not None
        assert mock_instance.get_prompt.call_count == 2
        calls = mock_instance.get_prompt.call_args_list
        assert calls[0].args == ("test-prompt",)
        assert calls[0].kwargs == {"label": "feat-v1", "cache_ttl_seconds": 60}
        assert calls[1].args == ("test-prompt",)
        assert calls[1].kwargs == {"label": None, "cache_ttl_seconds": 60}

    @patch("app.agents.infra.langfuse_client.get_lane", return_value="prod")
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_prod_lane_no_override(self, mock_langfuse_cls, _mock_lane):
        """prod 泳道不做额外路由"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label=None,
            cache_ttl_seconds=60,
        )

    @patch("app.agents.infra.langfuse_client.get_lane", return_value="feat-v1")
    @patch("app.agents.infra.langfuse_client.Langfuse")
    def test_explicit_label_skips_lane(self, mock_langfuse_cls, _mock_lane):
        """显式传 label 时不注入泳道"""
        mock_instance = MagicMock()
        mock_langfuse_cls.return_value = mock_instance

        langfuse_mod.get_prompt("test-prompt", label="staging")

        mock_instance.get_prompt.assert_called_once_with(
            "test-prompt",
            label="staging",
            cache_ttl_seconds=60,
        )
