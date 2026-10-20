"""全局 pytest fixtures

提供：
- sqlite3 环境兼容 workaround（Python 3.13 环境缺少 _sqlite3 C 扩展）
- 缓存清理（autouse）
- Langfuse mock
- model_info 工厂
- ``real_pg_required``：没有 docker 时让要真 Postgres 的用例红，不跳过
"""

import importlib
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
# 进程级登记清理 (autouse)
# ---------------------------------------------------------------------------
# 出站队列的登记 / 收件箱登记都是 module-level mutables。前一个测试登记的会污染
# 后续测试 —— 同一个类型再登记一次直接 ValueError。autouse 把每个测试都重置回干净状态。
#
# 要真实插件登记的测试用 ``app_host``（下面），它在测试结束时停掉起过的宿主，
# 宿主停下时撤掉自己登记的一切。
@pytest.fixture(autouse=True)
def _reset_runtime_registries():
    from app.infra import outbound
    from app.messaging.receiving import clear_inboxes

    outbound.clear()
    clear_inboxes()
    yield
    outbound.clear()
    clear_inboxes()


# ---------------------------------------------------------------------------
# app_host — 在测试里起一个 App 的插件宿主（tests/hosting.py）
# ---------------------------------------------------------------------------
@pytest.fixture
async def app_host(monkeypatch):
    """一个函数：``await app_host("world")`` 按 ``app.deployment.APPS`` 的清单起那个 App 的宿主，
    ``app_host("agent-service", [plugin, ...])`` 只起给定的插件（清单还是那个 App 的：插件登记的工具、
    段落、段和步骤按它校验），``manifest=`` 换一份清单；``http=`` 给一个 FastAPI，路由就
    挂在它上面。只跑 setup，不碰数据库、broker，不起钟和后台任务（:func:`tests.hosting.start_without_io`）。

    测试结束时停掉起过的每个宿主，测试失败了也停：宿主停下时撤掉它登记的路由、收件箱、知识来源。
    起宿主会改两样进程级的东西，这里在测试结束时还原：Dynamic Config 的泳道来源，以及 skills
    插件装进去的 guides。
    """
    from inner_shared.dynamic_config import dynamic_config

    from app import deployment
    from app.host import Host
    from app.skills.registry import SkillRegistry
    from tests.hosting import start_without_io

    monkeypatch.setattr(dynamic_config, "_lane_provider", dynamic_config._lane_provider)
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    hosts: list[Host] = []

    async def start(app_name: str, plugins=None, *, http=None, manifest=None) -> Host:
        if plugins is None:
            host = Host.for_app(app_name)
        else:
            if manifest is None:
                manifest = importlib.import_module(deployment.APPS[app_name]).MANIFEST
            host = Host(app_name, plugins, manifest=manifest)
        hosts.append(host)
        await start_without_io(host, http=http)
        return host

    yield start
    for host in reversed(hosts):
        await host.stop()


# ---------------------------------------------------------------------------
# real_pg_required — 没有真 Postgres 就红，不跳过
# ---------------------------------------------------------------------------
# ``test_db_dsn``（tests/runtime/conftest.py）在 docker 不可用时统一 ``pytest.skip``，对大多数
# 套件是对的。但有些用例验的东西只在真库上成立：她的记录的读写契约（提交序、收件去重、到期
# 消费，tests/living）、存储的事务边界和按提交序取号（tests/data）、行为回放
# （tests/replay）、App 启动后建出来的表（tests/apps）。这些静默跳过之后套件绿着过去，等于
# 没有门禁，所以它们的 fixture 先要这一个，把"跳过"翻译成"炸"。
def _docker_unavailable_reason() -> str | None:
    """docker 不可用时给出原因；可用返回 ``None``。"""
    try:
        import docker as _docker
    except Exception as exc:  # pragma: no cover - 依赖缺失路径
        return f"docker SDK 装不上：{exc!r}"
    try:
        _docker.from_env().ping()
    except Exception as exc:
        return f"连不上 docker daemon：{exc!r}"
    return None


@pytest.fixture(scope="session")
def real_pg_required() -> None:
    """没有真 pg 就让要它的用例红，而不是绿着跳过。"""
    reason = _docker_unavailable_reason()
    if reason is not None:
        pytest.fail(
            "这个用例需要真实 Postgres（testcontainers），但 "
            f"{reason}\n"
            "它验的是只在真库上成立的契约（提交序、事务边界、收件去重、到期消费），"
            "静默 skip 会让整个套件绿着过去 —— 等于没有门禁。\n"
            "起 docker 后重跑；确实要在无 docker 的机器上只跑纯函数部分，"
            "显式点名：pytest tests/host/test_turns.py tests/living/test_only_her_own_records.py",
            pytrace=False,
        )


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
# Langfuse 导出到内存 —— agent 层的 trace 测试和回放里的 trace 形状测试共用
# ---------------------------------------------------------------------------
# langfuse 的 client 按 public key 是进程里的单例：第二次按同一个 key 建 client 拿到的是第一次那个，
# span 也只导出到第一次装上的 exporter。所以整个测试进程只能有这一个 session 级的 fixture，
# 放在根上；在别的 conftest 或测试模块里再定义一份，后跑的那批测试就收不到任何 span。
@pytest.fixture(scope="session")
def _in_memory_langfuse():
    """一个 langfuse client，SDK 原样，只是导出的 span 落在内存里、不出网络。

    SDK 自己的 span processor 是批量发往 langfuse 服务端的，换成同步写内存的那个；别的都不动：
    span 挂在谁下面、哪个被标成 trace 的根、generation 上关联了哪个 prompt，全是 SDK 自己写的
    属性。client 按 public key 是单例，整个 session 共用一个，每个测试开始前清空导出的 span。
    """
    from langfuse import Langfuse
    from langfuse._client import resource_manager
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            resource_manager,
            "LangfuseSpanProcessor",
            lambda **_kwargs: SimpleSpanProcessor(exporter),
        )
        client = Langfuse(
            public_key="pk-lf-in-memory-test",
            secret_key="sk-lf-in-memory-test",
            host="https://langfuse.invalid",
            tracer_provider=TracerProvider(),
        )
    yield client, exporter
    client.shutdown()


@pytest.fixture
def exported_spans(_in_memory_langfuse, monkeypatch):
    """agent 公共层开的每一个 span（根、generation、工具）都经这个 client 导出到内存。"""
    client, exporter = _in_memory_langfuse
    exporter.clear()
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)
    monkeypatch.setattr("app.agent.core._get_trace_client", lambda: client)
    return exporter


# ---------------------------------------------------------------------------
# Langfuse prompts from the replay's fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def prompts_from_fixtures(monkeypatch):
    """Langfuse 换成回放用的那份 prompt 快照（``tests/replay/prompts``）：一轮在第一次调模型之前
    取齐它要用的 prompt，并严格核对模板声明的变量（:class:`app.agent.prompts.PromptSet`），所以
    跑一整轮的用例要一份声明对了变量的 prompt，而不是去连 Langfuse。"""
    from tests.replay.harness.prompts import FixtureLangfuse

    fixtures = FixtureLangfuse()
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: fixtures)
    return fixtures
