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

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from app.agent.models import resolve_model_info
from app.agent.neutral import Message, StreamChunk, ToolDef

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
    )
