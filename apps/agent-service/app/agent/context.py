"""Agent execution context — flat, frozen dataclass.

Carried through LangGraph runtime, accessible to tools via
``get_runtime(AgentContext).context``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.infra.image import ImageRegistry


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Immutable execution context for an agent run.

    Constructed once in ``chat/pipeline.py`` and passed to tools
    via LangGraph runtime.  Frozen to prevent accidental mutation
    across tool calls and sub-agent delegation.
    """

    message_id: str = ""
    chat_id: str = ""
    persona_id: str = ""
    image_registry: ImageRegistry | None = None
    features: dict[str, Any] = field(default_factory=dict)
    # Optional langfuse session: when set, this run's trace is grouped into the
    # named session so several traces (e.g. a persona's whole day of thinking)
    # read as one stream. The chat path leaves it None — unbound, status quo.
    session_id: str | None = None

    def get_feature(self, key: str, default: Any = None) -> Any:
        return self.features.get(key, default)
