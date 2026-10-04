"""tests/agent 共用的 fixture：真的 langfuse SDK，span 导出到内存。"""

from __future__ import annotations

import pytest
from langfuse import Langfuse
from langfuse._client import resource_manager
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture(scope="session")
def _in_memory_langfuse():
    """一个 langfuse client，SDK 原样，只是导出的 span 落在内存里、不出网络。

    SDK 自己的 span processor 是批量发往 langfuse 服务端的，换成同步写内存的那个；别的都不动：
    span 挂在谁下面、哪个被标成 trace 的根、generation 上关联了哪个 prompt，全是 SDK 自己写的
    属性。client 按 public key 是单例，整个 session 共用一个，每个测试开始前清空导出的 span。
    """
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
def exported_spans(_in_memory_langfuse, monkeypatch) -> InMemorySpanExporter:
    """agent 公共层开的每一个 span（根、generation、工具）都经这个 client 导出到内存。"""
    client, exporter = _in_memory_langfuse
    exporter.clear()
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)
    monkeypatch.setattr("app.agent.core._get_trace_client", lambda: client)
    return exporter
