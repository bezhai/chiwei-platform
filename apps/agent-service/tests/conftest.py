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
    from app.agent.models import clear_model_info_cache

    clear_model_info_cache()
    yield
    clear_model_info_cache()


# ---------------------------------------------------------------------------
# Runtime 注册表清理 (autouse)
# ---------------------------------------------------------------------------
# WIRING_REGISTRY (list) / placement bindings (dict) / emit graph cache 都是
# module-level mutables。前一个测试声明的 wire（比如 .debounce() 这种未实现边
# 的 regression 检查）会污染后续测试 —— 下一个 emit() 触发的 compile_graph
# 看到残留的 .debounce() 直接 GraphError。autouse 把每个测试都重置回干净状态。
#
# tests/wiring/ 里的测试需要真实生产 wiring，它们在自己的 setup 里调
# importlib.reload(app.wiring.memory) 重新执行 module body，把 wire/bind 调用
# 重新跑一遍 —— 跟 autouse 的清理顺序兼容。
@pytest.fixture(autouse=True)
def _reset_runtime_registries():
    from app.runtime.emit import reset_emit_runtime
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


# ---------------------------------------------------------------------------
# Langfuse mock — 阻止真实 HTTP 请求
# ---------------------------------------------------------------------------
@pytest.fixture()
def mock_langfuse_client():
    """Mock Langfuse client，阻止真实 HTTP 调用"""
    mock_client = MagicMock()
    with patch("app.agent.prompts._client", mock_client):
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
        use_proxy: bool = False,
        **overrides: Any,
    ) -> dict[str, Any]:
        info = {
            "model_id": model_id,
            "model_name": model_name,
            "api_key": api_key,
            "base_url": base_url,
            "client_type": client_type,
            "is_active": is_active,
            "use_proxy": use_proxy,
        }
        info.update(overrides)
        return info

    return _factory
