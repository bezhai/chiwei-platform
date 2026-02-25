"""Agents 模块

重构后的 agents 模块架构：

- core/: 核心抽象层 (ChatAgent, AgentContext, AgentConfig)
- clients/: AI 客户端层 (BaseAIClient, OpenAIClient, ArkClient, AzureHttpClient)
- infra/: 基础设施层 (ModelBuilder, Langfuse, Embedding)
- tools/: 工具层 (search, history, image)
- graphs/: Graph 流程层 (pre)
- domains/: 业务 Agent 层 (main)
"""

# 核心抽象
# 客户端
from app.agents.clients import (
    ArkClient,
    AzureHttpClient,
    BaseAIClient,
    ClientType,
    OpenAIClient,
    create_client,
)
from app.agents.core import (
    AgentConfig,
    AgentContext,
    AgentRegistry,
    ChatAgent,
    FeatureFlags,
    MediaContext,
    MessageContext,
)

# 业务 Agent
from app.agents.domains import stream_chat

# Pre Graph
from app.agents.graphs import (
    BlockReason,
    Complexity,
    ComplexityResult,
    PreState,
    SafetyResult,
    run_pre,
)

# 基础设施
from app.agents.infra import ModelBuilder, get_prompt
from app.agents.infra.embedding import InstructionBuilder, Modality

__all__ = [
    # Core
    "ChatAgent",
    "AgentContext",
    "MessageContext",
    "MediaContext",
    "FeatureFlags",
    "AgentConfig",
    "AgentRegistry",
    # Clients
    "BaseAIClient",
    "ClientType",
    "OpenAIClient",
    "ArkClient",
    "AzureHttpClient",
    "create_client",
    # Infra
    "ModelBuilder",
    "get_prompt",
    "Modality",
    "InstructionBuilder",
    # Domains
    "stream_chat",
    # Pre Graph
    "run_pre",
    "PreState",
    "SafetyResult",
    "ComplexityResult",
    "Complexity",
    "BlockReason",
]
