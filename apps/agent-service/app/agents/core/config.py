"""Agent 配置注册表"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class AgentConfig:
    """Agent 配置"""

    prompt_id: str
    model_id: str
    trace_name: str | None = None


class AgentRegistry:
    """Agent 配置注册表

    提供集中管理 Agent 配置的能力，避免硬编码分散在各处。
    """

    _configs: ClassVar[dict[str, AgentConfig]] = {}

    @classmethod
    def register(cls, name: str, config: AgentConfig) -> None:
        """注册 Agent 配置"""
        cls._configs[name] = config

    @classmethod
    def get(cls, name: str) -> AgentConfig:
        """获取 Agent 配置"""
        if name not in cls._configs:
            raise KeyError(f"Unknown agent config: {name}")
        return cls._configs[name]

    @classmethod
    def has(cls, name: str) -> bool:
        """检查是否存在指定配置"""
        return name in cls._configs

    @classmethod
    def all_configs(cls) -> dict[str, AgentConfig]:
        """获取所有配置"""
        return dict(cls._configs)


# 预注册配置
AgentRegistry.register(
    "main",
    AgentConfig(
        prompt_id="main",
        model_id="main-chat-model",
        trace_name="main",
    ),
)

AgentRegistry.register(
    "research",
    AgentConfig(
        prompt_id="research_agent",
        model_id="research-model",
        trace_name="research",
    ),
)

AgentRegistry.register(
    "schedule-ideation",
    AgentConfig(
        prompt_id="schedule_daily_ideation",
        model_id="offline-model",
        trace_name="schedule-ideation",
    ),
)

AgentRegistry.register(
    "schedule-writer",
    AgentConfig(
        prompt_id="schedule_daily_writer",
        model_id="offline-model",
        trace_name="schedule-writer",
    ),
)

AgentRegistry.register(
    "schedule-critic",
    AgentConfig(
        prompt_id="schedule_daily_critic",
        model_id="offline-model",
        trace_name="schedule-critic",
    ),
)

AgentRegistry.register(
    "relationship-extract",
    AgentConfig(
        prompt_id="relationship_extract",
        model_id="relationship-model",
        trace_name="relationship-extract",
    ),
)

AgentRegistry.register(
    "afterthought",
    AgentConfig(
        prompt_id="afterthought_conversation",
        model_id="diary-model",
        trace_name="afterthought",
    ),
)

AgentRegistry.register(
    "voice-generator",
    AgentConfig(
        prompt_id="voice_generator",
        model_id="offline-model",
        trace_name="voice-generator",
    ),
)

AgentRegistry.register(
    "dream-daily",
    AgentConfig(
        prompt_id="dream_daily",
        model_id="diary-model",
        trace_name="dream-daily",
    ),
)

AgentRegistry.register(
    "dream-weekly",
    AgentConfig(
        prompt_id="dream_weekly",
        model_id="diary-model",
        trace_name="dream-weekly",
    ),
)

AgentRegistry.register(
    "schedule-monthly",
    AgentConfig(
        prompt_id="schedule_monthly",
        model_id="offline-model",
        trace_name="schedule-monthly",
    ),
)

AgentRegistry.register(
    "schedule-weekly",
    AgentConfig(
        prompt_id="schedule_weekly",
        model_id="offline-model",
        trace_name="schedule-weekly",
    ),
)
