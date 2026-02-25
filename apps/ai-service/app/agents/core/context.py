"""Agent 执行上下文

使用组合模式设计上下文，将必需字段和可选字段分离。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MessageContext:
    """消息级上下文（必需字段）"""

    message_id: str
    chat_id: str


@dataclass
class MediaContext:
    """媒体上下文（可选）"""

    image_urls: list[str] = field(default_factory=list)


@dataclass
class FeatureFlags:
    """特性标志（灰度配置）"""

    flags: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.flags.get(key, default)


@dataclass
class AgentContext:
    """Agent 执行上下文（组合模式）

    使用组合模式将不同类型的上下文组合在一起，
    既保持了类型安全，又提供了灵活性。
    """

    message: MessageContext
    media: MediaContext = field(default_factory=MediaContext)
    features: FeatureFlags = field(default_factory=FeatureFlags)
