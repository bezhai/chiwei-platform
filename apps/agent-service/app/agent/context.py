"""Agent execution context — dataclasses for message, media, and feature flags.

Uses composition to separate required fields (MessageContext) from optional
capabilities (MediaContext, FeatureFlags).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.infra.image import ImageRegistry


@dataclass(frozen=True)
class MessageContext:
    """Message-level context (required fields)."""

    message_id: str
    chat_id: str


@dataclass
class MediaContext:
    """Media context (optional)."""

    registry: ImageRegistry | None = None


@dataclass
class FeatureFlags:
    """Feature flags (gray release configuration)."""

    flags: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.flags.get(key, default)


@dataclass
class AgentContext:
    """Agent execution context (composition pattern).

    Combines different context types together, maintaining type safety
    and flexibility.
    """

    message: MessageContext
    media: MediaContext = field(default_factory=MediaContext)
    features: FeatureFlags = field(default_factory=FeatureFlags)
