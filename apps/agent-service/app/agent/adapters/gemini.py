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
``image_url`` block both become a Gemini *file_data* part (``from_uri``),
carrying the image by reference.

**Thinking.** Outbound we ask for thoughts via
``thinking_config.include_thoughts=True``; inbound, a response ``Part`` with
``thought=True`` is routed to ``Message.reasoning_content`` (non-stream) /
``StreamChunk.reasoning`` (stream), NOT into visible content — mirroring how
the OpenAI adapter handles deepseek ``reasoning_content``.

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

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

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


class GeminiAdapter(ModelClient):
    """Native Gemini adapter for client_type ``google``."""

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str | None,
        use_proxy: bool = False,
        **_extra: Any,
    ) -> None:
        self._model = model_name
        http_options = self._build_http_options(base_url, use_proxy)
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_http_options(base_url: str | None, use_proxy: bool) -> types.HttpOptions:
        """Build genai HttpOptions: base_url + retry-off + optional proxy."""
        opts: dict[str, Any] = {
            # attempts=1 ⇒ a single attempt, no SDK-side retry (Agent owns it).
            "retry_options": types.HttpRetryOptions(attempts=1),
        }
        if base_url:
            opts["base_url"] = base_url
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
        contents, system_instruction = self._to_wire_contents(messages)
        config = self._build_config(
            system_instruction=system_instruction, tools=tools, **kwargs
        )

        with generation_span(
            name="llm.complete",
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
        contents, system_instruction = self._to_wire_contents(messages)
        config = self._build_config(
            system_instruction=system_instruction, tools=tools, **kwargs
        )

        with generation_span(
            name="llm.stream",
            model=self._model,
            input=_contents_for_trace(contents),
            model_parameters=_model_parameters(kwargs),
        ) as span:
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            usage: dict[str, int] | None = None

            stream = await self._client.aio.models.generate_content_stream(
                model=self._model, contents=contents, config=config
            )
            async for chunk in stream:
                # Gemini reports cumulative usage_metadata per chunk; keep the
                # latest non-None so the final tally lands on the span (token
                # accounting must match the non-streaming complete() path).
                chunk_usage = _usage_details(chunk)
                if chunk_usage is not None:
                    usage = chunk_usage
                for out in _chunk_to_neutral(chunk):
                    if out.text:
                        text_parts.append(out.text)
                    if out.tool_call is not None:
                        tool_calls.append(out.tool_call)
                    yield out

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
        contents, system_instruction = self._to_wire_contents(messages)
        config = self._build_config(
            system_instruction=system_instruction,
            tools=None,
            response_mime_type="application/json",
            response_schema=schema,
            **kwargs,
        )

        with generation_span(
            name="llm.structured",
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
        if tools:
            cfg["tools"] = [
                types.Tool(
                    function_declarations=[_tool_to_declaration(t) for t in tools]
                )
            ]
        if response_mime_type is not None:
            cfg["response_mime_type"] = response_mime_type
        if response_schema is not None:
            cfg["response_schema"] = response_schema
        cfg.update(_passthrough_kwargs(kwargs))
        return types.GenerateContentConfig(**cfg)

    def _to_wire_contents(
        self, messages: list[Message]
    ) -> tuple[list[types.Content], str | None]:
        """neutral messages → (Gemini contents, system_instruction).

        System turns are hoisted to system_instruction (concatenated). A tool
        result needs the name of the call it answers, so we track call_id→name
        as we walk the assistant function_call turns.
        """
        contents: list[types.Content] = []
        system_parts: list[str] = []
        call_names: dict[str, str] = {}

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_parts.append(msg.text())
                continue
            if msg.role == Role.TOOL:
                contents.append(_tool_result_to_content(msg, call_names))
                continue

            role = "model" if msg.role == Role.ASSISTANT else "user"
            parts = _message_parts(msg)
            for tc in msg.tool_calls:
                call_names[tc.id] = tc.name
                parts.append(_tool_call_to_part(tc))
            contents.append(types.Content(role=role, parts=parts))

        system_instruction = "\n".join(p for p in system_parts if p) or None
        return contents, system_instruction


# ---------------------------------------------------------------------------
# neutral → wire helpers (module-level, pure)
# ---------------------------------------------------------------------------


def _message_parts(message: Message) -> list[types.Part]:
    """Build the content parts for a user/model message (text + images)."""
    content = message.content
    if isinstance(content, str):
        return [types.Part.from_text(text=content)] if content else []

    parts: list[types.Part] = []
    for block in content:
        part = _block_to_part(block)
        if part is not None:
            parts.append(part)
    return parts


def _block_to_part(block: ContentBlock) -> types.Part | None:
    """neutral ContentBlock → Gemini Part.

    ``text``      → text part
    ``image``     → file_data part (chat-history image, url)
    ``image_url`` → file_data part (OpenAI-style tool-returned block)
    """
    if block.type == "text":
        return types.Part.from_text(text=block.text or "")
    if block.type == "image" and block.url:
        return types.Part.from_uri(file_uri=block.url, mime_type="image/*")
    if block.type == "image_url" and block.image_url:
        url = block.image_url.get("url")
        if url:
            return types.Part.from_uri(file_uri=url, mime_type="image/*")
    return None


def _tool_call_to_part(tc: ToolCall) -> types.Part:
    part = types.Part.from_function_call(name=tc.name, args=tc.arguments)
    # Echo the opaque thought_signature back on the functionCall part; Gemini 2.5
    # thinking models 400 the next turn without it. Absent ⇒ leave it unset.
    if tc.signature is not None:
        part.thought_signature = tc.signature
    return part


def _tool_result_to_content(
    message: Message, call_names: dict[str, str]
) -> types.Content:
    """A neutral TOOL message → a user-role Content with a function_response part.

    Gemini's protocol delivers tool results as a user turn carrying a
    function_response named after the call. We recover the function name from
    the call id tracked while walking the assistant turns.

    Multimodal tool results (read_images / generate_image return image blocks)
    can't ride inside the function_response — that part is structured JSON, not
    image bytes. So the function_response carries the flattened text result, and
    each image block is appended to the SAME user turn as a Gemini image part,
    so the model still sees the image the tool returned (flattening to .text()
    alone would silently drop it).
    """
    name = call_names.get(message.tool_call_id or "", message.tool_call_id or "tool")
    parts: list[types.Part] = [
        types.Part.from_function_response(
            name=name, response={"result": message.text()}
        )
    ]
    if isinstance(message.content, list):
        for block in message.content:
            if block.type in ("image", "image_url"):
                img_part = _block_to_part(block)
                if img_part is not None:
                    parts.append(img_part)
    return types.Content(role="user", parts=parts)


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
    """A non-streaming Gemini response → neutral assistant Message."""
    parts = _candidate_parts(response)
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for part in parts:
        fc = getattr(part, "function_call", None)
        if fc is not None:
            tool_calls.append(_function_call_to_neutral(fc, _part_signature(part)))
            continue
        text = getattr(part, "text", None)
        if not text:
            continue
        if getattr(part, "thought", False):
            reasoning_parts.append(text)
        else:
            text_parts.append(text)

    return Message(
        role=Role.ASSISTANT,
        content="".join(text_parts),
        reasoning_content="".join(reasoning_parts) or None,
        tool_calls=tool_calls,
    )


def _chunk_to_neutral(chunk: Any) -> list[StreamChunk]:
    """One streaming Gemini chunk → a list of neutral StreamChunks."""
    out: list[StreamChunk] = []
    for part in _candidate_parts(chunk):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            out.append(
                StreamChunk(
                    tool_call=_function_call_to_neutral(fc, _part_signature(part))
                )
            )
            continue
        text = getattr(part, "text", None)
        if not text:
            continue
        if getattr(part, "thought", False):
            out.append(StreamChunk(reasoning=text))
        else:
            out.append(StreamChunk(text=text))

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
    if getattr(part, "text", None):
        return {"text": part.text}
    fc = getattr(part, "function_call", None)
    if fc is not None:
        return {"function_call": {"name": fc.name, "args": dict(fc.args or {})}}
    fr = getattr(part, "function_response", None)
    if fr is not None:
        return {"function_response": {"name": fr.name}}
    fd = getattr(part, "file_data", None)
    if fd is not None:
        return {"file_data": {"file_uri": getattr(fd, "file_uri", None)}}
    return {"part": "?"}


def _usage_details(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    return {
        "input": getattr(usage, "prompt_token_count", 0) or 0,
        "output": getattr(usage, "candidates_token_count", 0) or 0,
        "total": getattr(usage, "total_token_count", 0) or 0,
    }


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
