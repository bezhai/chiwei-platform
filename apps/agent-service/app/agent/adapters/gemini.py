"""Gemini native ModelClient adapter (client_type ``google``).

Symmetric to the OpenAI adapter (T2) but for Gemini's *native* wire, served by
the ``google-genai`` SDK (``google.genai.Client``). It keeps Gemini's two
native edges the OpenAI-compat gateway would blur: **multimodal** image parts
and **thinking** (the model's exposed thoughts). The three ``ModelClient``
methods map onto one generate-content shape each: ``complete``
(``generate_content``), ``stream`` (``generate_content_stream``), ``structured``
(``response_mime_type=application/json`` + ``response_schema`` → dict).

neutral ↔ wire translation decisions
------------------------------------

**Roles.** Gemini has only ``user`` / ``model`` turns plus a separate
``system_instruction``. So a neutral ``SYSTEM`` message is hoisted out of the
turn list into ``config.system_instruction``; ``ASSISTANT`` maps to ``model``;
``USER`` and ``TOOL`` both map to ``user`` turns (a tool result is a user-side
``function_response`` part, per Gemini's protocol).

**Multimodal.** A neutral ``image`` block (``url``) and an OpenAI-style
``image_url`` block both carry an http(s) (pre-signed TOS) url or a ``data:``
URI. Gemini does NOT fetch arbitrary http urls through ``file_data`` (only
gs:// / Files-API URIs) and rejects wildcard mime types, so — mirroring the
old ``langchain-google-genai`` path (``ImageBytesLoader.load_part``) — the
adapter *downloads* http(s) urls (and decodes ``data:`` URIs) to bytes and
sends them as an *inline_data* part with a concrete mime type. Because this
needs network I/O, ``neutral → wire`` content building is async. A download that
fails costs that one picture and nothing else: the part degrades to the text
``[图片：打不开]`` (see ``_image_url_to_part``). On a *tool result* the same bytes
ride one level in, as ``FunctionResponse.parts``, so one answered call stays one
part (see ``_tool_result_to_content``).

**Thinking.** Outbound we ask for thoughts via
``thinking_config.include_thoughts=True``. Inbound, the whole model turn is
taken down as a *sequence* (``Message.turn_parts``): thoughts, spoken text and
function calls in the order they came back, each keeping the opaque
``thought_signature`` that rode on it. A thought stays out of visible content
(it surfaces as ``StreamChunk.reasoning`` while streaming), but it is NOT
dropped: the next request resends the sequence verbatim
(``_model_turn_parts``), because Google requires a stateless caller to return
every thought block unaltered — the signatures are what the model continues its
reasoning from.

**Function calling.** Neutral ``ToolDef``s become a single Gemini ``Tool`` with
``function_declarations`` (raw JSON schema via ``parameters_json_schema``). A
model ``function_call`` part → neutral ``ToolCall`` (Gemini calls have no id, so
we synthesise a stable one and remember name↔id so the following
``function_response`` can name its call). ``automatic_function_calling.disable``
is set: the SDK must NOT execute tools — the Agent layer owns the ReAct loop.

**finish_reason.** Gemini ``FinishReason`` → neutral: ``SAFETY`` / ``RECITATION``
→ ``content_filter``, ``MAX_TOKENS`` → ``length``, ``STOP`` → ``stop``; function
calls are surfaced as ``tool_call`` chunks, not via finish_reason.

**Retry off** (``HttpRetryOptions(attempts=1)``): retry is the Agent layer's
sole responsibility (spec). **Proxy**: ``use_proxy`` providers route the genai
http client through ``settings.forward_proxy_url`` (sync + async client args).
**Trace**: every call wraps a ``generation_span`` (always — see
``app.agent.trace``).
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from google import genai
from google.genai import types

from app.agent.client import ModelClient, register_adapter
from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolDef,
    TurnPart,
    TurnPartKind,
)
from app.agent.trace import generation_span
from app.infra.config import settings

logger = logging.getLogger(__name__)


# Gemini FinishReason → neutral StreamChunk.finish_reason. Unmapped reasons
# (LANGUAGE / OTHER / BLOCKLIST / ...) fall through to "stop": the turn ended,
# the loop should not treat them as a filter/length signal.
_FINISH_REASON_MAP: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
}


# What a picture that can't be retrieved reads as on the wire. Same wording as
# ``app.living.phone`` gives a picture it couldn't sign a url for: one picture
# she can't see is one thing to her, whether the phone already knew or the
# download failed here. Never a bare "[图片]" — that reads to the model exactly
# like "a picture I can see", and it will describe one it never saw.
_PICTURE_SHUT = "[图片：打不开]"


class GeminiAdapter(ModelClient):
    """Native Gemini adapter for client_type ``google``."""

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str | None,
        use_proxy: bool = False,
        api_version: str | None = None,
        **_extra: Any,
    ) -> None:
        self._model = model_name
        http_options = self._build_http_options(base_url, use_proxy, api_version)
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    # ------------------------------------------------------------------
    # native web search capability
    # ------------------------------------------------------------------

    @property
    def supports_native_web_search(self) -> bool:
        """Only Gemini 3 can co-host native google search with custom tools.

        Gemini 2.5's native grounding and custom function declarations can't
        live in the same request, and main chat always carries custom tools, so
        2.5 (and non-Gemini) report ``False``. The model name is normalised
        (drop a ``models/`` prefix, lower-case) before the ``gemini-3`` check;
        anything unrecognised stays ``False`` (fail-closed — better to fall back
        to ``search_web`` than send a request the provider would reject).
        """
        name = self._model.strip().lower()
        if name.startswith("models/"):
            name = name[len("models/") :]
        return name.startswith("gemini-3")

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_http_options(
        base_url: str | None,
        use_proxy: bool,
        api_version: str | None = None,
    ) -> types.HttpOptions:
        """Build genai HttpOptions: base_url + retry-off + optional proxy/version."""
        opts: dict[str, Any] = {
            # attempts=1 ⇒ a single attempt, no SDK-side retry (Agent owns it).
            "retry_options": types.HttpRetryOptions(attempts=1),
        }
        if base_url:
            opts["base_url"] = base_url
        if api_version:
            # The SDK appends {api_version}/models/... to base_url, so a
            # provider routing only one version can't be reached by baking it
            # into base_url. Left unset, the SDK picks its own default.
            opts["api_version"] = api_version
        if use_proxy and settings.forward_proxy_url:
            proxy_args = {"proxy": settings.forward_proxy_url}
            opts["client_args"] = proxy_args
            opts["async_client_args"] = proxy_args
        return types.HttpOptions(**opts)

    # ------------------------------------------------------------------
    # ModelClient: complete (non-streaming)
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        kwargs.pop("session_id", None)  # prompt-cache control param; not a Gemini arg
        native_search = kwargs.pop("native_web_search", False)
        contents, system_instruction = await self._to_wire_contents(messages)
        config = self._build_config(
            system_instruction=system_instruction,
            tools=tools,
            native_web_search=native_search,
            **kwargs,
        )

        with generation_span(
            name=self._model,
            model=self._model,
            input=_contents_for_trace(contents),
            model_parameters=_model_parameters(kwargs),
        ) as span:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
            message = _response_to_message(response)
            span.update(
                output=message.to_dict(),
                usage_details=_usage_details(response),
            )
        return message

    # ------------------------------------------------------------------
    # ModelClient: stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        kwargs.pop("session_id", None)  # prompt-cache control param; not a Gemini arg
        native_search = kwargs.pop("native_web_search", False)
        contents, system_instruction = await self._to_wire_contents(messages)
        config = self._build_config(
            system_instruction=system_instruction,
            tools=tools,
            native_web_search=native_search,
            **kwargs,
        )

        with generation_span(
            name=self._model,
            model=self._model,
            input=_contents_for_trace(contents),
            model_parameters=_model_parameters(kwargs),
        ) as span:
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            usage: dict[str, int] | None = None

            try:
                stream = await self._client.aio.models.generate_content_stream(
                    model=self._model, contents=contents, config=config
                )
                async for chunk in stream:
                    # Gemini reports cumulative usage_metadata per chunk; keep
                    # the latest one that said anything so the final tally lands
                    # on the span (token accounting must match the non-streaming
                    # complete() path). A chunk whose usage object reported no
                    # field at all is skipped rather than allowed to overwrite a
                    # real tally with nothing.
                    chunk_usage = _usage_details(chunk)
                    if chunk_usage:
                        usage = chunk_usage
                    for out in _chunk_to_neutral(chunk):
                        if out.text:
                            text_parts.append(out.text)
                        if out.tool_call is not None:
                            tool_calls.append(out.tool_call)
                        yield out
            finally:
                # However this stream ends — drained, raising, or abandoned by a
                # consumer that stops pulling (render_chat_turn does exactly that
                # on a content_filter chunk) — the tokens already reported are
                # what the call cost. Recording only after a clean drain loses
                # the whole call: no tokens, and no "the provider reported
                # nothing" either.
                span.update(
                    output={
                        "text": "".join(text_parts),
                        "tool_calls": [tc.to_dict() for tc in tool_calls],
                    },
                    usage_details=usage,
                )

    # ------------------------------------------------------------------
    # ModelClient: structured
    # ------------------------------------------------------------------

    async def structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs.pop("session_id", None)  # prompt-cache control param; not a Gemini arg
        contents, system_instruction = await self._to_wire_contents(messages)
        config = self._build_config(
            system_instruction=system_instruction,
            tools=None,
            response_mime_type="application/json",
            response_schema=schema,
            **kwargs,
        )

        with generation_span(
            name=self._model,
            model=self._model,
            input=_contents_for_trace(contents),
            model_parameters=_model_parameters(kwargs),
        ) as span:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
            text = _join_text(response) or "{}"
            data = json.loads(text)
            span.update(output=data, usage_details=_usage_details(response))
        return data

    # ------------------------------------------------------------------
    # config + neutral → wire contents
    # ------------------------------------------------------------------

    def _build_config(
        self,
        *,
        system_instruction: str | None,
        tools: list[ToolDef] | None,
        native_web_search: bool = False,
        response_mime_type: str | None = None,
        response_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> types.GenerateContentConfig:
        cfg: dict[str, Any] = {
            # always ask for thoughts; route thought parts → reasoning.
            "thinking_config": types.ThinkingConfig(include_thoughts=True),
            # the SDK must never run tools — the Agent layer owns the loop.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        }
        if system_instruction is not None:
            cfg["system_instruction"] = system_instruction
        # Custom function tools and Gemini's native google search co-exist as two
        # separate Tool entries (Gemini 3 supports both in one request). Only the
        # runtime turns native_web_search on, and only for main chat on Gemini 3.
        tool_list: list[types.Tool] = []
        if tools:
            tool_list.append(
                types.Tool(
                    function_declarations=[_tool_to_declaration(t) for t in tools]
                )
            )
        if native_web_search:
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))
            # Gemini 3 rejects a built-in tool (google search) combined with
            # function declarations unless server-side tool invocations are
            # explicitly enabled, 400-ing otherwise ("Please enable
            # tool_config.include_server_side_tool_invocations ..."). Set it only
            # on the native-search path so every other request is unchanged.
            cfg["tool_config"] = types.ToolConfig(
                include_server_side_tool_invocations=True
            )
        if tool_list:
            cfg["tools"] = tool_list
        if response_mime_type is not None:
            cfg["response_mime_type"] = response_mime_type
        if response_schema is not None:
            cfg["response_schema"] = response_schema
        cfg.update(_passthrough_kwargs(kwargs))
        return types.GenerateContentConfig(**cfg)

    async def _to_wire_contents(
        self, messages: list[Message]
    ) -> tuple[list[types.Content], str | None]:
        """neutral messages → (Gemini contents, system_instruction).

        System turns are hoisted to system_instruction (concatenated). A tool
        result needs the name of the call it answers, so we track call_id→name
        as we walk the assistant function_call turns. Async because image blocks
        are downloaded to inline bytes (see module docstring).

        Gemini requires the function_response parts answering a model turn to
        match that turn's function_call parts in number AND to arrive as a
        single user turn; a model turn with N calls answered by N separate user
        turns is rejected with 400 INVALID_ARGUMENT. The neutral layer carries
        one TOOL message per result (OpenAI's shape), so consecutive TOOL
        messages are merged into the open tool-result Content. Any other message
        closes it — results from two different rounds must stay apart, since
        each answers its own model turn.
        """
        contents: list[types.Content] = []
        system_parts: list[str] = []
        call_names: dict[str, str] = {}
        open_tool_turn: types.Content | None = None

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_parts.append(msg.text())
                continue
            if msg.role == Role.TOOL:
                turn = await _tool_result_to_content(msg, call_names)
                if open_tool_turn is None:
                    open_tool_turn = turn
                    contents.append(turn)
                else:
                    open_tool_turn.parts.extend(turn.parts or [])
                continue

            open_tool_turn = None
            role = "model" if msg.role == Role.ASSISTANT else "user"
            for tc in msg.tool_calls:
                call_names[tc.id] = tc.name
            if msg.turn_parts:
                parts = _model_turn_parts(msg)
            else:
                parts = await _message_parts(msg)
                parts.extend(_tool_call_to_part(tc) for tc in msg.tool_calls)
            contents.append(types.Content(role=role, parts=parts))

        system_instruction = "\n".join(p for p in system_parts if p) or None
        return contents, system_instruction


# ---------------------------------------------------------------------------
# neutral → wire helpers (module-level, pure)
# ---------------------------------------------------------------------------


def _model_turn_parts(message: Message) -> list[types.Part]:
    """Rebuild a model turn on the wire exactly as it came off it.

    Google requires a stateless caller to resend every thought block the model
    produced, unaltered and with its signature attached, or the model loses the
    reasoning it was continuing from. So the sequence recorded on the neutral
    message (:class:`~app.agent.neutral.TurnPart`) is walked in order: a thought
    part goes back as ``thought=True`` + its signature, a text part as text (a
    signature can ride on one of those too), a tool_call part as the
    functionCall part of the call it points at.

    A part pointing at a call that is no longer on the message is skipped: the
    call was stripped deliberately (the ReAct loop does this when it closes a
    run), and a functionCall part with nothing answering it makes the provider
    reject the whole request.
    """
    calls = {tc.id: tc for tc in message.tool_calls}
    parts: list[types.Part] = []
    for part in message.turn_parts:
        if part.kind is TurnPartKind.TOOL_CALL:
            call = calls.get(part.call_id or "")
            if call is not None:
                parts.append(_tool_call_to_part(call))
            continue
        wire = types.Part(
            text=part.text,
            thought=True if part.kind is TurnPartKind.THOUGHT else None,
        )
        if part.signature is not None:
            wire.thought_signature = part.signature
        parts.append(wire)
    return parts


async def _message_parts(message: Message) -> list[types.Part]:
    """Build the content parts for a user/model message (text + images)."""
    content = message.content
    if isinstance(content, str):
        return [types.Part.from_text(text=content)] if content else []

    parts: list[types.Part] = []
    for block in content:
        part = await _block_to_part(block)
        if part is not None:
            parts.append(part)
    return parts


async def _block_to_part(block: ContentBlock) -> types.Part | None:
    """neutral ContentBlock → Gemini Part.

    ``text``      → text part
    ``image``     → inline_data part (chat-history image; url downloaded)
    ``image_url`` → inline_data part (OpenAI-style tool-returned block)
    """
    if block.type == "text":
        return types.Part.from_text(text=block.text or "")
    url = _image_block_url(block)
    if url:
        return await _image_url_to_part(url)
    return None


def _image_block_url(block: ContentBlock) -> str | None:
    """Pull the image reference (http(s) / data: / gs:// url) out of a block."""
    if block.type == "image":
        return block.url
    if block.type == "image_url" and block.image_url:
        return block.image_url.get("url")
    return None


async def _image_url_to_part(url: str) -> types.Part:
    """Resolve an image reference to a Gemini Part.

    ``data:`` URIs are decoded locally; ``gs://`` URIs are passed by reference
    (the one case Gemini fetches itself); everything else (our pre-signed TOS
    http(s) urls) is downloaded to bytes and sent inline — Gemini won't fetch
    arbitrary http urls and rejects wildcard mime types.

    A download that fails — expired signature, network trouble, TOS down, the
    object deleted — costs that one picture and nothing else: the part becomes
    the text :data:`_PICTURE_SHUT`. This runs *before* the model is called and
    the history carrying the picture is replayed on every wakeup, so raising
    here would end the turn with nothing said, again and again, and she'd never
    reach the point of calling a tool to fetch something else.

    Only the retrieval is caught. Decoding a ``data:`` URI, or anything else
    raising in this function, is a defect in what we handed ourselves: it fails
    identically every time, no retry or tool call recovers it, and swallowing it
    would mean losing pictures with no one ever finding out.
    """
    if url.startswith("data:"):
        data, mime = _decode_data_uri(url)
        return types.Part(inline_data=types.Blob(data=data, mime_type=mime))
    if url.startswith("gs://"):
        mime, _ = mimetypes.guess_type(url)
        return types.Part(file_data=types.FileData(file_uri=url, mime_type=mime))
    try:
        data, mime = await _fetch_remote_image(url)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.warning(
            "gemini: image %s not fetched (%s), sent as %s",
            _image_ref(url),
            _fetch_failure(exc),
            _PICTURE_SHUT,
        )
        return types.Part.from_text(text=_PICTURE_SHUT)
    return types.Part(inline_data=types.Blob(data=data, mime_type=mime))


async def _fetch_remote_image(url: str) -> tuple[bytes, str]:
    """Download an http(s) image to (bytes, concrete mime type)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return resp.content, _normalise_image_mime(resp.headers.get("content-type"), url)


def _image_ref(url: str) -> str:
    """How a log names one picture: the object path, without the signed query.

    The query carries the TOS pre-signature — a credential that doesn't belong
    in a log line, and the path alone already says which picture it was.
    """
    return url.split("?", 1)[0]


def _fetch_failure(exc: Exception) -> str:
    """Why a download failed, short enough to read in a log line.

    A status error reports the code (403 = the signature expired or the object
    is no longer readable, 404 = it's gone); a transport error reports its class
    (``ReadTimeout``, ``ConnectError``, ...). httpx's own message would drag the
    full signed url in with it.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    """Decode a ``data:<mime>;base64,<payload>`` URI to (bytes, mime)."""
    header, _, payload = uri.partition(",")
    meta = header[len("data:") :] if header.startswith("data:") else ""
    mime = meta.split(";")[0] if meta else ""
    return base64.b64decode(payload), _normalise_image_mime(mime)


def _normalise_image_mime(content_type: str | None, url: str | None = None) -> str:
    """Pick a concrete image mime Gemini accepts (it rejects ``image/*``)."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime.startswith("image/") and mime != "image/*":
        return mime
    guessed, _ = mimetypes.guess_type(url or "")
    if guessed:
        return "image/jpeg" if guessed == "image/jpg" else guessed
    return "image/jpeg"


def _tool_call_to_part(tc: ToolCall) -> types.Part:
    part = types.Part.from_function_call(name=tc.name, args=tc.arguments)
    # Echo the opaque thought_signature back on the functionCall part; Gemini 2.5
    # thinking models 400 the next turn without it. Absent ⇒ leave it unset.
    if tc.signature is not None:
        part.thought_signature = tc.signature
    return part


async def _tool_result_to_content(
    message: Message, call_names: dict[str, str]
) -> types.Content:
    """A neutral TOOL message → a user-role Content with one function_response part.

    Gemini's protocol delivers tool results as a user turn carrying a
    function_response named after the call. We recover the function name from
    the call id tracked while walking the assistant turns.

    Multimodal tool results (a tool handing back pictures returns image blocks)
    carry their bytes *inside* the function_response, in ``FunctionResponse.parts``
    — the shape Gemini documents for tool-returned media. The structured
    ``response`` still holds the flattened text (flattening to .text() alone
    would silently drop the pictures). Nesting keeps the answer to one call at
    exactly one part however many pictures came back, which is what Gemini
    counts against the model turn's function_call parts.

    A picture the tool handed back that wouldn't download degrades the same way
    as anywhere else, but ``FunctionResponsePart`` carries only media — so the
    :data:`_PICTURE_SHUT` line rides in the answer's text, the one place inside a
    function_response that can say a picture was there.
    """
    name = call_names.get(message.tool_call_id or "", message.tool_call_id or "tool")
    media: list[types.FunctionResponsePart] = []
    shut: list[str] = []
    if isinstance(message.content, list):
        for block in message.content:
            if block.type not in ("image", "image_url"):
                continue
            img_part = await _block_to_part(block)
            blob = getattr(img_part, "inline_data", None)
            if blob is None:
                if getattr(img_part, "text", None) == _PICTURE_SHUT:
                    # The download failed and already logged why; carry the
                    # placeholder into the answer's text.
                    shut.append(_PICTURE_SHUT)
                    continue
                # Only inline bytes nest into a function response. A block with
                # no usable url, or a by-reference gs:// image, has nothing to
                # nest — say it was dropped instead of going out blind.
                logger.warning(
                    "gemini: tool result %s carried an image with no inline bytes, "
                    "dropping it",
                    name,
                )
                continue
            media.append(
                types.FunctionResponsePart(
                    inline_data=types.FunctionResponseBlob(
                        data=blob.data, mime_type=blob.mime_type
                    )
                )
            )
    response = types.FunctionResponse(
        name=name,
        response={"result": "\n".join(p for p in [message.text(), *shut] if p)},
        # Unset, not empty: an empty list is not None, so it would serialise a
        # "parts": [] onto every text-only tool result.
        parts=media or None,
    )
    return types.Content(role="user", parts=[types.Part(function_response=response)])


def _tool_to_declaration(tool: ToolDef) -> types.FunctionDeclaration:
    """neutral ToolDef → Gemini FunctionDeclaration (raw JSON schema)."""
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description,
        parameters_json_schema=tool.parameters,
    )


# ---------------------------------------------------------------------------
# wire → neutral helpers
# ---------------------------------------------------------------------------


def _response_to_message(response: Any) -> Message:
    """A non-streaming Gemini response → neutral assistant Message.

    The response parts are taken down **as a sequence**: thoughts, spoken text
    and function calls keep the order and the boundaries they arrived in, and
    each part keeps the ``thought_signature`` that rode on it. That whole
    sequence is what the next request has to send back (see
    :func:`_model_turn_parts`).

    A part with neither text nor a function call carries nothing to replay and
    is dropped — unless it carries a signature, which has to go back even when
    the part it sat on is empty.
    """
    parts: list[TurnPart] = []
    tool_calls: list[ToolCall] = []

    for part in _candidate_parts(response):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            call = _function_call_to_neutral(fc, _part_signature(part))
            tool_calls.append(call)
            parts.append(TurnPart.from_tool_call(call))
            continue
        text = getattr(part, "text", None)
        signature = _part_signature(part)
        if not text and signature is None:
            continue
        if getattr(part, "thought", False):
            parts.append(TurnPart.from_thought(text or "", signature=signature))
        else:
            parts.append(TurnPart.from_text(text or "", signature=signature))

    return Message.from_model_turn(parts, tool_calls)


def _chunk_to_neutral(chunk: Any) -> list[StreamChunk]:
    """One streaming Gemini chunk → a list of neutral StreamChunks.

    Each chunk carries the signature of the part it came from, so the ReAct
    loop can rebuild the streamed turn with its signatures intact.

    The part boundaries inside one wire chunk are known *here* and nowhere
    above: this loop is walking the provider's parts, while the loop rebuilding
    the turn only ever sees a flat run of chunks. So every part after the first
    one this chunk emits is marked ``starts_part`` — two parts that arrived in
    one chunk stay two parts, whatever their signatures look like. The first one
    is left unmarked: it may be the continuation of a part the previous chunk
    opened, and that is the one boundary this layer genuinely cannot see.
    """
    out: list[StreamChunk] = []
    emitted = 0
    for part in _candidate_parts(chunk):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            out.append(
                StreamChunk(
                    tool_call=_function_call_to_neutral(fc, _part_signature(part)),
                    starts_part=emitted > 0,
                )
            )
            emitted += 1
            continue
        text = getattr(part, "text", None)
        signature = _part_signature(part)
        if not text and signature is None:
            continue
        if getattr(part, "thought", False):
            out.append(
                StreamChunk(
                    reasoning=text or "",
                    signature=signature,
                    starts_part=emitted > 0,
                )
            )
        else:
            out.append(
                StreamChunk(
                    text=text or "", signature=signature, starts_part=emitted > 0
                )
            )
        emitted += 1

    finish = _finish_reason(chunk)
    if finish is not None:
        out.append(StreamChunk(finish_reason=finish))
    return out


def _candidate_parts(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    if content is None:
        return []
    return getattr(content, "parts", None) or []


def _function_call_to_neutral(fc: Any, signature: bytes | None = None) -> ToolCall:
    """Gemini function_call → neutral ToolCall (synthesise id when absent).

    ``signature`` is the part's ``thought_signature`` (Gemini 2.5 thinking
    models). It must travel with the call so the next turn can echo it back;
    omitting it 400s the following request.
    """
    call_id = getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:12]}"
    args = getattr(fc, "args", None) or {}
    return ToolCall(
        id=call_id,
        name=getattr(fc, "name", ""),
        arguments=dict(args),
        signature=signature,
    )


def _part_signature(part: Any) -> bytes | None:
    """The opaque ``thought_signature`` Gemini attaches to a functionCall part."""
    return getattr(part, "thought_signature", None)


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    raw = getattr(candidates[0], "finish_reason", None)
    if raw is None:
        return None
    # FinishReason may arrive as an enum (has .name) or a plain string.
    key = getattr(raw, "name", None) or str(raw)
    return _FINISH_REASON_MAP.get(key, "stop")


def _join_text(response: Any) -> str:
    """Concatenate all non-thought text parts (for structured JSON parsing)."""
    return "".join(
        getattr(p, "text", "") or ""
        for p in _candidate_parts(response)
        if not getattr(p, "thought", False)
    )


# ---------------------------------------------------------------------------
# kwargs / trace helpers
# ---------------------------------------------------------------------------

# Neutral model-behaviour kwargs → GenerateContentConfig field names. The
# thinking core passes openai-style kwargs (max_tokens, etc.); map the ones
# Gemini names differently, pass the rest through by exact name.
_KWARG_RENAME = {"max_tokens": "max_output_tokens"}
_PASSTHROUGH = frozenset({"temperature", "top_p", "max_output_tokens"})


def _passthrough_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        name = _KWARG_RENAME.get(k, k)
        if name in _PASSTHROUGH:
            out[name] = v
    return out


def _model_parameters(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Trace-worthy model params (the behaviour kwargs the caller passed)."""
    return dict(kwargs)


def _contents_for_trace(contents: list[types.Content]) -> Any:
    """Render wire contents to plain dicts for the langfuse input field."""
    rendered: list[dict[str, Any]] = []
    for c in contents:
        rendered.append(
            {
                "role": c.role,
                "parts": [_part_for_trace(p) for p in (c.parts or [])],
            }
        )
    return rendered


def _part_for_trace(part: types.Part) -> dict[str, Any]:
    """One wire part, for the trace: what it carries, plus its thinking marks.

    ``thought`` and ``thought_signature`` are rendered here because they are the
    only way to tell from a trace whether a thought block actually went out: a
    thought part carries plain text and would otherwise read as ordinary text,
    and a signature would not appear at all. The signature is reported by size
    only — it is an opaque provider blob, same rendering rule as a picture's
    bytes (:func:`_blob_for_trace`).
    """
    rendered = _part_body_for_trace(part)
    if getattr(part, "thought", False):
        rendered["thought"] = True
    signature = getattr(part, "thought_signature", None)
    if signature:
        rendered["signature_bytes"] = len(signature)
    return rendered


def _part_body_for_trace(part: types.Part) -> dict[str, Any]:
    if getattr(part, "text", None):
        return {"text": part.text}
    fc = getattr(part, "function_call", None)
    if fc is not None:
        return {"function_call": {"name": fc.name, "args": dict(fc.args or {})}}
    fr = getattr(part, "function_response", None)
    if fr is not None:
        rendered: dict[str, Any] = {"name": fr.name}
        # The pictures a tool handed back live inside the function_response, so
        # this is the only place a trace can show they went out at all.
        media = [getattr(p, "inline_data", None) for p in (fr.parts or [])]
        if media:
            rendered["images"] = [_blob_for_trace(b) for b in media]
        return {"function_response": rendered}
    inline = getattr(part, "inline_data", None)
    if inline is not None:
        return {"inline_data": _blob_for_trace(inline)}
    fd = getattr(part, "file_data", None)
    if fd is not None:
        return {"file_data": {"file_uri": getattr(fd, "file_uri", None)}}
    return {"part": "?"}


def _blob_for_trace(blob: Any) -> dict[str, Any]:
    """One picture on the wire, for the trace: what it is and how big.

    The bytes themselves never go to the trace — a base64 image would bury the
    conversation it belongs to.
    """
    data = getattr(blob, "data", b"") or b""
    return {"mime_type": getattr(blob, "mime_type", None), "bytes": len(data)}


# neutral usage key → the google-genai field it is read from.
#
#   - cache_read_input_tokens: an implicit-cache hit. Gemini counts the prompt
#     tokens served from cache here, and prompt_token_count already includes
#     them (so this is a slice of "input", not an addition to it). Reported
#     under the same key as the OpenAI adapter — trace's per-round accumulator
#     and ThinkingTokensSpent both read it by name.
#   - thinking_tokens: billed on top of candidates_token_count (it is the
#     residual in total - input - output), so it is its own dimension, not a
#     slice.
_USAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("input", "prompt_token_count"),
    ("output", "candidates_token_count"),
    ("total", "total_token_count"),
    ("cache_read_input_tokens", "cached_content_token_count"),
    ("thinking_tokens", "thoughts_token_count"),
)


def _usage_details(response: Any) -> dict[str, int] | None:
    """What this response reported it cost, one key per dimension it reported.

    A reported 0 is carried; a field the provider never sent leaves its key off
    entirely. "Measured, and it was zero" and "the provider said nothing about
    this" are different facts, and writing a 0 in for the second turns it into
    the first — which is exactly how the cache hit rate came to read as a solid
    0 for calls that carried no cache data at all. Every dimension follows that
    one rule. ``None`` (no usage object at all) means the response reported
    nothing whatsoever.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    details: dict[str, int] = {}
    for key, attr in _USAGE_FIELDS:
        value = getattr(usage, attr, None)
        if value is not None:
            details[key] = int(value)
    return details


# ---------------------------------------------------------------------------
# Registration — one adapter class for client_type "google"
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    model_name: str,
    api_key: str,
    base_url: str | None,
    **extra: Any,
) -> GeminiAdapter:
    return GeminiAdapter(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        **extra,
    )


register_adapter("google", _make_adapter)
