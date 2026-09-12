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
  - thinking part (``thought=True``) → a thought part of the turn sequence /
    chunk.reasoning, with its signature,
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

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.agent.adapters.gemini import GeminiAdapter
from app.agent.neutral import ContentBlock, Message, Role, ToolCall, ToolDef
from app.agent.tooling import tool

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
        # every generate call's kwargs, in order — a ReAct loop calls more than
        # once and what the SECOND request carried is the point of a replay test.
        self.generate_calls: list[dict[str, Any]] = []
        self._next_result: Any = None
        self._stream_chunks: list[Any] | None = None
        self._stream_scripts: list[list[Any]] = []

        async def _generate_content(**kw: Any) -> Any:
            self.last_generate_kwargs = kw
            self.generate_calls.append(kw)
            return self._next_result

        async def _generate_content_stream(**kw: Any) -> Any:
            self.last_generate_kwargs = kw
            self.generate_calls.append(kw)
            chunks = (
                self._stream_scripts.pop(0)
                if self._stream_scripts
                else (self._stream_chunks or [])
            )

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

    def set_streams(self, scripts: list[list[Any]]) -> None:
        """One chunk list per ``stream`` call, in order (the ReAct loop streams
        again after it has dispatched the tools a turn asked for)."""
        self._stream_scripts = [list(s) for s in scripts]


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


async def test_complete_pops_session_id_out_of_params(mock_sdk):
    """session_id is a prompt-cache-key control param meaningless to Gemini's
    native wire; the adapter consumes it instead of leaking it into the trace's
    model_parameters (abstraction leak) or the genai config."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    await adapter.complete(
        [Message(role=Role.USER, content="hi")], session_id="s"
    )

    assert "session_id" not in _span_calls[0]["model_parameters"]


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
# an image that can't be downloaded — degrades to text, doesn't kill the turn
# ---------------------------------------------------------------------------

# What a history picture's url looks like: a TOS object plus a signed query.
_SIGNED_URL = "https://tos.example/im/dog.png?X-Tos-Signature=deadbeef&expires=1"
_SHUT = "[图片：打不开]"


def _serve_http(monkeypatch, answer) -> None:
    """Route ``_fetch_remote_image``'s httpx client at ``answer(url)``.

    ``answer`` returns an ``httpx.Response`` (which the adapter then runs
    ``raise_for_status()`` on, so a 403 raises exactly as it would in prod) or
    raises a transport error itself.
    """

    class _FakeAsyncClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def get(self, url: str) -> httpx.Response:
            return answer(url)

    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)


def _forbidden(url: str) -> httpx.Response:
    """The shape of an expired TOS signature: 403 with an XML error body."""
    return httpx.Response(
        403,
        request=httpx.Request("GET", url),
        headers={"content-type": "application/xml"},
        content=b"<Error><Code>AccessDenied</Code></Error>",
    )


def _timed_out(url: str) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))


def _image_bytes(url: str) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("GET", url),
        headers={"content-type": "image/png"},
        content=b"ALIVE",
    )


async def test_history_image_that_403s_degrades_to_a_text_placeholder(
    mock_sdk, monkeypatch
):
    """An expired signature costs one picture, not the whole turn.

    The download runs before the model is even called, so a raise here means she
    never gets to speak — and the history that carries the picture is replayed
    every wakeup, so the turn would keep dying.
    """
    _serve_http(monkeypatch, _forbidden)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="看不到就算了")]))

    msg = Message(
        role=Role.USER,
        content=[
            ContentBlock.from_text("看这张"),
            ContentBlock.from_image(url=_SIGNED_URL),
            ContentBlock.from_text("好看吗"),
        ],
    )
    out = await adapter.complete([msg])

    parts = mock_sdk.instance.last_generate_kwargs["contents"][-1].parts
    # the placeholder sits where the picture was: she is told one was there
    assert [p.text for p in parts] == ["看这张", _SHUT, "好看吗"]
    assert all(getattr(p, "inline_data", None) is None for p in parts)
    assert out.content == "看不到就算了"


async def test_history_image_that_times_out_degrades_to_a_text_placeholder(
    mock_sdk, monkeypatch
):
    """A transport timeout reads the same as a 403: one picture is gone."""
    _serve_http(monkeypatch, _timed_out)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    msg = Message(
        role=Role.USER,
        content=[ContentBlock.from_image_url({"url": _SIGNED_URL})],
    )
    await adapter.complete([msg])

    parts = mock_sdk.instance.last_generate_kwargs["contents"][-1].parts
    assert [p.text for p in parts] == [_SHUT]


async def test_unreachable_image_is_logged_with_which_one_and_why(
    mock_sdk, monkeypatch, caplog
):
    """The degrade is never silent: the log names the object and the reason."""
    _serve_http(monkeypatch, _forbidden)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    msg = Message(role=Role.USER, content=[ContentBlock.from_image(url=_SIGNED_URL)])
    with caplog.at_level(logging.WARNING, logger="app.agent.adapters.gemini"):
        await adapter.complete([msg])

    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.agent.adapters.gemini" and r.levelno == logging.WARNING
    ]
    assert len(lines) == 1
    assert "https://tos.example/im/dog.png" in lines[0]
    assert "HTTP 403" in lines[0]
    # the pre-signature is a credential; the object path alone names the picture
    assert "X-Tos-Signature" not in lines[0]


async def test_timed_out_image_logs_the_transport_reason(mock_sdk, monkeypatch, caplog):
    _serve_http(monkeypatch, _timed_out)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    msg = Message(role=Role.USER, content=[ContentBlock.from_image(url=_SIGNED_URL)])
    with caplog.at_level(logging.WARNING, logger="app.agent.adapters.gemini"):
        await adapter.complete([msg])

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1
    assert "ReadTimeout" in lines[0]
    assert "https://tos.example/im/dog.png" in lines[0]


async def test_one_dead_image_leaves_the_rest_of_the_sequence_encodable(
    mock_sdk, monkeypatch
):
    """A whole system+user+assistant+tool sequence still reaches the wire.

    The dead picture becomes a placeholder; the live one beside it is still
    downloaded, the tool round still answers its call with exactly one part, and
    the trace still renders.
    """

    def _answer(url: str) -> httpx.Response:
        if "dead" in url:
            raise httpx.ConnectError("no route", request=httpx.Request("GET", url))
        return _image_bytes(url)

    _serve_http(monkeypatch, _answer)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="嗯")]))

    history = [
        Message(role=Role.SYSTEM, content="你是赤尾"),
        Message(
            role=Role.USER,
            content=[
                ContentBlock.from_text("[图片1]"),
                ContentBlock.from_image(url="https://tos.example/im/dead.png?sig=a"),
                ContentBlock.from_text("[图片2]"),
                ContentBlock.from_image(url="https://tos.example/im/alive.png?sig=b"),
            ],
        ),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="look_at_pictures", arguments={})],
        ),
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("@old.png:"),
                ContentBlock.from_image_url(
                    {"url": "https://tos.example/im/dead2.png?sig=c"}
                ),
            ],
        ),
    ]
    out = await adapter.complete(history)

    kwargs = mock_sdk.instance.last_generate_kwargs
    assert kwargs["config"].system_instruction == "你是赤尾"
    contents = kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]

    user_parts = contents[0].parts
    assert user_parts[1].text == _SHUT
    assert user_parts[3].inline_data.data == b"ALIVE"
    assert user_parts[3].inline_data.mime_type == "image/png"

    fr = contents[2].parts[0].function_response
    assert len(contents[2].parts) == 1
    assert fr.parts is None
    assert fr.response == {"result": f"@old.png:\n{_SHUT}"}

    assert out.content == "嗯"
    # the trace renders the whole thing, placeholder included
    trace_input = _span_calls[-1]["input"]
    assert {"text": _SHUT} in trace_input[0]["parts"]


async def test_tool_result_with_a_dead_picture_says_so_in_its_answer(
    mock_sdk, monkeypatch
):
    """A tool's own picture that won't download is reported, not just dropped.

    ``FunctionResponsePart`` carries only media, so the placeholder rides in the
    answer's text — the one place inside a function_response that can say it.
    """
    _serve_http(monkeypatch, _forbidden)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    history = [
        Message(role=Role.USER, content="找张图"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="find_a_picture_online", arguments={})],
        ),
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("找到 1 张:"),
                ContentBlock.from_image_url({"url": _SIGNED_URL}),
            ],
        ),
    ]
    await adapter.complete(history)

    tool_turn = mock_sdk.instance.last_generate_kwargs["contents"][2]
    fr = tool_turn.parts[0].function_response
    assert fr.parts is None
    assert fr.response == {"result": f"找到 1 张:\n{_SHUT}"}


async def test_an_odd_content_type_is_not_treated_as_a_failed_download(
    mock_sdk, monkeypatch
):
    """Object storage serves ``application/octet-stream`` for real pictures.

    Bytes arrived, so this is not a failed download: the mime is guessed from
    the object name and the picture goes out. Treating the header as the verdict
    would blind her to images that are perfectly fine.
    """

    def _octet_stream(url: str) -> httpx.Response:
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={"content-type": "application/octet-stream"},
            content=b"REALPNG",
        )

    _serve_http(monkeypatch, _octet_stream)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    msg = Message(role=Role.USER, content=[ContentBlock.from_image(url=_SIGNED_URL)])
    await adapter.complete([msg])

    img = mock_sdk.instance.last_generate_kwargs["contents"][-1].parts[0]
    assert img.inline_data.data == b"REALPNG"
    assert img.inline_data.mime_type == "image/png"


async def test_a_corrupt_data_uri_still_raises(mock_sdk):
    """A ``data:`` URI we built ourselves and can't decode is a defect, not a
    picture that got away: it fails the same way every time, no tool call
    recovers it, and degrading it would hide the bug forever.
    """
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    msg = Message(
        role=Role.USER,
        content=[ContentBlock.from_image(url="data:image/png;base64,QQ")],
    )
    with pytest.raises(Exception, match="padding"):
        await adapter.complete([msg])


# ---------------------------------------------------------------------------
# thinking — thought parts land in the turn sequence, out of the spoken text
# ---------------------------------------------------------------------------


async def test_complete_thought_part_stays_out_of_the_spoken_text(mock_sdk):
    """A response part with ``thought=True`` is a thought part of the turn, not content."""
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
    assert out.thought_text() == "let me think about this"
    assert [str(p.kind) for p in out.turn_parts] == ["thought", "text"]


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


async def test_tool_result_image_rides_inside_the_function_response(
    mock_sdk, monkeypatch
):
    """A tool result's pictures ride INSIDE the function_response.

    A tool that hands back pictures returns list[ContentBlock] with image_url
    blocks. Flattening the tool message with .text() drops the image entirely,
    so the model never sees what the tool returned; hanging it beside the
    function_response makes the tool-result turn carry more parts than the model
    turn had function_calls. Gemini's documented shape for a multimodal tool
    result nests the media in ``FunctionResponse.parts``, so the answer to one
    call stays exactly one part.
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
            tool_calls=[ToolCall(id="c1", name="look_at_pictures", arguments={})],
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

    # one call answered ⇒ one part, whatever came back inside it
    assert len(tool_turn.parts) == 1
    fr = tool_turn.parts[0].function_response
    assert fr.name == "look_at_pictures"
    assert fr.response == {"result": "@3.png:"}

    # the image block reached the wire as downloaded bytes, nested in the response
    assert [(b.inline_data.data, b.inline_data.mime_type) for b in fr.parts] == [
        (b"IMG3", "image/png")
    ]
    # and nothing rides beside the function_response
    assert not [p for p in tool_turn.parts if getattr(p, "inline_data", None)]


async def test_many_pictures_from_one_tool_stay_one_part(mock_sdk, monkeypatch):
    """``find_a_picture_online`` hands back several pictures at once.

    Every one of them must reach the model, and they must all ride in the SAME
    function_response: N pictures beside it would make one answered call cost
    N+1 parts, and Gemini counts parts against the model turn's function_calls.
    """
    fetched: list[str] = []

    async def _stub(url: str) -> tuple[bytes, str]:
        fetched.append(url)
        return url.encode(), "image/jpeg"

    monkeypatch.setattr("app.agent.adapters.gemini._fetch_remote_image", _stub)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="nice")]))

    history = [
        Message(role=Role.USER, content="find me cats"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="c1", name="find_a_picture_online", arguments={})
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("猫 pic=a"),
                ContentBlock.from_image_url({"url": "https://img/a.jpg"}),
                ContentBlock.from_text("猫 pic=b"),
                ContentBlock.from_image_url({"url": "https://img/b.jpg"}),
                ContentBlock.from_text("猫 pic=c"),
                ContentBlock.from_image_url({"url": "https://img/c.jpg"}),
            ],
        ),
    ]
    await adapter.complete(history)

    tool_turn = mock_sdk.instance.last_generate_kwargs["contents"][2]
    assert len(tool_turn.parts) == 1
    fr = tool_turn.parts[0].function_response
    # all three, in the order the tool handed them back
    assert [b.inline_data.data for b in fr.parts] == [
        b"https://img/a.jpg",
        b"https://img/b.jpg",
        b"https://img/c.jpg",
    ]
    assert fetched == [
        "https://img/a.jpg",
        "https://img/b.jpg",
        "https://img/c.jpg",
    ]


async def test_text_only_tool_result_carries_no_media_field(mock_sdk):
    """A tool result with no pictures leaves ``parts`` unset, not empty.

    An empty list is not None, so it would serialise a ``"parts": []`` onto
    every text-only tool result on the wire.
    """
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    history = [
        Message(role=Role.USER, content="find cats"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "cats"})],
        ),
        Message(role=Role.TOOL, content="3 results", tool_call_id="c1"),
    ]
    await adapter.complete(history)

    tool_turn = mock_sdk.instance.last_generate_kwargs["contents"][2]
    assert tool_turn.parts[0].function_response.parts is None


async def test_parallel_tool_results_with_pictures_keep_the_part_count(
    mock_sdk, monkeypatch
):
    """Two calls answered in one turn stay two parts, pictures and all.

    This is the case that breaks with sibling images: two calls, one of them
    handing back two pictures, used to make a four-part user turn answering a
    two-call model turn.
    """

    async def _stub(url: str) -> tuple[bytes, str]:
        return url.encode(), "image/png"

    monkeypatch.setattr("app.agent.adapters.gemini._fetch_remote_image", _stub)

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    history = [
        Message(role=Role.USER, content="look and search"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="c1", name="look_at_phone", arguments={}),
                ToolCall(id="c2", name="search", arguments={"q": "cats"}),
            ],
        ),
        Message(
            role=Role.TOOL,
            tool_call_id="c1",
            content=[
                ContentBlock.from_text("[图片1]"),
                ContentBlock.from_image_url({"url": "https://img/1.png"}),
                ContentBlock.from_image_url({"url": "https://img/2.png"}),
            ],
        ),
        Message(role=Role.TOOL, content="3 results", tool_call_id="c2"),
    ]
    await adapter.complete(history)

    contents = mock_sdk.instance.last_generate_kwargs["contents"]
    calls = [p for p in contents[1].parts if getattr(p, "function_call", None)]
    tool_turn = contents[2]
    assert len(tool_turn.parts) == len(calls) == 2

    phone, search = (p.function_response for p in tool_turn.parts)
    assert phone.name == "look_at_phone"
    assert [b.inline_data.data for b in phone.parts] == [
        b"https://img/1.png",
        b"https://img/2.png",
    ]
    assert search.name == "search"
    assert search.parts is None


async def test_trace_input_shows_the_pictures_in_a_tool_result(mock_sdk, monkeypatch):
    """Langfuse must still show that pictures went out with a tool result.

    They no longer ride as top-level inline_data parts, so the trace renders
    them from the function_response: how many, what each one is, how big.
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
            tool_calls=[ToolCall(id="c1", name="look_at_pictures", arguments={})],
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

    traced = _span_calls[0]["input"][2]
    assert traced["parts"] == [
        {
            "function_response": {
                "name": "look_at_pictures",
                "images": [{"mime_type": "image/png", "bytes": 4}],
            }
        }
    ]


# ---------------------------------------------------------------------------
# parallel tool calls — Gemini requires the function_response parts answering a
# model turn to equal that turn's function_call parts, and to ride in ONE user
# turn. One Content per neutral TOOL message 400s every round where the model
# called more than one tool ("Please ensure that the number of function response
# parts is equal to the number of function call parts of the function call
# turn.", INVALID_ARGUMENT).
# ---------------------------------------------------------------------------


async def test_parallel_tool_results_merge_into_one_user_turn(mock_sdk):
    """Two tool results answering one model turn become one Content, two parts."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    history = [
        Message(role=Role.USER, content="cats and weather"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="call_1", name="search", arguments={"q": "cats"}),
                ToolCall(id="call_2", name="weather", arguments={"city": "sh"}),
            ],
        ),
        Message(role=Role.TOOL, content="3 cats", tool_call_id="call_1"),
        Message(role=Role.TOOL, content="sunny", tool_call_id="call_2"),
    ]
    await adapter.complete(history)

    contents = mock_sdk.instance.last_generate_kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]

    calls = [p for p in contents[1].parts if getattr(p, "function_call", None)]
    responses = [p for p in contents[2].parts if getattr(p, "function_response", None)]
    assert len(responses) == len(calls) == 2

    # each part keeps the name of the call it answers (call_id → name mapping)
    assert [p.function_response.name for p in responses] == ["search", "weather"]
    assert [p.function_response.response for p in responses] == [
        {"result": "3 cats"},
        {"result": "sunny"},
    ]


async def test_single_tool_result_keeps_its_one_part_turn(mock_sdk):
    """One tool call keeps the shape it already had: one user turn, one part."""
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
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert len(contents[2].parts) == 1
    assert contents[2].parts[0].function_response.name == "search"
    assert contents[2].parts[0].function_response.response == {"result": "3 results"}


async def test_tool_results_from_different_rounds_stay_in_separate_turns(mock_sdk):
    """Only *consecutive* tool results merge.

    Two rounds each have their own model turn with one function_call, so their
    results must stay two user turns — merging across the assistant message
    between them would break the same count rule the merge exists to keep.
    """
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    history = [
        Message(role=Role.USER, content="cats then weather"),
        Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="call_1", name="search", arguments={"q": "cats"})],
        ),
        Message(role=Role.TOOL, content="3 cats", tool_call_id="call_1"),
        Message(
            role=Role.ASSISTANT,
            content="now the weather",
            tool_calls=[
                ToolCall(id="call_2", name="weather", arguments={"city": "sh"})
            ],
        ),
        Message(role=Role.TOOL, content="sunny", tool_call_id="call_2"),
    ]
    await adapter.complete(history)

    contents = mock_sdk.instance.last_generate_kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model", "user", "model", "user"]
    assert [p.function_response.name for p in contents[2].parts] == ["search"]
    assert [p.function_response.name for p in contents[4].parts] == ["weather"]


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


async def test_api_version_passed_to_http_options(mock_sdk):
    """A gateway pinned to one API version needs it configurable per provider.

    The SDK appends ``{api_version}/models/...`` to base_url, so a provider
    that only routes ``/v1`` cannot be reached by putting v1 in the base_url —
    the version segment is always appended on top.
    """
    GeminiAdapter(
        model_name="gemini-3.7-flash",
        api_key="k",
        base_url="https://gw/prefix",
        api_version="v1",
    )
    assert mock_sdk.http_options.api_version == "v1"


async def test_no_api_version_leaves_the_sdk_default(mock_sdk):
    """Providers that don't set one keep whatever the SDK picks (v1beta)."""
    GeminiAdapter(model_name="gemini-2.5-flash", api_key="k", base_url="https://g")
    assert mock_sdk.http_options.api_version is None


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
# prompt cache — Gemini's implicit cache is on by default and reports the hit
# in ``usage_metadata.cached_content_token_count``. Without it on the span a
# stable-prefix design has no way to show whether the prefix is actually reused.
# ---------------------------------------------------------------------------


def _usage_with_cache(prompt: int, candidates: int, cached: Any) -> SimpleNamespace:
    """Gemini usage_metadata carrying the implicit-cache hit counter.

    ``cached_content_token_count`` counts the prompt tokens served from cache;
    ``prompt_token_count`` already includes them (so cached ⊆ input, same
    relation as OpenAI's prompt_tokens / cached_tokens).
    """
    usage = _usage(prompt=prompt, candidates=candidates)
    usage.cached_content_token_count = cached
    return usage


async def test_complete_records_cached_content_tokens_in_usage(mock_sdk):
    """A cache hit surfaces on the span as cache_read_input_tokens."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    response = _response()
    response.usage_metadata = _usage_with_cache(
        prompt=62000, candidates=18, cached=60000
    )
    mock_sdk.instance.set_result(response)

    await adapter.complete([Message(role=Role.USER, content="hi")])

    span = _MOST_RECENT_SPAN[-1]
    usage_updates = [u for u in span.updates if "usage_details" in u]
    assert usage_updates, "complete span never recorded usage_details"
    details = usage_updates[-1]["usage_details"]
    assert details["input"] == 62000
    assert details["cache_read_input_tokens"] == 60000


async def test_structured_records_cached_content_tokens_in_usage(mock_sdk):
    """The structured path reports a cache hit the same way complete() does."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    response = _response(parts=[_part(text='{"ok": true}')])
    response.usage_metadata = _usage_with_cache(prompt=900, candidates=5, cached=700)
    mock_sdk.instance.set_result(response)

    await adapter.structured(
        [Message(role=Role.USER, content="q")],
        schema={"title": "X", "type": "object"},
    )

    details = [u for u in _MOST_RECENT_SPAN[-1].updates if "usage_details" in u][-1][
        "usage_details"
    ]
    assert details["cache_read_input_tokens"] == 700


async def test_stream_records_cached_content_tokens_in_usage(mock_sdk):
    """The cumulative usage of the last streamed chunk carries the cache hit."""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    last = _response(parts=[], finish_reason="STOP")
    last.usage_metadata = _usage_with_cache(prompt=62000, candidates=22, cached=60000)
    mock_sdk.instance.set_stream(
        [
            _response(parts=[_part(text="hi")], finish_reason=None),
            last,
        ]
    )

    async for _ in adapter.stream([Message(role=Role.USER, content="hi")]):
        pass

    details = [u for u in _MOST_RECENT_SPAN[-1].updates if "usage_details" in u][-1][
        "usage_details"
    ]
    assert details["cache_read_input_tokens"] == 60000


async def test_usage_without_cached_content_omits_cache_key(mock_sdk):
    """No cached_content_token_count at all ⇒ no fabricated cache key.

    Gemini leaves the field off entirely when nothing was served from cache, so
    reporting a 0 would read as "measured, missed" rather than "not reported".
    """
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response())  # plain usage, no cache field

    await adapter.complete([Message(role=Role.USER, content="hi")])

    details = [u for u in _MOST_RECENT_SPAN[-1].updates if "usage_details" in u][-1][
        "usage_details"
    ]
    assert "cache_read_input_tokens" not in details


async def test_usage_with_a_none_cached_content_omits_cache_key(mock_sdk):
    """``cached_content_token_count`` present but None ⇒ nothing was reported.

    It is Optional in the SDK's UsageMetadata, so "not reported" arrives either
    as an absent attribute or as an explicit None.
    """
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    response = _response()
    response.usage_metadata = _usage_with_cache(prompt=5, candidates=7, cached=None)
    mock_sdk.instance.set_result(response)

    await adapter.complete([Message(role=Role.USER, content="hi")])

    details = [u for u in _MOST_RECENT_SPAN[-1].updates if "usage_details" in u][-1][
        "usage_details"
    ]
    assert "cache_read_input_tokens" not in details
    assert details["input"] == 5


async def test_a_reported_zero_cache_hit_is_carried_as_a_zero(mock_sdk):
    """报了 0 就带着 0 —— 它跟"这次没有缓存数据"是两件事，压成一个形状就分不出了。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    response = _response()
    response.usage_metadata = _usage_with_cache(prompt=5, candidates=7, cached=0)
    mock_sdk.instance.set_result(response)

    await adapter.complete([Message(role=Role.USER, content="hi")])

    details = [u for u in _MOST_RECENT_SPAN[-1].updates if "usage_details" in u][-1][
        "usage_details"
    ]
    assert details["cache_read_input_tokens"] == 0


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


# ---------------------------------------------------------------------------
# supports_native_web_search — only Gemini 3 can co-host native google search
# with custom function declarations; Gemini 2.5 can't, so it stays False.
# Model-name normalisation strips a "models/" prefix and is case-insensitive;
# anything we can't recognise is treated as unsupported (fail-closed).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name",
    [
        "gemini-3.5-flash",  # main chat
        "gemini-3-flash-preview",  # diary
        "models/gemini-3.5-flash",  # SDK-style "models/" prefix
        "Gemini-3.5-Flash",  # mixed case
        "MODELS/Gemini-3-Pro",  # prefix + case
    ],
)
def test_supports_native_web_search_true_for_gemini_3(mock_sdk, model_name):
    adapter = GeminiAdapter(model_name=model_name, api_key="k", base_url="https://g")
    assert adapter.supports_native_web_search is True


@pytest.mark.parametrize(
    "model_name",
    [
        "gemini-2.5-flash",  # Gemini 2.5 can't co-host grounding + tools
        "models/gemini-2.5-pro",
        "Gemini-2.5-Flash",
        "gemini-2.0-flash",
        "gpt-4o",  # not Gemini at all
        "",  # unknown → fail-closed
    ],
)
def test_supports_native_web_search_false_for_non_gemini_3(mock_sdk, model_name):
    adapter = GeminiAdapter(model_name=model_name, api_key="k", base_url="https://g")
    assert adapter.supports_native_web_search is False


# ---------------------------------------------------------------------------
# native_web_search signal → request carries native google search tool
# (④). When the loop hands native_web_search=True, the adapter appends a
# Tool(google_search=GoogleSearch()) ALONGSIDE the custom function-declaration
# tool (Gemini 3 co-hosts both). Default (no signal) leaves only the function
# tool — non-Gemini-3 paths and the switch-off case are byte-for-byte unchanged.
# ---------------------------------------------------------------------------


def _split_tools(cfg: Any) -> tuple[list[Any], list[Any]]:
    """Partition a config's tools into (function-declaration tools, search tools)."""
    tools = cfg.tools or []
    func_tools = [t for t in tools if t.function_declarations]
    search_tools = [t for t in tools if getattr(t, "google_search", None) is not None]
    return func_tools, search_tools


async def test_complete_native_web_search_appends_google_search_tool(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    tools = [ToolDef(name="draw", description="d", parameters={"type": "object"})]
    await adapter.complete(
        [Message(role=Role.USER, content="weather?")],
        tools=tools,
        native_web_search=True,
    )

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    func_tools, search_tools = _split_tools(cfg)
    # custom function tool still present
    assert len(func_tools) == 1
    assert func_tools[0].function_declarations[0].name == "draw"
    # native google search co-hosted
    assert len(search_tools) == 1
    # Gemini 3 requires include_server_side_tool_invocations to combine a built-in
    # tool (google search) with function declarations — without it the API 400s
    # ("Please enable tool_config.include_server_side_tool_invocations ...").
    assert cfg.tool_config is not None
    assert cfg.tool_config.include_server_side_tool_invocations is True


async def test_stream_native_web_search_appends_google_search_tool(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream([_response(parts=[_part(text="ok")])])

    tools = [ToolDef(name="draw", description="d", parameters={"type": "object"})]
    async for _ in adapter.stream(
        [Message(role=Role.USER, content="weather?")],
        tools=tools,
        native_web_search=True,
    ):
        pass

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    func_tools, search_tools = _split_tools(cfg)
    assert len(func_tools) == 1
    assert len(search_tools) == 1


async def test_complete_without_native_web_search_has_no_google_search_tool(mock_sdk):
    """Default (no signal): only the custom function tool, no google_search."""
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    tools = [ToolDef(name="draw", description="d", parameters={"type": "object"})]
    await adapter.complete([Message(role=Role.USER, content="q")], tools=tools)

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    func_tools, search_tools = _split_tools(cfg)
    assert len(func_tools) == 1
    assert search_tools == []
    # no native search → no server-side-tool toggle (non-native path unchanged)
    assert cfg.tool_config is None


async def test_complete_native_web_search_false_has_no_google_search_tool(mock_sdk):
    """Explicit native_web_search=False is the same as no signal: no search tool."""
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    tools = [ToolDef(name="draw", description="d", parameters={"type": "object"})]
    await adapter.complete(
        [Message(role=Role.USER, content="q")],
        tools=tools,
        native_web_search=False,
    )

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    _, search_tools = _split_tools(cfg)
    assert search_tools == []


async def test_native_web_search_signal_not_leaked_into_config_fields(mock_sdk):
    """native_web_search is a control param, never a GenerateContentConfig field
    or a trace model_parameter — it's consumed, not passed through."""
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    await adapter.complete(
        [Message(role=Role.USER, content="q")],
        native_web_search=True,
    )

    assert "native_web_search" not in _span_calls[0]["model_parameters"]
    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    assert not hasattr(cfg, "native_web_search")


async def test_native_web_search_with_no_tools_still_appends_google_search(mock_sdk):
    """Even with no custom tools, the native search signal yields a search tool.

    (In practice main chat always carries tools, but _build_config must not gate
    the search tool on the function tool existing.)
    """
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="ok")]))

    await adapter.complete(
        [Message(role=Role.USER, content="q")],
        native_web_search=True,
    )

    cfg = mock_sdk.instance.last_generate_kwargs["config"]
    func_tools, search_tools = _split_tools(cfg)
    assert func_tools == []
    assert len(search_tools) == 1


# ---------------------------------------------------------------------------
# ⑤ grounding sources must not leak into赤尾's visible reply. The wire→neutral
# helpers only read part.text (skipping thoughts) and never touch
# candidate.grounding_metadata / search_entry_point. These fixtures attach
# grounding metadata (source urls / chunks / search-entry html) and assert the
# neutral content is the visible prose ONLY.
# ---------------------------------------------------------------------------

_SOURCE_URL = "https://example.com/grounded-source"
_SEARCH_ENTRY_HTML = '<div class="search-entry">google search suggestions</div>'


def _grounding_metadata() -> SimpleNamespace:
    """A canned grounding_metadata bundle with source urls + search entry point."""
    chunk = SimpleNamespace(
        web=SimpleNamespace(uri=_SOURCE_URL, title="Grounded Source")
    )
    return SimpleNamespace(
        grounding_chunks=[chunk],
        web_search_queries=["today's weather"],
        search_entry_point=SimpleNamespace(rendered_content=_SEARCH_ENTRY_HTML),
    )


def _grounded_response(visible_text: str) -> SimpleNamespace:
    """A response whose candidate carries grounding_metadata + visible prose."""
    candidate = SimpleNamespace(
        content=_content([_part(text=visible_text)]),
        finish_reason="STOP",
        index=0,
        grounding_metadata=_grounding_metadata(),
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=_usage())


async def test_complete_drops_grounding_metadata_from_visible_content(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _grounded_response("今天广州多云转晴，气温 28 度。")
    )

    out = await adapter.complete([Message(role=Role.USER, content="天气?")])

    assert out.content == "今天广州多云转晴，气温 28 度。"
    assert _SOURCE_URL not in out.content
    assert "example.com" not in out.content
    assert _SEARCH_ENTRY_HTML not in out.content
    assert "search-entry" not in out.content
    assert "web_search_queries" not in out.content


async def test_stream_drops_grounding_metadata_from_visible_content(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-3.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream(
        [_grounded_response("今天广州多云转晴，气温 28 度。")]
    )

    chunks = [
        c async for c in adapter.stream([Message(role=Role.USER, content="天气?")])
    ]
    text = "".join(c.text for c in chunks if c.text)

    assert text == "今天广州多云转晴，气温 28 度。"
    assert _SOURCE_URL not in text
    assert "example.com" not in text
    assert _SEARCH_ENTRY_HTML not in text
    assert "search-entry" not in text


# ---------------------------------------------------------------------------
# trace rendering — a thought part and a signature must be readable on the trace
#
# Without this a thought part renders exactly like a plain text part and a
# signature renders nowhere at all, so "did the thought block actually go out"
# can't be answered from langfuse. The signature is reported by size only: the
# raw bytes are an opaque provider blob and would bury the conversation.
# ---------------------------------------------------------------------------


def test_trace_marks_a_thought_part_apart_from_plain_text():
    from google.genai import types as genai_types

    from app.agent.adapters.gemini import _contents_for_trace

    rendered = _contents_for_trace(
        [
            genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(text="让我想想", thought=True),
                    genai_types.Part(text="好的"),
                ],
            )
        ]
    )

    assert rendered[0]["parts"] == [
        {"text": "让我想想", "thought": True},
        {"text": "好的"},
    ]


def test_trace_reports_a_signature_by_size_never_its_bytes():
    from google.genai import types as genai_types

    from app.agent.adapters.gemini import _contents_for_trace

    rendered = _contents_for_trace(
        [
            genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        text="让我想想",
                        thought=True,
                        thought_signature=b"\x00\xffopaque",
                    )
                ],
            )
        ]
    )

    part = rendered[0]["parts"][0]
    assert part["signature_bytes"] == len(b"\x00\xffopaque")
    assert "\\x00" not in repr(part)
    assert "opaque" not in repr(part).replace("signature_bytes", "")


def test_trace_marks_a_signature_on_a_function_call_part():
    from google.genai import types as genai_types

    from app.agent.adapters.gemini import _contents_for_trace

    part = genai_types.Part.from_function_call(name="search", args={"q": "cats"})
    part.thought_signature = b"sig-abc"

    rendered = _contents_for_trace([genai_types.Content(role="model", parts=[part])])

    assert rendered[0]["parts"][0] == {
        "function_call": {"name": "search", "args": {"q": "cats"}},
        "signature_bytes": len(b"sig-abc"),
    }


def test_trace_leaves_an_unsigned_part_unmarked():
    from google.genai import types as genai_types

    from app.agent.adapters.gemini import _contents_for_trace

    rendered = _contents_for_trace(
        [genai_types.Content(role="user", parts=[genai_types.Part(text="hi")])]
    )

    assert rendered[0]["parts"] == [{"text": "hi"}]


# ---------------------------------------------------------------------------
# the model turn is taken down as a sequence and put back as the same sequence
#
# Google's stateless contract: every thought block the model returned must come
# back verbatim on the next request, each with the signature that rode on it.
# Splitting the response into "all the text" + "all the calls" loses the order
# and the signature-to-segment mapping, and the model then continues from a
# turn it never had.
# ---------------------------------------------------------------------------


async def test_complete_keeps_thoughts_text_and_calls_interleaved(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[
                _part(text="先看看", thought=True, thought_signature=b"sig-t1"),
                _part(text="好"),
                _part(
                    function_call=_function_call("look_around", {}, "call_1"),
                    thought_signature=b"sig-c1",
                ),
                _part(text="再说一句", thought=True, thought_signature=b"sig-t2"),
                _part(
                    function_call=_function_call("say", {"words": "嗯"}, "call_2"),
                    thought_signature=b"sig-c2",
                ),
            ]
        )
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])

    assert [str(p.kind) for p in out.turn_parts] == [
        "thought",
        "text",
        "tool_call",
        "thought",
        "tool_call",
    ]
    assert [p.signature for p in out.turn_parts if p.kind == "thought"] == [
        b"sig-t1",
        b"sig-t2",
    ]
    assert [p.call_id for p in out.turn_parts if p.call_id] == ["call_1", "call_2"]
    assert [tc.signature for tc in out.tool_calls] == [b"sig-c1", b"sig-c2"]
    assert out.content == "好"


async def test_complete_keeps_each_thought_segment_apart(mock_sdk):
    """一轮多段思考：段界和每段自己的签名都不能被拼没。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[
                _part(text="第一段", thought=True, thought_signature=b"sig-a"),
                _part(text="第二段", thought=True, thought_signature=b"sig-b"),
                _part(text="说出来的"),
            ]
        )
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])

    thoughts = [p for p in out.turn_parts if p.kind == "thought"]
    assert [p.text for p in thoughts] == ["第一段", "第二段"]
    assert [p.signature for p in thoughts] == [b"sig-a", b"sig-b"]


async def test_a_signature_with_no_text_is_not_dropped(mock_sdk):
    """带签名但没正文的那一段仍然留着 —— 丢掉它就是丢掉一个签名。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(parts=[_part(text="", thought_signature=b"bare-sig")])
    )

    out = await adapter.complete([Message(role=Role.USER, content="q")])

    assert [p.signature for p in out.turn_parts] == [b"bare-sig"]


async def test_the_model_turn_goes_back_on_the_wire_as_it_came_off(mock_sdk):
    """wire → 中立层 → wire：段数、段序、thought 标记、签名字节逐个对上。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    returned = [
        _part(text="先看看", thought=True, thought_signature=b"sig-t1"),
        _part(text="好"),
        _part(
            function_call=_function_call("look_around", {}, "call_1"),
            thought_signature=b"sig-c1",
        ),
        _part(text="再想想", thought=True, thought_signature=b"sig-t2"),
    ]
    mock_sdk.instance.set_result(_response(parts=returned))
    turn = await adapter.complete([Message(role=Role.USER, content="q")])

    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))
    await adapter.complete(
        [
            Message(role=Role.USER, content="q"),
            turn,
            Message(role=Role.TOOL, content="看到了", tool_call_id="call_1"),
        ]
    )

    sent = mock_sdk.instance.last_generate_kwargs["contents"][1]
    assert sent.role == "model"
    assert len(sent.parts) == len(returned)
    assert [p.text for p in sent.parts] == ["先看看", "好", None, "再想想"]
    assert [bool(p.thought) for p in sent.parts] == [True, False, False, True]
    assert [p.thought_signature for p in sent.parts] == [
        b"sig-t1",
        None,
        b"sig-c1",
        b"sig-t2",
    ]
    assert sent.parts[2].function_call.name == "look_around"


async def test_a_replayed_thought_shows_up_on_the_trace(mock_sdk):
    """T1 的渲染 + T3 的回放合起来：trace 上看得见 thought part 真的发出去了。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(parts=[_part(text="想了想", thought=True, thought_signature=b"sig")])
    )
    turn = await adapter.complete([Message(role=Role.USER, content="q")])

    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))
    await adapter.complete([Message(role=Role.USER, content="q"), turn])

    traced = _span_calls[-1]["input"][1]["parts"]
    assert traced == [{"text": "想了想", "thought": True, "signature_bytes": 3}]


async def test_a_turn_nobody_recorded_a_sequence_for_still_goes_out(mock_sdk):
    """没有段序的消息（USER、旧转录读回来的那些）照旧按 content + tool_calls 编码。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))

    await adapter.complete(
        [
            Message(role=Role.USER, content="q"),
            Message(
                role=Role.ASSISTANT,
                content="我去搜一下",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="search",
                        arguments={"q": "cats"},
                        signature=b"sig-old",
                    )
                ],
            ),
            Message(role=Role.TOOL, content="3 results", tool_call_id="call_1"),
        ]
    )

    sent = mock_sdk.instance.last_generate_kwargs["contents"][1]
    assert [p.text for p in sent.parts] == ["我去搜一下", None]
    assert sent.parts[1].function_call.name == "search"
    assert sent.parts[1].thought_signature == b"sig-old"


async def test_a_call_stripped_off_the_turn_takes_its_part_with_it(mock_sdk):
    """调用被剥掉之后，段序里指向它的那一段不能还留在 wire 上。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(
            parts=[
                _part(text="想了想", thought=True, thought_signature=b"sig-t"),
                _part(function_call=_function_call("say", {}, "call_1")),
            ]
        )
    )
    turn = await adapter.complete([Message(role=Role.USER, content="q")])
    stripped = Message(
        role=Role.ASSISTANT,
        content=turn.content,
        turn_parts=turn.turn_parts,
    )

    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))
    await adapter.complete([Message(role=Role.USER, content="q"), stripped])

    sent = mock_sdk.instance.last_generate_kwargs["contents"][1]
    assert [bool(p.thought) for p in sent.parts] == [True]
    assert sent.parts[0].function_call is None


async def test_stream_carries_the_signature_on_a_thought_chunk(mock_sdk):
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_stream(
        [
            _response(
                parts=[_part(text="想", thought=True, thought_signature=b"sig-t")],
                finish_reason=None,
            ),
            _response(
                parts=[_part(text="好", thought_signature=b"sig-x")],
                finish_reason="STOP",
            ),
        ]
    )

    chunks = [c async for c in adapter.stream([Message(role=Role.USER, content="hi")])]

    thought = next(c for c in chunks if c.reasoning)
    assert thought.signature == b"sig-t"
    spoken = next(c for c in chunks if c.text)
    assert spoken.signature == b"sig-x"


# ---------------------------------------------------------------------------
# 流式收发一整圈：这一块里回来的是几段，回放出去就还是几段
#
# 段边界在 adapter 这一层是知道的（``_chunk_to_neutral`` 正在遍历 parts），到了
# 上层就只剩下一串 chunk。所以下面这一圈走的是真 adapter + 真 ReAct loop：一块里
# 两段思考进去，第二次请求的 model turn 里必须还是两段，签名各归各段。
# ---------------------------------------------------------------------------


@tool
async def look_around() -> str:
    """Look around and report what is there."""
    return "空无一人"


async def test_two_thought_parts_in_one_chunk_stay_two_parts_on_replay(mock_sdk):
    """同一块里回来的两段思考不能被并成一段，签名也不能跟着搬家。"""
    from app.agent.core import _stream_loop

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_streams(
        [
            [
                _response(
                    parts=[
                        _part(text="先想一下", thought=True),
                        _part(
                            text="再想一下",
                            thought=True,
                            thought_signature=b"sig-B",
                        ),
                        _part(
                            function_call=_function_call("look_around", {}, "call_1"),
                            thought_signature=b"sig-c",
                        ),
                    ],
                    finish_reason="STOP",
                )
            ],
            [_response(parts=[_part(text="好了")], finish_reason="STOP")],
        ]
    )

    async for _ in _stream_loop(
        adapter,
        messages=[Message(role=Role.USER, content="q")],
        tools=[look_around],
        context=None,
        recursion_limit=4,
    ):
        pass

    replayed = mock_sdk.instance.generate_calls[1]["contents"][1]
    assert replayed.role == "model"
    assert [p.text for p in replayed.parts] == ["先想一下", "再想一下", None]
    assert [bool(p.thought) for p in replayed.parts] == [True, True, False]
    assert [p.thought_signature for p in replayed.parts] == [
        None,
        b"sig-B",
        b"sig-c",
    ]
    assert replayed.parts[2].function_call.name == "look_around"


async def test_one_part_cut_across_chunks_is_still_one_part_on_replay(mock_sdk):
    """一段被切成多块传回来的，仍然合成一段——边界未知时照旧顺着签名收口。"""
    from app.agent.core import _stream_loop

    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_streams(
        [
            [
                _response(parts=[_part(text="先想", thought=True)], finish_reason=None),
                _response(
                    parts=[
                        _part(text="一下", thought=True, thought_signature=b"sig-A")
                    ],
                    finish_reason=None,
                ),
                _response(
                    parts=[
                        _part(
                            function_call=_function_call("look_around", {}, "call_1"),
                            thought_signature=b"sig-c",
                        )
                    ],
                    finish_reason="STOP",
                ),
            ],
            [_response(parts=[_part(text="好了")], finish_reason="STOP")],
        ]
    )

    async for _ in _stream_loop(
        adapter,
        messages=[Message(role=Role.USER, content="q")],
        tools=[look_around],
        context=None,
        recursion_limit=4,
    ):
        pass

    replayed = mock_sdk.instance.generate_calls[1]["contents"][1]
    assert [p.text for p in replayed.parts] == ["先想一下", None]
    assert [p.thought_signature for p in replayed.parts] == [b"sig-A", b"sig-c"]


async def test_a_signed_part_with_no_text_keeps_its_signature_on_the_wire(mock_sdk):
    """Google 文档说流式会回空正文的签名段：它发出去时必须还带着签名。"""
    adapter = GeminiAdapter(
        model_name="gemini-2.5-flash", api_key="k", base_url="https://g"
    )
    mock_sdk.instance.set_result(
        _response(parts=[_part(text="", thought_signature=b"bare-sig")])
    )
    turn = await adapter.complete([Message(role=Role.USER, content="q")])

    mock_sdk.instance.set_result(_response(parts=[_part(text="done")]))
    await adapter.complete([Message(role=Role.USER, content="q"), turn])

    sent = mock_sdk.instance.last_generate_kwargs["contents"][1]
    assert len(sent.parts) == 1
    assert sent.parts[0].thought_signature == b"bare-sig"
