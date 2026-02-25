"""核心抽象层

提供 Agent 核心抽象、上下文管理、配置注册等基础能力。
"""

from app.agents.core.agent import ChatAgent
from app.agents.core.config import AgentConfig, AgentRegistry
from app.agents.core.context import (
    AgentContext,
    FeatureFlags,
    MediaContext,
    MessageContext,
)

__all__ = [
    # Agent
    "ChatAgent",
    # Context
    "AgentContext",
    "MessageContext",
    "MediaContext",
    "FeatureFlags",
    # Config
    "AgentConfig",
    "AgentRegistry",
]
