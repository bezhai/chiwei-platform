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
# Mock setup_logging — 阻止在 main.py 模块导入时创建 /logs 目录
# ---------------------------------------------------------------------------
patch("inner_shared.logger.setup_logging", MagicMock()).start()


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
# WIRING_REGISTRY (list) / 收件箱登记 / emit graph cache 都是 module-level
# mutables。前一个测试登记的 wire 会污染后续测试 —— 下一个 emit() 触发的
# compile_graph 看到残留的 wire，可能直接 GraphError。autouse 把每个测试都重置
# 回干净状态。
#
# 要真实插件登记的测试用 ``app_host``（下面），它在测试结束时停掉起过的宿主，
# 宿主停下时撤掉自己登记的一切。
@pytest.fixture(autouse=True)
def _reset_runtime_registries():
    from app.messaging.receiving import clear_inboxes
    from app.runtime.emit import reset_emit_runtime
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_inboxes()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_inboxes()
    reset_emit_runtime()


# ---------------------------------------------------------------------------
# app_host — 在测试里起一个 App 的插件宿主（tests/hosting.py）
# ---------------------------------------------------------------------------
@pytest.fixture
async def app_host(monkeypatch):
    """一个函数：``await app_host("world")`` 按 ``app.deployment.APPS`` 的清单起那个 App 的宿主，
    ``app_host("agent-service", [plugin, ...])`` 只起给定的插件；``http=`` 给一个 FastAPI，路由就
    挂在它上面。只跑 setup，不碰数据库、broker，不起钟和后台任务（:func:`tests.hosting.start_without_io`）。

    测试结束时停掉起过的每个宿主，测试失败了也停：宿主停下时撤掉它登记的路由、收件箱、知识来源。
    起宿主会改两样进程级的东西，这里在测试结束时还原：Dynamic Config 的泳道来源，以及 skills
    插件装进去的 guides。
    """
    from inner_shared.dynamic_config import dynamic_config

    from app.host import Host
    from app.skills.registry import SkillRegistry
    from tests.hosting import start_without_io

    monkeypatch.setattr(dynamic_config, "_lane_provider", dynamic_config._lane_provider)
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    hosts: list[Host] = []

    async def start(app_name: str, plugins=None, *, http=None) -> Host:
        host = Host.for_app(app_name) if plugins is None else Host(app_name, plugins)
        hosts.append(host)
        await start_without_io(host, http=http)
        return host

    yield start
    for host in reversed(hosts):
        await host.stop()


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


# ---------------------------------------------------------------------------
# capture_emit — Phase 5a node-level test helper
# ---------------------------------------------------------------------------
@pytest.fixture
def capture_emit(monkeypatch):
    """Capture every emit() call into a list. Returns the list.

    Phase 5a helper: most node-level tests want a uniform way to assert
    "what segments / requests this @node emitted." This fixture patches
    BOTH `emit` attribute surfaces so callers using either import style
    get the fake:
      * ``from app.runtime import emit`` (reexport on the package object)
      * ``import app.runtime.emit as X; X.emit(...)`` (module attribute lookup)

    Callers using ``from app.runtime.emit import emit`` bind the function
    locally at import time and cannot be intercepted by this fixture —
    those tests must monkeypatch the caller module's own ``emit`` symbol.
    """
    seen: list = []

    async def _fake_emit(data):
        seen.append(data)

    import sys

    import app.runtime

    emit_mod = sys.modules["app.runtime.emit"]
    monkeypatch.setattr(emit_mod, "emit", _fake_emit)
    monkeypatch.setattr(app.runtime, "emit", _fake_emit)
    return seen
