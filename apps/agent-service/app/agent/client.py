"""ModelClient — neutral client interface + model resolution seam.

The thinking core never talks to a provider SDK directly. It talks to a
``ModelClient``: a thin adapter that translates neutral types (``app.agent.
neutral``) to one provider's wire and back. ``Agent.run / stream / extract``
each map onto one of three consumptions:

  - ``complete``   — non-streaming; returns the final assistant ``Message``
                     (``run`` drinks this to its last state),
  - ``stream``     — yields neutral ``StreamChunk``s (``stream`` forwards them),
  - ``structured`` — one structured output as a dict the caller validates
                     against its pydantic schema (``extract``).

``build_model_client`` is the resolution seam. It reuses the existing DB
resolution (``resolve_model_info`` in ``app.agent.models`` — TTL cache, model
mapping, provider lookup) unchanged, then dispatches by ``client_type`` to a
registered adapter class.

T1 ships no real adapter (OpenAI is T2, Gemini is T4). Real ``client_type``s
raise ``NotImplementedError``; the dispatch + resolution wiring is proven by a
test-injected fake adapter via ``register_adapter``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, Protocol, TypeVar, runtime_checkable

from app.agent.models import resolve_model_info
from app.agent.neutral import Message, StreamChunk, ToolDef
from app.capabilities._errors import CapabilityTimeout

T = TypeVar("T")

# ---------------------------------------------------------------------------
# How long one model call waits for the provider
# ---------------------------------------------------------------------------

# Every ModelClient call waits on its provider for at most this long: for the
# whole answer of a non-streaming call, and for the opening and then each next
# chunk of a streamed one. Past it the call fails with ``CapabilityTimeout``.
#
# Why it exists: the google-genai SDK sends requests with no timeout at all
# (``HttpOptions.timeout`` unset → httpx ``timeout=None`` for connect, read,
# write and pool), and the TLS handshake with the model gateway sometimes never
# completes. Nothing then ended the call until the per-persona lock
# (``app.living.serial.HELD_SECONDS``, 900 s) cut the whole round: she was stuck
# for 15 minutes, and Langfuse showed a generation with no output and no error.
#
# Calibrated on measured calls: single-call traces in Langfuse (day page /
# persona review on gpt-5.5, 2026-09-05..10-05) top out at 59 s; her completed
# rounds on gemini-3.7-flash (coe-world, 10-03..10-05, n=60) take 37 s at most
# for the whole multi-call round; the slowest round of any kind measured was
# 189 s (``app.living.serial``). 180 s is three times the slowest single call,
# and two attempts (the Agent layer's default retry) still end well inside the
# 900 s lock.
#
# A source constant, not Dynamic Config: it changes nothing about what any agent
# does, it only turns a hang into a failure — the same judgment as the other
# hang guards in this service (``app.living.outgoing``, ``app.agent.reading``).
MODEL_ANSWER_SECONDS = 180.0


@asynccontextmanager
async def answer_deadline(model: str) -> AsyncIterator[None]:
    """Give ``model`` at most :data:`MODEL_ANSWER_SECONDS` for what this block awaits.

    Past the deadline the wait is cancelled and ``CapabilityTimeout`` is raised,
    chained to the cancelled wait so its traceback shows where the call was
    stuck. Only this deadline is turned into one: a ``TimeoutError`` the SDK
    raises on its own passes through untouched, so the error never reports a
    wait that did not happen.
    """
    try:
        async with asyncio.timeout(MODEL_ANSWER_SECONDS) as deadline:
            yield
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise CapabilityTimeout(
            f"{model} gave no answer within {MODEL_ANSWER_SECONDS:g}s",
            meta={"model": model, "seconds": MODEL_ANSWER_SECONDS},
        ) from exc


async def stream_within_deadline(
    model: str, opening: Awaitable[AsyncIterable[T]]
) -> AsyncIterator[T]:
    """Open a streamed answer and yield its chunks, each wait under :func:`answer_deadline`.

    The deadline covers each wait on the provider — opening the stream, then
    every next chunk — and never spans a ``yield``: a stream that keeps
    producing is alive however long it runs, and the time the consumer spends
    on a chunk is not the provider's.
    """
    async with answer_deadline(model):
        chunks = await opening
    pending = aiter(chunks)
    while True:
        async with answer_deadline(model):
            try:
                chunk = await anext(pending)
            except StopAsyncIteration:
                return
        yield chunk


# ---------------------------------------------------------------------------
# Adapter constructor protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AdapterFactory(Protocol):
    """Callable that builds a ModelClient from resolved provider config."""

    def __call__(
        self, *, model_name: str, api_key: str, base_url: str | None, **extra: Any
    ) -> ModelClient: ...


# ---------------------------------------------------------------------------
# ModelClient interface
# ---------------------------------------------------------------------------


class ModelClient(ABC):
    """Provider-agnostic chat client.

    The three abstract methods are the only surface ``Agent.run / stream /
    extract`` consume. Adapters keep the neutral contract on both sides:
    neutral ``Message``/``ToolDef`` in, neutral ``Message``/``StreamChunk``/
    ``dict`` out.

    Every wait on the provider runs under :func:`answer_deadline` (streams
    through :func:`stream_within_deadline`), inside the call's generation span,
    so a provider that never answers fails the call with ``CapabilityTimeout``
    and the generation records why.
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> Message:
        """Non-streaming completion → final assistant ``Message``."""

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion → neutral chunk stream."""

    @abstractmethod
    async def structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Single structured output → dict the caller validates (``extract``)."""

    @property
    def supports_native_web_search(self) -> bool:
        """Whether this model can run native web search alongside custom tools.

        Default ``False`` (fail-closed): only providers whose native grounding
        co-exists with custom function declarations override this. OpenAI and
        every other adapter inherit the default — the agent layer reads it to
        decide whether to swap the ``search_web`` tool for native search.
        """
        return False


# ---------------------------------------------------------------------------
# Adapter registry (client_type → factory)
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, AdapterFactory] = {}


def register_adapter(client_type: str, factory: AdapterFactory) -> None:
    """Register an adapter factory for a ``client_type``.

    Real adapters register themselves on import (OpenAI / Gemini); tests
    register fakes to exercise the dispatch seam.
    """
    _ADAPTERS[client_type] = factory


_adapters_loaded = False


def _ensure_adapters_loaded() -> None:
    """Import the real adapter modules on first resolve (registration side effect).

    Lazy (not at module import) so merely importing the thinking core doesn't
    eagerly pull in the provider SDKs (google-genai / openai) — that would
    break unrelated tests that patch those SDKs at call time.
    """
    global _adapters_loaded
    if _adapters_loaded:
        return
    import app.agent.adapters  # noqa: F401  (registers ModelClient adapters)

    _adapters_loaded = True


# ---------------------------------------------------------------------------
# Resolution seam
# ---------------------------------------------------------------------------


async def build_model_client(
    model_id: str,
    *,
    required_fields: tuple[str, ...] = ("api_key", "base_url", "model_name"),
) -> ModelClient:
    """Resolve ``model_id`` and build the matching ``ModelClient``.

    DB resolution (model mapping, provider lookup, TTL cache, validation) is
    delegated to ``resolve_model_info`` unchanged — this is the single source
    of provider config. Dispatch is by ``client_type``; an unregistered real
    ``client_type`` raises ``NotImplementedError`` until its adapter lands.
    """
    _ensure_adapters_loaded()
    info = await resolve_model_info(model_id, required_fields=required_fields)
    client_type = info.get("client_type", "")

    factory = _ADAPTERS.get(client_type)
    if factory is None:
        raise NotImplementedError(
            f"no ModelClient adapter for client_type={client_type!r} "
            f"(model_id={model_id!r}); real adapters land in T2/T4"
        )

    return factory(
        model_name=info["model_name"],
        api_key=info["api_key"],
        base_url=info.get("base_url"),
        use_proxy=info.get("use_proxy", False),
        api_version=info.get("api_version"),
    )
