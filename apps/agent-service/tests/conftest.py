"""全局 pytest fixtures

提供：
- sqlite3 环境兼容 workaround（Python 3.13 环境缺少 _sqlite3 C 扩展）
- 缓存清理（autouse）
- Langfuse mock
- model_info 工厂
"""

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 环境兼容：mock sqlite3 相关模块（容器环境可能缺少 _sqlite3 C 扩展）
# 必须在任何 app 模块导入之前执行
# ---------------------------------------------------------------------------
_sqlite3_mock = MagicMock()
_sqlite3_mock.sqlite_version = "3.45.0"
_sqlite3_mock.sqlite_version_info = (3, 45, 0)

for mod_name in ("_sqlite3", "sqlite3", "sqlite3.dbapi2"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = _sqlite3_mock


# ---------------------------------------------------------------------------
# 缓存清理 (autouse) — 每个测试前后清空 ModelBuilder 缓存
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_model_cache():
    """每个测试前后清空 ModelBuilder 的 model_info 缓存"""
    from app.agents.infra.model_builder import clear_model_info_cache

    clear_model_info_cache()
    yield
    clear_model_info_cache()


# ---------------------------------------------------------------------------
# Langfuse mock — 阻止真实 HTTP 请求
# ---------------------------------------------------------------------------
@pytest.fixture()
def mock_langfuse_client():
    """Mock Langfuse client，阻止真实 HTTP 调用"""
    mock_client = MagicMock()
    with patch("app.agents.infra.langfuse_client._client", mock_client):
        yield mock_client


# ---------------------------------------------------------------------------
# model_info 工厂 — 快速创建测试用模型信息字典
# ---------------------------------------------------------------------------
@pytest.fixture()
def model_info_factory():
    """返回一个工厂函数，用于创建测试用 model_info dict"""

    def _factory(
        *,
        model_id: str = "test-model",
        model_name: str = "gpt-4o-mini",
        api_key: str = "sk-test-key",
        base_url: str = "https://api.test.com/v1",
        client_type: str = "openai-http",
        is_active: bool = True,
        **overrides: Any,
    ) -> dict[str, Any]:
        info = {
            "model_id": model_id,
            "model_name": model_name,
            "api_key": api_key,
            "base_url": base_url,
            "client_type": client_type,
            "is_active": is_active,
        }
        info.update(overrides)
        return info

    return _factory
