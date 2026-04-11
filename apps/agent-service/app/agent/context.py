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
    image_registry: ImageRegistry | None = None
    features: dict[str, Any] = field(default_factory=dict)

    def get_feature(self, key: str, default: Any = None) -> Any:
        return self.features.get(key, default)
