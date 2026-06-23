"""OpenAI-family ModelClient adapter.

One adapter class serves every openai-compatible provider in prod; the variant
is a constructor parameter (``client_type``), not a separate code path:

  - ``openai``            — plain Chat Completions (default),
  - ``deepseek``          — Chat Completions + reasoning_content (both ways),
  - ``azure-http``        — AsyncAzureOpenAI auth,
  - ``openai-responses``  — grok / xAI; served via Chat Completions (see below).

It translates neutral types (``app.agent.neutral``) ↔ OpenAI chat-completions
wire, using the ``openai`` SDK's ``AsyncOpenAI`` / ``AsyncAzureOpenAI``. The
three ``ModelClient`` methods map to one ``chat.completions.create`` shape each:
``complete`` (non-stream), ``stream`` (stream=True), ``structured``
(response_format=json_schema → dict).

**grok / openai-responses → Chat Completions.** xAI's ``/v1/chat/completions``
is fully OpenAI-SDK-compatible and still functional (only marked "legacy"). The
only Responses-API-exclusive features are *stateful* server-side conversations
(previous_response_id), encrypted-reasoning carryover, and context compaction —
none of which the thinking core uses: we own the ReAct loop and resend the full
stateless message history every turn. So ``openai-responses`` is served through
the same Chat Completions path here; no separate Responses adapter is needed.

**reasoning_content (deepseek)** is replicated from the legacy
``_ReasoningChatOpenAI`` onto neutral types:
  - *out*: read ``message.reasoning_content`` from the raw response into
    ``Message.reasoning_content``;
  - *in*: re-inject an assistant message's ``reasoning_content`` into the wire
    payload and normalise every message's content to a plain string (deepseek
    rejects array / null content).

**Retry is off** (``max_retries=0``): retry is the Agent layer's sole
responsibility (spec). **Proxy**: ``use_proxy`` providers get an httpx client
configured with ``settings.forward_proxy_url``. **Trace**: every call wraps a
``generation_span`` (always — see ``app.agent.trace``).
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.agent.client import ModelClient, register_adapter
from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    StreamChunk,
    ToolCall,
    ToolDef,
    normalize_content_to_text,
)
from app.agent.trace import generation_span
from app.infra.config import settings

logger = logging.getLogger(__name__)

_AZURE_API_VERSION = "2024-08-01-preview"

# 字节 GPT openapi 网关做隐式 prompt cache:请求带上 retention 窗口(body)+ 稳定的
# session_id(``extra`` JSON header),让长而稳定的前缀跨唤醒复用。网关只接受枚举值
# ``in_memory`` / ``24h``(传 "3600s" 这类秒数会被 400 拒绝,param 校验);取 24h
# 远超 world 10~30min 的唤醒间隔——默认窗口太短,下次醒来缓存已过期就是 0 命中。
_PROMPT_CACHE_RETENTION = "24h"


class OpenAIAdapter(ModelClient):
    """Chat-Completions adapter for every openai-compatible provider."""

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str | None,
        client_type: str = "openai",
        use_proxy: bool = False,
        **_extra: Any,
    ) -> None:
        self._model = model_name
        self._client_type = client_type
        self._is_deepseek = client_type == "deepseek"

        http_client = self._build_http_client(use_proxy)

        if client_type == "azure-http":
            self._client: AsyncOpenAI | AsyncAzureOpenAI = AsyncAzureOpenAI(
                azure_endpoint=base_url or "",
                api_key=api_key,
                api_version=_AZURE_API_VERSION,
                azure_deployment=model_name,
                max_retries=0,
                http_client=http_client,
            )
        else:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                http_client=http_client,
            )

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_http_client(use_proxy: bool) -> httpx.AsyncClient | None:
        """Build an httpx client routed through the forward proxy, or None."""
        if use_proxy and settings.forward_proxy_url:
            return httpx.AsyncClient(proxy=settings.forward_proxy_url)
        return None

    def _apply_cache_params(
        self, request: dict[str, Any], session_id: str | None
    ) -> None:
        """Wire the 字节 GPT 网关 implicit prompt-cache params (azure-http only).

        The gateway reuses a long stable prefix when the request carries a
        retention window (body) + a stable session_id (the ``extra`` JSON
        header). Other openai-compatible providers don't understand these and
        could 400 on the unknown field, so this is scoped to azure-http. The
        session header is only added when a session_id is actually present.
        """
        if self._client_type != "azure-http":
            return
        # Merge into any caller-supplied extra_body/extra_headers rather than
        # replacing them, so the **kwargs passthrough contract is preserved.
        extra_body = request.setdefault("extra_body", {})
        extra_body["prompt_cache_retention"] = _PROMPT_CACHE_RETENTION
        if session_id:
            extra_headers = request.setdefault("extra_headers", {})
            extra_headers["extra"] = json.dumps({"session_id": session_id})

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
        session_id = kwargs.pop("session_id", None)
        # Gemini-only native-search control signal; never an OpenAI request field.
        kwargs.pop("native_web_search", None)
        wire_messages = self._to_wire_messages(messages)
        request: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            **kwargs,
        }
        if tools:
            request["tools"] = [_tool_to_wire(t) for t in tools]
        self._apply_cache_params(request, session_id)

        with generation_span(
            name=self._model,
            model=self._model,
            input=wire_messages,
            model_parameters=_model_parameters(request),
        ) as span:
            response = await self._client.chat.completions.create(**request)
            message = self._from_wire_response(response)
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
        session_id = kwargs.pop("session_id", None)
        # Gemini-only native-search control signal; never an OpenAI request field.
        kwargs.pop("native_web_search", None)
        wire_messages = self._to_wire_messages(messages)
        request: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "stream": True,
            # opt into the usage-bearing final chunk; without this OpenAI never
            # streams token counts and langfuse accounting is silently lost.
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            request["tools"] = [_tool_to_wire(t) for t in tools]
        self._apply_cache_params(request, session_id)

        with generation_span(
            name=self._model,
            model=self._model,
            input=wire_messages,
            model_parameters=_model_parameters(request),
        ) as span:
            assembler = _ToolCallAssembler()
            text_parts: list[str] = []
            usage: dict[str, int] | None = None

            stream = await self._client.chat.completions.create(**request)
            async for chunk in stream:
                # the usage-only final chunk (no choices) carries token counts.
                chunk_usage = _usage_details(chunk)
                if chunk_usage is not None:
                    usage = chunk_usage
                async for out in self._chunk_to_neutral(chunk, assembler, text_parts):
                    yield out

            span.update(
                output={
                    "text": "".join(text_parts),
                    "tool_calls": [tc.to_dict() for tc in assembler.finished()],
                },
                usage_details=usage,
            )

    async def _chunk_to_neutral(
        self,
        chunk: Any,
        assembler: _ToolCallAssembler,
        text_parts: list[str],
    ) -> AsyncIterator[StreamChunk]:
        if not getattr(chunk, "choices", None):
            return
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)

        if delta is not None:
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield StreamChunk(reasoning=reasoning)

            text = getattr(delta, "content", None)
            if text:
                text_parts.append(text)
                yield StreamChunk(text=text)

            for completed in assembler.feed(getattr(delta, "tool_calls", None)):
                yield StreamChunk(tool_call=completed)

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None:
            # flush any tool call still being assembled at finish
            for completed in assembler.flush():
                yield StreamChunk(tool_call=completed)
            yield StreamChunk(finish_reason=finish_reason)

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
        session_id = kwargs.pop("session_id", None)
        wire_messages = self._to_wire_messages(messages)
        request: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.get("title", "response"),
                    "schema": schema,
                },
            },
            **kwargs,
        }
        self._apply_cache_params(request, session_id)

        with generation_span(
            name=self._model,
            model=self._model,
            input=wire_messages,
            model_parameters=_model_parameters(request),
        ) as span:
            response = await self._client.chat.completions.create(**request)
            content = response.choices[0].message.content or "{}"
            data = json.loads(content)
            span.update(output=data, usage_details=_usage_details(response))
        return data

    # ------------------------------------------------------------------
    # neutral → wire
    # ------------------------------------------------------------------

    def _to_wire_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire = [self._message_to_wire(m) for m in messages]
        if self._is_deepseek:
            wire = _normalise_for_deepseek(wire)
        return wire

    def _message_to_wire(self, message: Message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": str(message.role)}

        if isinstance(message.content, list):
            out["content"] = [_block_to_wire(b) for b in message.content]
        else:
            out["content"] = message.content

        if message.tool_calls:
            out["tool_calls"] = [_tool_call_to_wire(tc) for tc in message.tool_calls]
        if message.tool_call_id is not None:
            out["tool_call_id"] = message.tool_call_id
        if self._is_deepseek and message.reasoning_content is not None:
            out["reasoning_content"] = message.reasoning_content

        return out

    # ------------------------------------------------------------------
    # wire → neutral
    # ------------------------------------------------------------------

    def _from_wire_response(self, response: Any) -> Message:
        choice = response.choices[0]
        wire_msg = choice.message

        content = getattr(wire_msg, "content", None) or ""

        tool_calls: list[ToolCall] = []
        for tc in getattr(wire_msg, "tool_calls", None) or []:
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=_parse_args(tc.function.arguments),
                )
            )

        reasoning = None
        if self._is_deepseek:
            reasoning = getattr(wire_msg, "reasoning_content", None)

        return Message(
            role=Role.ASSISTANT,
            content=content,
            reasoning_content=reasoning,
            tool_calls=tool_calls,
        )


# ---------------------------------------------------------------------------
# Streaming tool-call assembly
# ---------------------------------------------------------------------------


class _ToolCallAssembler:
    """Reassemble streamed tool_call deltas (id/name/args arrive in pieces).

    OpenAI streams a tool call across chunks: the first delta for an index
    carries id + name, later deltas append argument string fragments. Fragments
    are accumulated purely by ``index`` — we make NO assumption that an index's
    fragments arrive contiguously, so interleaved deltas (index 0, then 1, then
    0 again) can't finalise a call before its arguments finish. Every call is
    emitted exactly once at ``flush`` (finish_reason), in index order.
    """

    def __init__(self) -> None:
        # index -> {"id", "name", "args"} — argument fragments accumulate here
        self._calls: dict[int, dict[str, str]] = {}
        # indices already emitted (no double-emit)
        self._emitted: set[int] = set()
        self._completed: list[ToolCall] = []

    def feed(self, tool_call_deltas: Any) -> list[ToolCall]:
        """Consume a chunk's tool_call deltas; accumulate fragments by index.

        Returns nothing during the stream: a call is only complete once the
        stream finishes, because fragments for the same index may be interleaved
        with other indices (finalising early on an index change would discard a
        later continuation). ``flush`` emits the completed calls at finish.
        """
        if not tool_call_deltas:
            return []

        for d in tool_call_deltas:
            index = getattr(d, "index", 0)
            slot = self._calls.setdefault(index, {"id": "", "name": "", "args": ""})
            if getattr(d, "id", None):
                slot["id"] = d.id
            func = getattr(d, "function", None)
            if func is not None:
                if getattr(func, "name", None):
                    slot["name"] = func.name
                if getattr(func, "arguments", None):
                    slot["args"] += func.arguments

        return []

    def flush(self) -> list[ToolCall]:
        """Finalise every still-unemitted tool call (called at finish_reason).

        Every accumulated index not yet emitted is finalised here, in index
        order, so the full set of parallel calls surfaces together once their
        arguments are fully assembled.
        """
        out: list[ToolCall] = []
        for index in sorted(self._calls):
            if index in self._emitted:
                continue
            done = self._finalise(index)
            if done is not None:
                out.append(done)
        return out

    def finished(self) -> list[ToolCall]:
        """All completed tool calls so far (for the trace summary)."""
        return self._completed

    def _finalise(self, index: int) -> ToolCall | None:
        if index in self._emitted:
            return None
        slot = self._calls.get(index)
        if slot is None or not slot["id"]:
            return None
        self._emitted.add(index)
        call = ToolCall(
            id=slot["id"],
            name=slot["name"],
            arguments=_parse_args(slot["args"]),
        )
        self._completed.append(call)
        return call


# ---------------------------------------------------------------------------
# Translation helpers (module-level, pure)
# ---------------------------------------------------------------------------


def _block_to_wire(block: ContentBlock) -> dict[str, Any]:
    """neutral ContentBlock → OpenAI content part.

    ``text``      → ``{"type":"text","text":...}``
    ``image``     → ``{"type":"image_url","image_url":{"url":...}}`` (chat hist)
    ``image_url`` → ``{"type":"image_url","image_url":{...}}`` (tool-returned)
    """
    if block.type == "text":
        return {"type": "text", "text": block.text or ""}
    if block.type == "image":
        return {"type": "image_url", "image_url": {"url": block.url}}
    if block.type == "image_url":
        return {"type": "image_url", "image_url": block.image_url or {}}
    # unknown block: pass type through with whatever it carried
    return block.to_dict()


def _tool_call_to_wire(tc: ToolCall) -> dict[str, Any]:
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.name,
            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
        },
    }


def _tool_to_wire(tool: ToolDef) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _parse_args(raw: Any) -> dict[str, Any]:
    """Parse a tool-call arguments JSON string into a dict (tolerant)."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("tool_call arguments not valid JSON: %r", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalise_for_deepseek(
    wire_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten array/null content to plain strings (deepseek rejects them).

    Mirrors ``_ReasoningChatOpenAI._normalize_content`` on the wire dicts: an
    array content (image blocks etc.) collapses to its concatenated text.
    """
    for msg in wire_messages:
        content = msg.get("content")
        if isinstance(content, list):
            blocks = [ContentBlock.from_dict(_part_to_block_dict(p)) for p in content]
            msg["content"] = normalize_content_to_text(blocks)
        elif content is None:
            msg["content"] = ""
    return wire_messages


def _part_to_block_dict(part: dict[str, Any]) -> dict[str, Any]:
    """Map an OpenAI content part back to a neutral-block dict for flattening."""
    if part.get("type") == "text":
        return {"type": "text", "text": part.get("text", "")}
    return {"type": part.get("type", "image_url")}


def _model_parameters(request: dict[str, Any]) -> dict[str, Any]:
    """Extract trace-worthy model params from a request (skip bulky fields)."""
    skip = {
        "model",
        "messages",
        "tools",
        "stream",
        "stream_options",
        "response_format",
        "extra_body",
        "extra_headers",
    }
    return {k: v for k, v in request.items() if k not in skip}


def _usage_details(response: Any) -> dict[str, int] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = {
        "input": getattr(usage, "prompt_tokens", 0) or 0,
        "output": getattr(usage, "completion_tokens", 0) or 0,
        "total": getattr(usage, "total_tokens", 0) or 0,
    }
    # prompt-cache hit: cached_tokens lives under prompt_tokens_details. Surface
    # it as a langfuse cache key so a hit is observable — only when non-zero, so
    # a 0 doesn't read as "measured, missed" when the field is simply absent.
    ptd = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(ptd, "cached_tokens", 0) if ptd is not None else 0
    if cached:
        details["cache_read_input_tokens"] = cached
    return details


# ---------------------------------------------------------------------------
# Registration — one adapter class, the openai-compatible client_types
# ---------------------------------------------------------------------------

# The openai-compatible client_types this one adapter serves. The seam
# (build_model_client) calls the factory WITHOUT client_type — it only carries
# model_name / api_key / base_url / use_proxy — so the factory must supply the
# client_type itself. ``functools.partial`` binds it per registration, avoiding
# the late-binding-loop footgun a bare lambda would hit.
#
# Why these keys (behaviour-equivalence with legacy ``build_chat_model``):
#   legacy treated everything that wasn't azure / google / openai-responses /
#   deepseek as the *default* ``ChatOpenAI`` (plain Chat Completions). Chat
#   providers resolve to a non-empty client_type via
#   ``_get_model_and_provider_info`` (``provider.client_type or "openai"``), so
#   the reachable chat surface is exactly: openai (the DB default), deepseek,
#   azure-http, openai-responses (grok). The empty-string key mirrors the seam's
#   ``info.get("client_type", "")`` default and also maps to plain completions.
#   (``ark`` is image-gen / embedding only — out of this chat adapter's scope.)
#   A genuinely unknown chat client_type still fails loud via the T1 seam, which
#   is the intended "misconfigured provider" signal, not a regression.
_CLIENT_TYPES = ("openai", "", "deepseek", "azure-http", "openai-responses")


def _make_adapter(
    client_type: str,
    *,
    model_name: str,
    api_key: str,
    base_url: str | None,
    **extra: Any,
) -> OpenAIAdapter:
    return OpenAIAdapter(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        client_type=client_type,
        **extra,
    )


for _ct in _CLIENT_TYPES:
    register_adapter(_ct, functools.partial(_make_adapter, _ct))
