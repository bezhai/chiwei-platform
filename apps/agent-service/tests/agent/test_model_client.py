"""T1 — ModelClient interface + model resolution seam.

Two things are pinned here:

1. The ``ModelClient`` abstract interface must serve the three consumptions
   ``Agent.run / stream / extract`` need (read from app/agent/core.py):
     - ``complete`` — non-streaming, returns a final assistant Message,
     - ``stream``   — yields neutral StreamChunks,
     - ``structured`` — single structured output (a dict the caller validates
       against its pydantic model in extract()).

2. ``build_model_client`` reuses the existing DB resolution
   (``resolve_model_info``) and dispatches by ``client_type`` to an adapter.
   T1 has no real adapter yet (OpenAI is T2, Gemini is T4), so:
     - real client_types raise NotImplementedError,
     - a registered *fake* adapter proves the dispatch + resolution wiring.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

from app.agent.client import (
    ModelClient,
    build_model_client,
    register_adapter,
)
from app.agent.neutral import Message, Role, StreamChunk, ToolDef


# ---------------------------------------------------------------------------
# Interface shape: the three consumptions run/stream/extract require
# ---------------------------------------------------------------------------


def test_model_client_exposes_three_consumptions():
    for name in ("complete", "stream", "structured"):
        assert hasattr(ModelClient, name), f"ModelClient missing {name}"


def test_complete_signature_takes_messages_and_tools():
    sig = inspect.signature(ModelClient.complete)
    params = sig.parameters
    assert "messages" in params
    assert "tools" in params  # tools optional → run() with/without tools


def test_stream_signature_takes_messages_and_tools():
    sig = inspect.signature(ModelClient.stream)
    assert "messages" in sig.parameters
    assert "tools" in sig.parameters


def test_structured_signature_takes_messages_and_schema():
    sig = inspect.signature(ModelClient.structured)
    params = sig.parameters
    assert "messages" in params
    # extract() needs to pass the response json-schema down
    assert "schema" in params


# ---------------------------------------------------------------------------
# Fake adapter proving the seam end-to-end (messages in → neutral out)
# ---------------------------------------------------------------------------


class _FakeAdapter(ModelClient):
    """Echo adapter: proves the neutral contract without any real provider."""

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str | None,
        **extra: object,
    ):
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.extra = extra

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        **kwargs: object,
    ) -> Message:
        last = messages[-1]
        return Message(
            role=Role.ASSISTANT,
            content=f"echo:{last.text()}",
            reasoning_content="faked-reasoning",
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        **kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(reasoning="thinking")
        yield StreamChunk(text="hel")
        yield StreamChunk(text="lo")
        yield StreamChunk(finish_reason="stop")

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: dict,
        **kwargs: object,
    ) -> dict:
        return {"ok": True, "schema_seen": schema.get("title", "")}


@pytest.fixture
def _fake_provider(monkeypatch):
    """Make resolve_model_info return a fake provider on client_type='fake'."""
    register_adapter("fake", _FakeAdapter)

    async def _fake_resolve(model_id, *, required_fields=()):
        return {
            "model_name": "fake-model",
            "api_key": "sk-fake",
            "base_url": "https://fake.local",
            "is_active": True,
            "client_type": "fake",
            "use_proxy": False,
        }

    monkeypatch.setattr("app.agent.client.resolve_model_info", _fake_resolve)


async def test_build_model_client_resolves_and_dispatches_to_adapter(
    _fake_provider,
):
    client = await build_model_client("whatever")
    assert isinstance(client, _FakeAdapter)
    assert client.model_name == "fake-model"
    assert client.api_key == "sk-fake"
    assert client.base_url == "https://fake.local"


async def test_complete_returns_neutral_assistant_message(_fake_provider):
    client = await build_model_client("whatever")
    out = await client.complete([Message(role=Role.USER, content="ping")])
    assert out.role == Role.ASSISTANT
    assert out.content == "echo:ping"
    assert out.reasoning_content == "faked-reasoning"


async def test_stream_yields_neutral_chunks(_fake_provider):
    client = await build_model_client("whatever")
    chunks = [
        c async for c in client.stream([Message(role=Role.USER, content="hi")])
    ]
    assert chunks[0].reasoning == "thinking"
    assert "".join(c.text for c in chunks if c.text) == "hello"
    assert chunks[-1].finish_reason == "stop"


async def test_structured_returns_dict_for_extract(_fake_provider):
    client = await build_model_client("whatever")
    out = await client.structured(
        [Message(role=Role.USER, content="q")],
        schema={"title": "Verdict", "type": "object"},
    )
    assert out == {"ok": True, "schema_seen": "Verdict"}


# ---------------------------------------------------------------------------
# Adapter availability: OpenAI family lands in T2, Gemini (google) in T4.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_type", ["openai", "azure-http", "deepseek"])
async def test_openai_family_adapters_implemented(monkeypatch, client_type):
    """T2 implements the OpenAI-family client_types; the seam dispatches them.

    The SDK clients are stubbed so no network is touched; we only assert the
    resolution seam now finds a real adapter (no NotImplementedError).
    """
    import app.agent.adapters  # noqa: F401 - registers OpenAI-family adapters
    import app.agent.adapters.openai as openai_mod

    class _StubClient:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(openai_mod, "AsyncOpenAI", _StubClient)
    monkeypatch.setattr(openai_mod, "AsyncAzureOpenAI", _StubClient)

    async def _resolve(model_id, *, required_fields=()):
        return {
            "model_name": "m",
            "api_key": "k",
            "base_url": "https://x",
            "is_active": True,
            "client_type": client_type,
            "use_proxy": False,
        }

    monkeypatch.setattr("app.agent.client.resolve_model_info", _resolve)

    client = await build_model_client("whatever")
    assert isinstance(client, openai_mod.OpenAIAdapter)


async def test_gemini_adapter_implemented(monkeypatch):
    """T3 implements the ``google`` client_type; the seam dispatches it.

    The genai client is stubbed so no network is touched; we only assert the
    resolution seam now finds a real adapter (no NotImplementedError).
    """
    import app.agent.adapters  # noqa: F401 - registers the Gemini adapter
    import app.agent.adapters.gemini as gemini_mod

    class _StubClient:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(gemini_mod.genai, "Client", _StubClient)

    async def _resolve(model_id, *, required_fields=()):
        return {
            "model_name": "m",
            "api_key": "k",
            "base_url": "https://x",
            "is_active": True,
            "client_type": "google",
            "use_proxy": False,
        }

    monkeypatch.setattr("app.agent.client.resolve_model_info", _resolve)

    client = await build_model_client("whatever")
    assert isinstance(client, gemini_mod.GeminiAdapter)
