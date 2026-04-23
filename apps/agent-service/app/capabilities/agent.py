"""AgentRunner — thin adapter over ``app.agent.core.Agent``.

Holds a preconfigured ``Agent`` so dataflow nodes can declare an agent field
once at node definition and call ``run`` / ``stream`` / ``extract`` per event.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from pydantic import BaseModel

from app.agent.core import Agent, AgentConfig


class AgentRunner:
    """Adapter over ``app.agent.core.Agent`` — one preconfigured agent per instance."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: list[Any] | None = None,
        model_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._agent = Agent(config, tools=tools, model_kwargs=model_kwargs)

    async def run(self, messages: list[Any], **kwargs: Any) -> Any:
        return await self._agent.run(messages, **kwargs)

    async def stream(
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        async for chunk in self._agent.stream(messages, **kwargs):
            yield chunk

    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[Any],
        **kwargs: Any,
    ) -> BaseModel:
        return await self._agent.extract(response_model, messages, **kwargs)
