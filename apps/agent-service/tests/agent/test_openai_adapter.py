"""T2 — OpenAI-family adapter (azure / deepseek variants + structured + trace).

The adapter translates neutral types (``app.agent.neutral``) to OpenAI
chat-completions wire and back, using the ``openai`` SDK. The dev box has no
network egress to providers and production keys must not be extracted, so every
test mocks the SDK: we hand the adapter a *canned* AsyncOpenAI whose
``chat.completions.create`` returns hand-built response / chunk objects, then
assert the adapter's neutral translation.

Coverage (spec §T2 Verification, adapted to mocked transport):
  - plain text round-trip neutral→wire→neutral,
  - tool_calls round-trip,
  - multimodal image content block survives the wire build,
  - deepseek reasoning_content both directions (extract out / re-inject in +
    string normalisation),
  - structured output → dict,
  - streaming chunks (text / reasoning / finish_reason / tool boundary),
  - use_proxy wires settings.forward_proxy_url into the SDK http client,
  - SDK auto-retry is disabled (max_retries=0),
  - a generation span is produced even with update_trace=False.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.adapters.openai import OpenAIAdapter
from app.agent.neutral import ContentBlock, Message, Role, ToolCall, ToolDef

# ---------------------------------------------------------------------------
# Canned SDK response builders (mimic openai SDK object shapes)
# ---------------------------------------------------------------------------


def _usage(prompt: int = 5, completion: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def _completion(
    *,
    content: str | None = "hi there",
    reasoning_content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    message = SimpleNamespace(
        role="assistant",
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason, index=0)
    return SimpleNamespace(
        choices=[choice],
        usage=_usage(),
        model="canned-model",
    )


def _tool_call_obj(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason, index=0)
    return SimpleNamespace(choices=[choice], usage=None)


def _tool_call_delta(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


# ---------------------------------------------------------------------------
# Mock AsyncOpenAI: captures kwargs, returns the canned object the test sets
# ---------------------------------------------------------------------------


class _MockAsyncOpenAI:
    """Stand-in for openai.AsyncOpenAI capturing the create() call."""

    def __init__(self, **kwargs: Any):
        self.init_kwargs = kwargs
        self.last_create_kwargs: dict[str, Any] | None = None
        self._next_result: Any = None
        self._stream_chunks: list[Any] | None = None

        async def _create(**kw: Any) -> Any:
            self.last_create_kwargs = kw
            if kw.get("stream"):
                chunks = self._stream_chunks or []

                async def _gen() -> Any:
                    for c in chunks:
                        yield c

                return _gen()
            return self._next_result

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=_create)
        )

    def set_result(self, result: Any) -> None:
        self._next_result = result

    def set_stream(self, chunks: list[Any]) -> None:
        self._stream_chunks = chunks


@pytest.fixture
def mock_sdk(monkeypatch):
    """Patch the adapter's AsyncOpenAI / AsyncAzureOpenAI with a capturing mock.

    Returns a holder whose ``.instance`` is set to the constructed mock so
    tests can read init kwargs and set canned results.
    """
    holder = SimpleNamespace(instance=None, azure_instance=None)

    def _make_openai(**kwargs: Any) -> _MockAsyncOpenAI:
        m = _MockAsyncOpenAI(**kwargs)
        holder.instance = m
        return m

    def _make_azure(**kwargs: Any) -> _MockAsyncOpenAI:
        m = _MockAsyncOpenAI(**kwargs)
        holder.azure_instance = m
        return m

    monkeypatch.setattr("app.agent.adapters.openai.AsyncOpenAI", _make_openai)
    monkeypatch.setattr("app.agent.adapters.openai.AsyncAzureOpenAI", _make_azure)
    # neutralise the langfuse trace helper so tests don't hit network
    monkeypatch.setattr(
        "app.agent.adapters.openai.generation_span", _fake_generation_span
    )
    return holder


# A recording stand-in for the trace helper context manager
_span_calls: list[dict[str, Any]] = []
_MOST_RECENT_SPAN: list[Any] = []


class _FakeSpan:
    def __init__(self, kwargs: dict[str, Any]):
        self.kwargs = kwargs
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kw: Any) -> None:
        self.updates.append(kw)

    def end(self) -> None:
        self.ended = True


class _fake_generation_span:  # noqa: N801 - mimics a ctx-manager factory
    def __init__(self, **kwargs: Any):
        self.span = _FakeSpan(kwargs)
        _span_calls.append(kwargs)
        _MOST_RECENT_SPAN.append(self.span)

    def __enter__(self) -> _FakeSpan:
        return self.span

    def __exit__(self, *exc: Any) -> bool:
        self.span.end()
        return False


@pytest.fixture(autouse=True)
def _reset_span_calls():
    _span_calls.clear()
    _MOST_RECENT_SPAN.clear()
    yield
    _span_calls.clear()
    _MOST_RECENT_SPAN.clear()


# ---------------------------------------------------------------------------
# complete() — plain text round-trip
# ---------------------------------------------------------------------------


async def test_complete_plain_text_roundtrip(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="hello world"))

    out = await adapter.complete([Message(role=Role.USER, content="hi")])

    assert out.role == Role.ASSISTANT
    assert out.content == "hello world"
    # wire request carried the user message
    sent = mock_sdk.instance.last_create_kwargs
    assert sent["model"] == "gpt-4o"
    assert sent["messages"][-1] == {"role": "user", "content": "hi"}
    assert sent.get("stream") is not True


async def test_complete_tool_calls_roundtrip(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(
        _completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call_obj("call_1", "search", '{"q": "cats"}'),
            ],
        )
    )

    tools = [
        ToolDef(
            name="search",
            description="search the web",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        )
    ]
    out = await adapter.complete(
        [Message(role=Role.USER, content="find cats")], tools=tools
    )

    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "search"
    assert tc.arguments == {"q": "cats"}

    # tools were translated to function-calling schema on the wire
    sent = mock_sdk.instance.last_create_kwargs
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["function"]["name"] == "search"
    assert sent["tools"][0]["function"]["parameters"]["type"] == "object"


async def test_complete_sends_assistant_tool_call_and_tool_result(mock_sdk):
    """An assistant turn with tool_calls + a following tool result serialise."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="done"))

    history = [
        Message(role=Role.USER, content="find cats"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="call_1", name="search", arguments={"q": "cats"})],
        ),
        Message(role=Role.TOOL, content="3 results", tool_call_id="call_1"),
    ]
    await adapter.complete(history)

    sent = mock_sdk.instance.last_create_kwargs["messages"]
    assistant_msg = sent[1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "call_1"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "search"
    # arguments serialised back to a JSON string on the wire
    import json

    assert json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"]) == {
        "q": "cats"
    }
    tool_msg = sent[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    assert tool_msg["content"] == "3 results"


async def test_complete_tool_call_with_malformed_json_args(mock_sdk):
    """Malformed tool-call arguments degrade to {} (and are logged), not crash.

    This pins the documented contract: the adapter never raises on bad JSON
    from the model; the empty-dict fallback is a deliberate, visible choice.
    """
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(
        _completion(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[_tool_call_obj("call_1", "search", "{not valid json")],
        )
    )

    out = await adapter.complete([Message(role=Role.USER, content="x")])
    assert out.tool_calls[0].arguments == {}


async def test_complete_handles_none_content(mock_sdk):
    """A response with content=None (pure tool call) yields '' content, not None."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content=None, finish_reason="stop"))
    out = await adapter.complete([Message(role=Role.USER, content="x")])
    assert out.content == ""


async def test_complete_multimodal_image_block_survives_wire(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="a cat"))

    msg = Message(
        role=Role.USER,
        content=[
            ContentBlock.from_text("what is this?"),
            ContentBlock.from_image_url(
                {"url": "https://img/cat.png", "detail": "auto"}
            ),
        ],
    )
    await adapter.complete([msg])

    sent = mock_sdk.instance.last_create_kwargs["messages"][-1]
    parts = sent["content"]
    assert {"type": "text", "text": "what is this?"} in parts
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"] == {"url": "https://img/cat.png", "detail": "auto"}


async def test_tool_result_image_url_block_survives_wire(mock_sdk):
    """A tool that returns an image_url block keeps it on the tool message wire.

    Image-returning tools (e.g. a vision tool) attach an OpenAI image_url block
    to their ToolResult; it must reach the wire as an image_url content part,
    not be dropped or reshaped.
    """
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="seen"))

    history = [
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="vision", arguments={})],
        ),
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("here is the image"),
                ContentBlock.from_image_url({"url": "https://img/out.png"}),
            ],
        ),
    ]
    await adapter.complete(history)

    tool_msg = mock_sdk.instance.last_create_kwargs["messages"][-1]
    assert tool_msg["role"] == "tool"
    parts = tool_msg["content"]
    img = next(p for p in parts if p["type"] == "image_url")
    assert img["image_url"] == {"url": "https://img/out.png"}


async def test_deepseek_normalises_tool_message_array_content(mock_sdk):
    """Under deepseek, a tool message's array content collapses to text.

    deepseek rejects array/null content on ANY role, including tool. The image
    block contributes no text; only the text block survives.
    """
    adapter = OpenAIAdapter(
        model_name="deepseek-reasoner",
        api_key="sk",
        base_url="https://ds",
        client_type="deepseek",
    )
    mock_sdk.instance.set_result(_completion(content="ok"))

    history = [
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("result text"),
                ContentBlock.from_image_url({"url": "https://img/x.png"}),
            ],
        ),
    ]
    await adapter.complete(history)

    tool_msg = mock_sdk.instance.last_create_kwargs["messages"][-1]
    assert tool_msg["content"] == "result text"
    assert isinstance(tool_msg["content"], str)


async def test_complete_chat_history_image_block_becomes_image_url(mock_sdk):
    """``image`` blocks (build_*_messages shape) become OpenAI image_url."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="ok"))

    msg = Message(
        role=Role.USER,
        content=[
            ContentBlock.from_text("look"),
            ContentBlock.from_image(url="https://img/dog.png"),
        ],
    )
    await adapter.complete([msg])

    parts = mock_sdk.instance.last_create_kwargs["messages"][-1]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"] == "https://img/dog.png"


# ---------------------------------------------------------------------------
# deepseek reasoning_content — both directions
# ---------------------------------------------------------------------------


async def test_deepseek_extracts_reasoning_content_from_response(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="deepseek-reasoner",
        api_key="sk",
        base_url="https://ds",
        client_type="deepseek",
    )
    mock_sdk.instance.set_result(
        _completion(content="the answer is 42", reasoning_content="let me think...")
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])

    assert out.content == "the answer is 42"
    assert out.reasoning_content == "let me think..."


async def test_deepseek_reinjects_reasoning_and_normalises_content(mock_sdk):
    """Sending an assistant msg with reasoning_content back to deepseek:

    reasoning_content is re-injected into the wire message AND every message's
    content is normalised to a plain string (deepseek rejects arrays/null).
    """
    adapter = OpenAIAdapter(
        model_name="deepseek-reasoner",
        api_key="sk",
        base_url="https://ds",
        client_type="deepseek",
    )
    mock_sdk.instance.set_result(_completion(content="ok"))

    history = [
        Message(
            role=Role.USER,
            content=[
                ContentBlock.from_text("hello "),
                ContentBlock.from_text("there"),
            ],
        ),
        Message(
            role=Role.ASSISTANT,
            content="hi",
            reasoning_content="prior thinking",
        ),
        Message(role=Role.USER, content="continue"),
    ]
    await adapter.complete(history)

    sent = mock_sdk.instance.last_create_kwargs["messages"]
    # 1) array content normalised to a plain string
    assert sent[0]["content"] == "hello there"
    assert isinstance(sent[0]["content"], str)
    # 2) reasoning_content re-injected on assistant message
    assert sent[1]["reasoning_content"] == "prior thinking"
    assert sent[1]["content"] == "hi"


async def test_non_deepseek_does_not_inject_reasoning(mock_sdk):
    """Plain openai must NOT emit reasoning_content on the wire."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="ok"))

    await adapter.complete(
        [Message(role=Role.ASSISTANT, content="hi", reasoning_content="thinking")]
    )

    sent = mock_sdk.instance.last_create_kwargs["messages"][-1]
    assert "reasoning_content" not in sent


# ---------------------------------------------------------------------------
# structured() — response_format json_schema → dict
# ---------------------------------------------------------------------------


async def test_structured_returns_dict(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(
        _completion(content='{"is_injection": true, "confidence": 0.9}')
    )

    schema = {
        "title": "InjectionResult",
        "type": "object",
        "properties": {
            "is_injection": {"type": "boolean"},
            "confidence": {"type": "number"},
        },
        "required": ["is_injection", "confidence"],
    }
    out = await adapter.structured(
        [Message(role=Role.USER, content="check this")], schema=schema
    )

    assert out == {"is_injection": True, "confidence": 0.9}

    sent = mock_sdk.instance.last_create_kwargs
    rf = sent["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "InjectionResult"
    assert rf["json_schema"]["schema"] == schema


async def test_structured_passes_through_reasoning_effort(mock_sdk):
    """Guards call extract with reasoning_effort kwarg — it reaches the wire."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content='{"ok": true}'))

    await adapter.structured(
        [Message(role=Role.USER, content="q")],
        schema={"title": "X", "type": "object"},
        reasoning_effort="low",
    )

    assert mock_sdk.instance.last_create_kwargs["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# stream() — neutral chunk emission
# ---------------------------------------------------------------------------


async def test_stream_emits_text_reasoning_finish(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="deepseek-reasoner",
        api_key="sk",
        base_url="https://ds",
        client_type="deepseek",
    )
    mock_sdk.instance.set_stream(
        [
            _chunk(reasoning_content="think"),
            _chunk(content="hel"),
            _chunk(content="lo"),
            _chunk(finish_reason="stop"),
        ]
    )

    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="hi")])]

    assert chunks[0].reasoning == "think"
    assert "".join(c.text for c in chunks if c.text) == "hello"
    assert chunks[-1].finish_reason == "stop"
    # stream=True was set on the wire
    assert mock_sdk.instance.last_create_kwargs["stream"] is True


async def test_stream_emits_tool_call_boundary(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_stream(
        [
            _chunk(content="let me search"),
            _chunk(
                tool_calls=[
                    _tool_call_delta(0, call_id="call_1", name="search", arguments="")
                ]
            ),
            _chunk(tool_calls=[_tool_call_delta(0, arguments='{"q":')]),
            _chunk(tool_calls=[_tool_call_delta(0, arguments='"cats"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )

    tools = [ToolDef(name="search", description="d", parameters={"type": "object"})]
    chunks = [
        c
        async for c in adapter.stream(
            [Message(role=Role.USER, content="find cats")], tools=tools
        )
    ]

    # text emitted
    assert "".join(c.text for c in chunks if c.text) == "let me search"
    # a tool_call boundary chunk surfaced with the assembled call
    tool_chunks = [c for c in chunks if c.tool_call is not None]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_call.id == "call_1"
    assert tool_chunks[0].tool_call.name == "search"
    assert tool_chunks[0].tool_call.arguments == {"q": "cats"}
    # finish_reason surfaced
    assert chunks[-1].finish_reason == "tool_calls"


async def test_stream_emits_content_filter_finish(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_stream(
        [_chunk(content="partial"), _chunk(finish_reason="content_filter")]
    )

    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="x")])]
    assert chunks[-1].finish_reason == "content_filter"


async def test_stream_emits_parallel_tool_calls(mock_sdk):
    """Two parallel tool calls (index 0 + 1) must BOTH surface, exactly once.

    Index 0 finishes (fragments contiguous) when index 1 begins; index 1
    finishes at finish_reason. Neither may be dropped or double-emitted.
    """
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_stream(
        [
            _chunk(
                tool_calls=[
                    _tool_call_delta(0, call_id="call_0", name="search", arguments="")
                ]
            ),
            _chunk(tool_calls=[_tool_call_delta(0, arguments='{"q": "a"}')]),
            _chunk(
                tool_calls=[
                    _tool_call_delta(1, call_id="call_1", name="lookup", arguments="")
                ]
            ),
            _chunk(tool_calls=[_tool_call_delta(1, arguments='{"id": 7}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )

    tools = [
        ToolDef(name="search", description="d", parameters={"type": "object"}),
        ToolDef(name="lookup", description="d", parameters={"type": "object"}),
    ]
    chunks = [
        c
        async for c in adapter.stream(
            [Message(role=Role.USER, content="do both")], tools=tools
        )
    ]

    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    assert len(tool_calls) == 2
    by_id = {tc.id: tc for tc in tool_calls}
    assert by_id["call_0"].name == "search"
    assert by_id["call_0"].arguments == {"q": "a"}
    assert by_id["call_1"].name == "lookup"
    assert by_id["call_1"].arguments == {"id": 7}


async def test_stream_emits_tool_calls_in_one_chunk(mock_sdk):
    """Multiple tool-call deltas arriving in a SINGLE chunk both surface."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_stream(
        [
            _chunk(
                tool_calls=[
                    _tool_call_delta(
                        0, call_id="c0", name="a", arguments='{"x": 1}'
                    ),
                    _tool_call_delta(
                        1, call_id="c1", name="b", arguments='{"y": 2}'
                    ),
                ]
            ),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="x")])]
    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    assert {tc.id for tc in tool_calls} == {"c0", "c1"}
    assert {tc.id: tc.arguments for tc in tool_calls} == {
        "c0": {"x": 1},
        "c1": {"y": 2},
    }


async def test_stream_handles_empty_choices_chunk(mock_sdk):
    """A keepalive / usage-only chunk with no choices must be skipped safely."""
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    empty = SimpleNamespace(choices=[], usage=None)
    mock_sdk.instance.set_stream(
        [empty, _chunk(content="hi"), _chunk(finish_reason="stop")]
    )
    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="x")])]
    assert "".join(c.text for c in chunks if c.text) == "hi"


async def test_stream_requests_usage_and_records_it(mock_sdk):
    """Streaming must opt into usage (stream_options) and record it on the span.

    OpenAI only emits a usage-bearing final chunk when
    ``stream_options={"include_usage": True}`` is set; otherwise token counts
    never arrive and langfuse accounting is silently lost. The adapter must
    request it AND surface the final chunk's usage as usage_details.
    """
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    usage_chunk = SimpleNamespace(choices=[], usage=_usage(prompt=11, completion=22))
    mock_sdk.instance.set_stream(
        [
            _chunk(content="hi"),
            _chunk(finish_reason="stop"),
            usage_chunk,
        ]
    )

    async for _ in adapter.stream([Message(role=Role.USER, content="x")]):
        pass

    # 1) the wire request opted into usage reporting
    assert mock_sdk.instance.last_create_kwargs["stream_options"] == {
        "include_usage": True
    }
    # 2) the span recorded the usage from the final chunk
    span = _MOST_RECENT_SPAN[-1]
    usage_updates = [u for u in span.updates if "usage_details" in u]
    assert usage_updates, "stream span never recorded usage_details"
    assert usage_updates[-1]["usage_details"] == {
        "input": 11,
        "output": 22,
        "total": 33,
    }


async def test_stream_interleaved_tool_call_indices_reassemble(mock_sdk):
    """Interleaved tool-call delta indices (0,1,0,1) must both reassemble fully.

    The contiguous-by-index assumption is an *optimisation*; the assembler must
    still produce two complete calls when fragments for index 0 and index 1
    arrive interleaved (a delta for 0, then 1, then 0 again, then 1). Neither
    call may lose argument fragments or be dropped.
    """
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_stream(
        [
            _chunk(
                tool_calls=[
                    _tool_call_delta(0, call_id="c0", name="search", arguments='{"q":')
                ]
            ),
            _chunk(
                tool_calls=[
                    _tool_call_delta(1, call_id="c1", name="lookup", arguments='{"id":')
                ]
            ),
            _chunk(tool_calls=[_tool_call_delta(0, arguments=' "cats"}')]),
            _chunk(tool_calls=[_tool_call_delta(1, arguments=" 7}")]),
            _chunk(finish_reason="tool_calls"),
        ]
    )

    tools = [
        ToolDef(name="search", description="d", parameters={"type": "object"}),
        ToolDef(name="lookup", description="d", parameters={"type": "object"}),
    ]
    chunks = [
        c
        async for c in adapter.stream(
            [Message(role=Role.USER, content="do both")], tools=tools
        )
    ]

    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    by_id = {tc.id: tc for tc in tool_calls}
    assert set(by_id) == {"c0", "c1"}
    assert by_id["c0"].arguments == {"q": "cats"}
    assert by_id["c1"].arguments == {"id": 7}


# ---------------------------------------------------------------------------
# proxy / retry / azure construction
# ---------------------------------------------------------------------------


async def test_use_proxy_wires_forward_proxy_into_http_client(mock_sdk, monkeypatch):
    captured: dict[str, Any] = {}

    class _FakeHttpClient:
        def __init__(self, **kwargs: Any):
            captured.update(kwargs)

    monkeypatch.setattr(
        "app.agent.adapters.openai.httpx.AsyncClient", _FakeHttpClient
    )
    monkeypatch.setattr(
        "app.agent.adapters.openai.settings",
        SimpleNamespace(forward_proxy_url="http://proxy:8080"),
    )

    OpenAIAdapter(
        model_name="gpt-4o",
        api_key="sk",
        base_url="https://x",
        client_type="openai",
        use_proxy=True,
    )

    # http_client was passed to AsyncOpenAI and built with the proxy
    assert "http_client" in mock_sdk.instance.init_kwargs
    assert captured.get("proxy") == "http://proxy:8080"


async def test_no_proxy_when_use_proxy_false(mock_sdk):
    OpenAIAdapter(
        model_name="gpt-4o",
        api_key="sk",
        base_url="https://x",
        client_type="openai",
        use_proxy=False,
    )
    assert mock_sdk.instance.init_kwargs.get("http_client") is None


async def test_sdk_auto_retry_disabled(mock_sdk):
    OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    assert mock_sdk.instance.init_kwargs["max_retries"] == 0


async def test_azure_variant_uses_azure_client(mock_sdk):
    OpenAIAdapter(
        model_name="my-deployment",
        api_key="sk",
        base_url="https://my.azure.com",
        client_type="azure-http",
    )
    # azure client constructed, plain client not
    assert mock_sdk.azure_instance is not None
    az = mock_sdk.azure_instance.init_kwargs
    assert az["azure_endpoint"] == "https://my.azure.com"
    assert az["api_version"] == "2024-08-01-preview"
    assert az["max_retries"] == 0


# ---------------------------------------------------------------------------
# trace — generation span produced even with update_trace=False
# ---------------------------------------------------------------------------


async def test_generation_span_produced_on_complete(mock_sdk):
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content="hi"))

    await adapter.complete([Message(role=Role.USER, content="hello")])

    assert len(_span_calls) == 1
    assert _span_calls[0]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# registration seam — build_model_client dispatches each client_type with the
# right variant (T1 caller does NOT pass client_type; the registration closure
# supplies it; late-binding footgun guarded)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client_type", "expect_deepseek"),
    [
        ("openai", False),
        ("deepseek", True),
        ("azure-http", False),
        ("openai-responses", False),
    ],
)
async def test_registration_dispatches_correct_variant(
    mock_sdk, monkeypatch, client_type, expect_deepseek
):
    import app.agent.adapters  # noqa: F401 - ensures registration ran

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

    from app.agent.client import build_model_client

    client = await build_model_client("whatever")
    assert isinstance(client, OpenAIAdapter)
    assert client._client_type == client_type
    assert client._is_deepseek is expect_deepseek


async def test_generation_span_produced_for_structured(mock_sdk):
    """update_trace=False (guard path) still produces a generation span.

    The adapter never decides update_trace; it always emits a generation span.
    'update_trace=False' lives at the Agent layer (T3/T4) and only governs
    whether the *parent trace* name/metadata is overwritten — not whether the
    generation span exists. This test pins that the span is unconditional.
    """
    adapter = OpenAIAdapter(
        model_name="gpt-4o", api_key="sk", base_url="https://x", client_type="openai"
    )
    mock_sdk.instance.set_result(_completion(content='{"ok": true}'))

    await adapter.structured(
        [Message(role=Role.USER, content="q")],
        schema={"title": "X", "type": "object"},
    )

    assert len(_span_calls) == 1
