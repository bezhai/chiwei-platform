"""T3 — Gemini native adapter (multimodal / thinking + structured + trace).

Symmetric to the OpenAI adapter (T2): it translates neutral types
(``app.agent.neutral``) to the google-genai *wire* (Content / Part /
FunctionDeclaration / FunctionCall / FunctionResponse) and back. The dev box has
no network egress to Gemini and production keys must not be extracted, so every
test mocks the SDK: we hand the adapter a *canned* genai client whose
``aio.models.generate_content`` / ``generate_content_stream`` return hand-built
response / chunk objects, then assert the adapter's neutral translation.

Coverage (spec §T3 Verification, adapted to mocked transport):
  - plain text round-trip neutral→wire→neutral,
  - multimodal image content block → Gemini image part,
  - thinking part (``thought=True``) → Message.reasoning_content / chunk.reasoning,
  - tool_call (function calling) round-trip,
  - tool_result (function_response) → wire,
  - structured output → dict (response_mime_type json + response_schema),
  - finish_reason mapping (SAFETY/RECITATION→content_filter, MAX_TOKENS→length,
    STOP→stop),
  - use_proxy wires settings.forward_proxy_url into the genai http options,
  - SDK auto-retry is disabled (attempts=1),
  - a generation span is produced.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.adapters.gemini import GeminiAdapter
from app.agent.neutral import ContentBlock, Message, Role, ToolCall, ToolDef

# ---------------------------------------------------------------------------
# Canned google-genai response / chunk builders
# ---------------------------------------------------------------------------


def _part(
    *,
    text: str | None = None,
    thought: bool = False,
    function_call: Any = None,
    inline_data: Any = None,
    thought_signature: bytes | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        thought=thought,
        function_call=function_call,
        inline_data=inline_data,
        thought_signature=thought_signature,
    )


def _function_call(name: str, args: dict[str, Any], call_id: str | None = None) -> Any:
    return SimpleNamespace(name=name, args=args, id=call_id)


def _content(parts: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(parts=parts, role="model")


def _usage(prompt: int = 5, candidates: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        total_token_count=prompt + candidates,
    )


def _response(
    *,
    parts: list[Any] | None = None,
    finish_reason: Any = "STOP",
) -> SimpleNamespace:
    # ``None`` → default text; an explicit ``[]`` stays empty (a finish-only chunk).
    if parts is None:
        parts = [_part(text="hi there")]
    candidate = SimpleNamespace(
        content=_content(parts),
        finish_reason=finish_reason,
        index=0,
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=_usage())


# ---------------------------------------------------------------------------
# Mock genai client: captures kwargs, returns canned objects
# ---------------------------------------------------------------------------


class _MockGenaiClient:
    """Stand-in for google.genai.Client capturing the generate_content call."""

    def __init__(self, **kwargs: Any):
        self.init_kwargs = kwargs
        self.last_generate_kwargs: dict[str, Any] | None = None
        self._next_result: Any = None
        self._stream_chunks: list[Any] | None = None

        async def _generate_content(**kw: Any) -> Any:
            self.last_generate_kwargs = kw
            return self._next_result

        async def _generate_content_stream(**kw: Any) -> Any:
            self.last_generate_kwargs = kw
            chunks = self._stream_chunks or []

            async def _gen() -> Any:
                for c in chunks:
                    yield c

            return _gen()

        async def _aclose() -> None:
            pass

        models = SimpleNamespace(
            generate_content=_generate_content,
            generate_content_stream=_generate_content_stream,
        )
        self.aio = SimpleNamespace(models=models, close=_aclose)

    def set_result(self, result: Any) -> None:
        self._next_result = result

    def set_stream(self, chunks: list[Any]) -> None:
        self._stream_chunks = chunks


@pytest.fixture
def mock_sdk(monkeypatch):
    """Patch the adapter's genai.Client with a capturing mock.

    Returns a holder whose ``.instance`` is the constructed mock so tests can
    read init kwargs and set canned results. Also captures the HttpOptions the
    adapter built (proxy / retry assertions) and neutralises the trace helper.
    """
    holder = SimpleNamespace(instance=None, http_options=None)

    def _make_client(**kwargs: Any) -> _MockGenaiClient:
        m = _MockGenaiClient(**kwargs)
        holder.instance = m
        holder.http_options = kwargs.get("http_options")
        return m

    monkeypatch.setattr("app.agent.adapters.gemini.genai.Client", _make_client)
    monkeypatch.setattr(
        "app.agent.adapters.gemini.generation_span", _fake_generation_span
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
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="hello world")]))

    out = await adapter.complete([Message(role=Role.USER, content="hi")])

    assert out.role == Role.ASSISTANT
    assert out.content == "hello world"
    # wire request carried the user message as a Content with a text part
    sent = mock_sdk.instance.last_generate_kwargs
    assert sent["model"] == "gemini-2.5-flash"
    last = sent["contents"][-1]
    assert last.role == "user"
    assert last.parts[0].text == "hi"


async def test_complete_system_message_goes_to_system_instruction(mock_sdk):
    """A neutral system message becomes config.system_instruction, not a turn."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    await adapter.complete(
        [
            Message(role=Role.SYSTEM, content="you are a cat"),
            Message(role=Role.USER, content="hi"),
        ]
    )

    sent = mock_sdk.instance.last_generate_kwargs
    # system turn is NOT in contents; it's hoisted to system_instruction
    roles = [c.role for c in sent["contents"]]
    assert "system" not in roles
    assert sent["config"].system_instruction == "you are a cat"


async def test_complete_assistant_role_maps_to_model(mock_sdk):
    """Neutral ASSISTANT role serialises to Gemini's ``model`` role."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    await adapter.complete(
        [
            Message(role=Role.USER, content="hi"),
            Message(role=Role.ASSISTANT, content="prior reply"),
            Message(role=Role.USER, content="again"),
        ]
    )

    sent = mock_sdk.instance.last_generate_kwargs["contents"]
    assert sent[1].role == "model"
    assert sent[1].parts[0].text == "prior reply"


# ---------------------------------------------------------------------------
# multimodal — image content blocks → Gemini image part
# ---------------------------------------------------------------------------


async def test_complete_chat_history_image_block_downloaded_to_inline_part(
    mock_sdk, monkeypatch
):
    """A neutral ``image`` block (http url) is downloaded to inline_data bytes.

    Gemini does not fetch arbitrary http urls via file_data and rejects wildcard
    mime types, so — mirroring the old langchain-google-genai path — the adapter
    downloads the bytes and sends them inline with a concrete mime type.
    """
    fetched: dict[str, str] = {}

    async def _stub(url: str) -> tuple[bytes, str]:
        fetched["url"] = url
        return b"PNGBYTES", "image/png"

    monkeypatch.setattr("app.agent.adapters.gemini._fetch_remote_image", _stub)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="a dog")]))

    msg = Message(
        role=Role.USER,
        content=[
            ContentBlock.from_text("look"),
            ContentBlock.from_image(url="https://img/dog.png"),
        ],
    )
    await adapter.complete([msg])

    parts = mock_sdk.instance.last_generate_kwargs["contents"][-1].parts
    assert parts[0].text == "look"
    img = parts[1]
    # inlined bytes, NOT a file_data uri-by-reference with image/*
    assert getattr(img, "file_data", None) is None
    assert img.inline_data is not None
    assert img.inline_data.data == b"PNGBYTES"
    assert img.inline_data.mime_type == "image/png"
    assert fetched["url"] == "https://img/dog.png"


async def test_complete_openai_style_image_url_block_downloaded_to_inline_part(
    mock_sdk, monkeypatch
):
    """A tool-returned OpenAI-style ``image_url`` block is downloaded + inlined."""
    fetched: dict[str, str] = {}

    async def _stub(url: str) -> tuple[bytes, str]:
        fetched["url"] = url
        return b"JPEGBYTES", "image/jpeg"

    monkeypatch.setattr("app.agent.adapters.gemini._fetch_remote_image", _stub)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="seen")]))

    msg = Message(
        role=Role.USER,
        content=[
            ContentBlock.from_text("what is this"),
            ContentBlock.from_image_url({"url": "https://img/out.png"}),
        ],
    )
    await adapter.complete([msg])

    parts = mock_sdk.instance.last_generate_kwargs["contents"][-1].parts
    img = parts[1]
    assert getattr(img, "file_data", None) is None
    assert img.inline_data.data == b"JPEGBYTES"
    assert img.inline_data.mime_type == "image/jpeg"
    assert fetched["url"] == "https://img/out.png"


async def test_complete_data_uri_image_decoded_inline_without_network(
    mock_sdk, monkeypatch
):
    """A ``data:`` URI image is decoded inline; it must NOT hit the network."""

    async def _boom(url: str) -> tuple[bytes, str]:
        raise AssertionError("data: URI must be decoded locally, not downloaded")

    monkeypatch.setattr("app.agent.adapters.gemini._fetch_remote_image", _boom)

    import base64

    payload = base64.b64encode(b"JPEGBYTES").decode()
    data_uri = f"data:image/jpeg;base64,{payload}"

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    msg = Message(role=Role.USER, content=[ContentBlock.from_image(url=data_uri)])
    await adapter.complete([msg])

    img = mock_sdk.instance.last_generate_kwargs["contents"][-1].parts[0]
    assert img.inline_data.data == b"JPEGBYTES"
    assert img.inline_data.mime_type == "image/jpeg"


# ---------------------------------------------------------------------------
# thinking — thought parts → reasoning_content / reasoning
# ---------------------------------------------------------------------------


async def test_complete_thought_part_becomes_reasoning_content(mock_sdk):
    """A response part with ``thought=True`` lands in reasoning_content, not content."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[
                _part(text="let me think about this", thought=True),
                _part(text="the answer is 42"),
            ]
        )
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])

    assert out.content == "the answer is 42"
    assert out.reasoning_content == "let me think about this"


async def test_complete_requests_thinking_with_thoughts(mock_sdk):
    """The adapter asks Gemini to include thoughts (thinking_config.include_thoughts)."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    await adapter.complete([Message(role=Role.USER, content="q")])

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.include_thoughts is True


# ---------------------------------------------------------------------------
# tool_call / tool_result — function calling round-trip
# ---------------------------------------------------------------------------


async def test_complete_tool_call_roundtrip(mock_sdk):
    """A Gemini function_call part → neutral ToolCall; tools → function_declarations."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[
                _part(function_call=_function_call("search", {"q": "cats"}, "call_1"))
            ],
            finish_reason="STOP",
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

    # tools were translated to a function-declaration tool on the wire
    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "search"
    assert decl.description == "search the web"


async def test_complete_tool_call_without_id_gets_synthesised_id(mock_sdk):
    """Gemini function calls may lack an id; the adapter synthesises a stable one."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[_part(function_call=_function_call("search", {"q": "x"}, None))]
        )
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id  # non-empty synthesised id
    assert out.tool_calls[0].name == "search"


async def test_complete_sends_assistant_tool_call_and_tool_result(mock_sdk):
    """An assistant tool_call + a tool result serialise to function_call /
    function_response parts."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

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

    contents = mock_sdk.instance.last_generate_kwargs["contents"]

    # assistant turn → model role with a function_call part
    assistant = contents[1]
    assert assistant.role == "model"
    fc_part = assistant.parts[0]
    assert fc_part.function_call is not None
    assert fc_part.function_call.name == "search"
    assert fc_part.function_call.args == {"q": "cats"}

    # tool result → user role with a function_response part
    tool_turn = contents[2]
    assert tool_turn.role == "user"
    fr_part = tool_turn.parts[0]
    assert fr_part.function_response is not None
    assert fr_part.function_response.name == "search"
    # the function name is recovered from the matching call id
    assert fr_part.function_response.response == {"result": "3 results"}


async def test_tool_result_image_block_surfaces_as_inline_part(mock_sdk, monkeypatch):
    """A tool result carrying image blocks must NOT silently drop the images.

    read_images / generate_image return list[ContentBlock] with image_url blocks.
    Gemini's function_response part is structured JSON (no image); flattening the
    tool message with .text() drops the image entirely, so the model never sees
    what the tool returned. The fix keeps the function_response (text result)
    AND surfaces each image block as a downloaded inline_data part on the same
    user turn.
    """

    async def _stub(url: str) -> tuple[bytes, str]:
        return b"IMG3", "image/png"

    monkeypatch.setattr("app.agent.adapters.gemini._fetch_remote_image", _stub)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="a dog")]))

    history = [
        Message(role=Role.USER, content="show me 3.png"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="read_images", arguments={})],
        ),
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("@3.png:"),
                ContentBlock.from_image_url({"url": "https://img/3.png"}),
            ],
        ),
    ]
    await adapter.complete(history)

    tool_turn = mock_sdk.instance.last_generate_kwargs["contents"][2]
    assert tool_turn.role == "user"

    # function_response part still present (names the answered call)
    fr_parts = [p for p in tool_turn.parts if getattr(p, "function_response", None)]
    assert len(fr_parts) == 1
    assert fr_parts[0].function_response.name == "read_images"

    # the image block reached the wire as a downloaded inline image part
    img_parts = [p for p in tool_turn.parts if getattr(p, "inline_data", None)]
    assert len(img_parts) == 1
    assert img_parts[0].inline_data.data == b"IMG3"
    assert img_parts[0].inline_data.mime_type == "image/png"


# ---------------------------------------------------------------------------
# thought_signature — Gemini 2.5 thinking models attach an opaque signature to
# the functionCall part. It MUST round-trip: resending an assistant
# function_call turn WITHOUT its signature 400s with
# "Function call is missing a thought_signature in functionCall parts"
# (INVALID_ARGUMENT), which broke multi-turn tool loops (load_skill) on ppe.
# ---------------------------------------------------------------------------


async def test_complete_captures_thought_signature_on_tool_call(mock_sdk):
    """A function_call part's thought_signature lands on the neutral ToolCall."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[
                _part(
                    function_call=_function_call("load_skill", {"name": "x"}, "c1"),
                    thought_signature=b"sig-abc",
                )
            ]
        )
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])
    assert out.tool_calls[0].signature == b"sig-abc"


async def test_stream_captures_thought_signature_on_tool_call(mock_sdk):
    """The streamed tool_call chunk carries the part's thought_signature too."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream(
        [
            _response(
                parts=[
                    _part(
                        function_call=_function_call(
                            "load_skill", {"name": "x"}, "c1"
                        ),
                        thought_signature=b"sig-xyz",
                    )
                ],
                finish_reason="STOP",
            )
        ]
    )
    tools = [
        ToolDef(name="load_skill", description="d", parameters={"type": "object"})
    ]
    chunks = [
        c
        async for c in adapter.stream(
            [Message(role=Role.USER, content="q")], tools=tools
        )
    ]
    tc_chunks = [c for c in chunks if c.tool_call is not None]
    assert tc_chunks[0].tool_call.signature == b"sig-xyz"


async def test_assistant_tool_call_reattaches_thought_signature_on_wire(mock_sdk):
    """Resending an assistant function_call turn re-attaches its thought_signature
    to the wire Part — else Gemini 2.5 rejects the next turn (400 INVALID_ARGUMENT,
    missing thought_signature)."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    history = [
        Message(role=Role.USER, content="use skill"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="load_skill",
                    arguments={"name": "x"},
                    signature=b"sig-abc",
                )
            ],
        ),
        Message(role=Role.TOOL, content="ok", tool_call_id="c1"),
    ]
    await adapter.complete(history)

    contents = mock_sdk.instance.last_generate_kwargs["contents"]
    fc_part = contents[1].parts[0]
    assert fc_part.function_call.name == "load_skill"
    assert fc_part.thought_signature == b"sig-abc"


async def test_tool_call_without_signature_omits_it_on_wire(mock_sdk):
    """A ToolCall with no signature must not force a (None) thought_signature
    that would itself trip the API — the part simply carries no signature."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    history = [
        Message(role=Role.USER, content="hi"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "x"})],
        ),
        Message(role=Role.TOOL, content="ok", tool_call_id="c1"),
    ]
    await adapter.complete(history)

    fc_part = mock_sdk.instance.last_generate_kwargs["contents"][1].parts[0]
    assert fc_part.function_call.name == "search"
    assert fc_part.thought_signature is None


# ---------------------------------------------------------------------------
# structured() — response_schema json → dict
# ---------------------------------------------------------------------------


async def test_structured_returns_dict(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(parts=[_part(text='{"is_injection": true, "confidence": 0.9}')])
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

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema == schema


# ---------------------------------------------------------------------------
# stream() — neutral chunk emission
# ---------------------------------------------------------------------------


async def test_stream_emits_text_reasoning_finish(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream(
        [
            _response(parts=[_part(text="think", thought=True)], finish_reason=None),
            _response(parts=[_part(text="hel")], finish_reason=None),
            _response(parts=[_part(text="lo")], finish_reason=None),
            _response(parts=[], finish_reason="STOP"),
        ]
    )

    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="hi")])]

    assert any(c.reasoning == "think" for c in chunks)
    assert "".join(c.text for c in chunks if c.text) == "hello"
    assert chunks[-1].finish_reason == "stop"


async def test_stream_emits_tool_call_boundary(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream(
        [
            _response(parts=[_part(text="let me search")], finish_reason=None),
            _response(
                parts=[
                    _part(
                        function_call=_function_call("search", {"q": "cats"}, "call_1")
                    )
                ],
                finish_reason="STOP",
            ),
        ]
    )

    tools = [ToolDef(name="search", description="d", parameters={"type": "object"})]
    chunks = [
        c
        async for c in adapter.stream(
            [Message(role=Role.USER, content="find cats")], tools=tools
        )
    ]

    assert "".join(c.text for c in chunks if c.text) == "let me search"
    tool_chunks = [c for c in chunks if c.tool_call is not None]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_call.name == "search"
    assert tool_chunks[0].tool_call.arguments == {"q": "cats"}
    assert chunks[-1].finish_reason == "stop"


# ---------------------------------------------------------------------------
# finish_reason mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gemini_reason", "neutral_reason"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
    ],
)
async def test_stream_finish_reason_mapping(mock_sdk, gemini_reason, neutral_reason):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream(
        [_response(parts=[_part(text="partial")], finish_reason=gemini_reason)]
    )

    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="x")])]
    assert chunks[-1].finish_reason == neutral_reason


async def test_finish_reason_accepts_enum_objects(mock_sdk):
    """A finish_reason given as an enum-like object (has ``.name``) maps too."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    enum_like = SimpleNamespace(name="MAX_TOKENS")
    mock_sdk.instance.set_stream(
        [_response(parts=[_part(text="x")], finish_reason=enum_like)]
    )
    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="x")])]
    assert chunks[-1].finish_reason == "length"


# ---------------------------------------------------------------------------
# proxy / retry / trace / function-calling-auto-off
# ---------------------------------------------------------------------------


async def test_use_proxy_wires_forward_proxy_into_http_options(mock_sdk, monkeypatch):
    monkeypatch.setattr(
        "app.agent.adapters.gemini.settings",
        SimpleNamespace(forward_proxy_url="http://proxy:8080"),
    )

    GeminiAdapter(
        model_name="gemini-2.5-flash",
        api_key="k",
        base_url="https://g",
        use_proxy=True,
    )

    http_opts = mock_sdk.http_options
    assert http_opts is not None
    assert http_opts.client_args == {"proxy": "http://proxy:8080"}
    assert http_opts.async_client_args == {"proxy": "http://proxy:8080"}


async def test_no_proxy_when_use_proxy_false(mock_sdk, monkeypatch):
    monkeypatch.setattr(
        "app.agent.adapters.gemini.settings",
        SimpleNamespace(forward_proxy_url="http://proxy:8080"),
    )
    GeminiAdapter(
        model_name="gemini-2.5-flash",
        api_key="k",
        base_url="https://g",
        use_proxy=False,
    )
    http_opts = mock_sdk.http_options
    # no proxy injected when use_proxy is False
    assert http_opts is None or http_opts.client_args is None


async def test_sdk_auto_retry_disabled(mock_sdk):
    GeminiAdapter(model_name="gemini-2.5-flash", api_key="k", base_url="https://g")
    http_opts = mock_sdk.http_options
    assert http_opts is not None
    assert http_opts.retry_options is not None
    assert http_opts.retry_options.attempts == 1


async def test_base_url_passed_to_http_options(mock_sdk):
    GeminiAdapter(model_name="gemini-2.5-flash", api_key="k", base_url="https://g")
    assert mock_sdk.http_options.base_url == "https://g"


async def test_automatic_function_calling_disabled(mock_sdk):
    """The SDK must not run tools itself — the Agent layer owns the ReAct loop."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    tools = [ToolDef(name="search", description="d", parameters={"type": "object"})]
    await adapter.complete([Message(role=Role.USER, content="q")], tools=tools)

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    assert cfg.automatic_function_calling.disable is True


async def test_generation_span_produced_on_complete(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="hi")]))

    await adapter.complete([Message(role=Role.USER, content="hello")])

    assert len(_span_calls) == 1
    assert _span_calls[0]["model"] == "gemini-2.5-flash"


async def test_generation_span_produced_for_structured(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text='{"ok": true}')]))

    await adapter.structured(
        [Message(role=Role.USER, content="q")],
        schema={"title": "X", "type": "object"},
    )

    assert len(_span_calls) == 1


async def test_stream_generation_span_records_usage(mock_sdk):
    """Streaming must record token usage on the generation span.

    Gemini delivers cumulative usage_metadata on the last streamed chunk. The
    stream() span — like complete() — must surface it as usage_details so
    langfuse token accounting isn't silently lost on the main chat path.
    """
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    last = _response(parts=[], finish_reason="STOP")
    last.usage_metadata = _usage(prompt=11, candidates=22)
    mock_sdk.instance.set_stream(
        [
            _response(parts=[_part(text="hel")], finish_reason=None),
            _response(parts=[_part(text="lo")], finish_reason=None),
            last,
        ]
    )

    async for _ in adapter.stream([Message(role=Role.USER, content="hi")]):
        pass

    # the streamed generation span recorded usage_details from the last chunk
    span = _MOST_RECENT_SPAN[-1]
    usage_updates = [u for u in span.updates if "usage_details" in u]
    assert usage_updates, "stream span never recorded usage_details"
    assert usage_updates[-1]["usage_details"] == {
        "input": 11,
        "output": 22,
        "total": 33,
    }


# ---------------------------------------------------------------------------
# registration seam — build_model_client dispatches client_type "google"
# ---------------------------------------------------------------------------


async def test_registration_dispatches_google(monkeypatch):
    import app.agent.adapters  # noqa: F401 - ensures registration ran

    captured: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> _MockGenaiClient:
        captured.update(kwargs)
        return _MockGenaiClient(**kwargs)

    monkeypatch.setattr("app.agent.adapters.gemini.genai.Client", _make_client)

    async def _resolve(model_id, *, required_fields=()):
        return {
            "model_name": "gemini-2.5-flash",
            "api_key": "k",
            "base_url": "https://g",
            "is_active": True,
            "client_type": "google",
            "use_proxy": False,
        }

    monkeypatch.setattr("app.agent.client.resolve_model_info", _resolve)

    from app.agent.client import build_model_client

    client = await build_model_client("whatever")
    assert isinstance(client, GeminiAdapter)
